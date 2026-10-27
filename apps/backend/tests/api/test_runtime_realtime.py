"""Task 24's required real-time regression test: protects "live glow within
~200ms" and "a node recovers on its next run" (D1) from regressing again.

No Mongo needed: a `FileTailTraceSource` tailing a real temp file, a
hand-built `Graph` (same pattern as `tests/runtime/test_channel.py` and
`test_otlp_end_to_end.py`), driving `RuntimeChannel` directly -- this is
about the file-write -> tail -> resolve -> apply -> publish pipeline's
actual timing and ordering, not the WebSocket transport layer itself
(already covered by `tests/api/test_runtime_ws.py`).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import pytest

from agentic_visualizer.domain import Graph, Node, NodeType, Provenance
from agentic_visualizer.runtime.channel import MethodConfig, RuntimeChannel
from agentic_visualizer.runtime.messages import RuntimeDeltaMessage, RuntimeMessage
from agentic_visualizer.runtime.methods.apv_live_file import ApvLiveFileSettings
from agentic_visualizer.trace.events import TraceEvent, TraceRef

_TS = datetime(2026, 1, 1, tzinfo=UTC)
_FAST_BATCH_WINDOW = 0.02

# apps/backend/tests/api/test_runtime_realtime.py -> repo root is 4 parents up.
REPO_ROOT = Path(__file__).resolve().parents[4]
MOCK_AGENT_PROJECT = REPO_ROOT / "mock-agent-project"
MANIFEST_PATH = MOCK_AGENT_PROJECT / "simulator" / "manifest.json"

_AGENT_TOOLS = {
    "Coordinator": ["internal_search", "file_read", "file_write"],
    "Research Agent": ["web_search", "summarize_url", "calculator"],
    "Writer Agent": ["document_query", "index_ingest", "citation_lookup"],
    "Code Reviewer": ["web_search", "file_read"],
}
_WORKFLOW_NAME = "Research and Write"

_SUBPROCESS_SCRIPT = """
import asyncio
import sys
from pathlib import Path

import apv_trace
from simulator.scripts.research_and_write import run

manifest_path = Path(sys.argv[2])
apv_trace.configure(
    log_path=Path(sys.argv[1]),
    run_id="realtime-test",
    manifest_path=manifest_path if manifest_path.exists() else None,
)
asyncio.run(run(fail=(sys.argv[3] == "true")))
"""


def _prov() -> Provenance:
    return Provenance(source="test", source_ref="test:1", scanned_at=_TS)


def _append_trace_line(
    log_path: Path,
    *,
    span: str,
    phase: Literal["start", "end", "error"],
    kind: Literal["workflow", "agent", "tool", "function", "model"],
    name: str,
) -> None:
    event = TraceEvent(
        v=1, ts=_TS, run_id="r1", span_id=span, phase=phase, kind=kind,
        ref=TraceRef(name=name, module="pkg.mod"),
    )
    with log_path.open("a", encoding="utf-8") as f:
        f.write(event.model_dump_json() + "\n")


async def _collect_until(
    queue: asyncio.Queue[RuntimeMessage], predicate: Callable[[RuntimeMessage], bool], timeout: float
) -> RuntimeMessage:
    async def _run() -> RuntimeMessage:
        while True:
            message = await queue.get()
            if predicate(message):
                return message

    return await asyncio.wait_for(_run(), timeout=timeout)


@pytest.mark.asyncio
async def test_start_then_end_deliver_active_then_idle_within_500ms(tmp_path: Path) -> None:
    """Item 1: a `start` line produces an `active` delta before the `end`
    line is even written; then `end` produces `idle`. 500ms is generous so
    CI timing doesn't make this flaky -- see `scripts/diagnose_runtime_timeline.py`
    for the actual measured numbers this bound is generous against."""
    log_path = tmp_path / "live.jsonl"
    log_path.write_bytes(b"")
    node = Node(id="mongo:mock_agent.tools:1", type="tool", name="demo_tool", provenance=_prov())

    channel = RuntimeChannel(batch_window_seconds=_FAST_BATCH_WINDOW)
    await channel.update_graph(Graph(nodes=[node]))
    await channel.apply_config(
        [MethodConfig(id="apv_live_file", settings=ApvLiveFileSettings(path=str(log_path)))]
    )
    subscriber = channel.subscribe()
    await channel.start()
    try:
        # The file-tail reader task is only SCHEDULED by apply_config/start,
        # not guaranteed to have opened the file and seeked to its end yet.
        # Writing before that happens would have it open+seek PAST this
        # test's own line, silently skipping it -- wait for the source to
        # actually report "running" first, same as a real target process
        # naturally would (it isn't writing the instant Setup is confirmed).
        for _ in range(50):
            statuses = await channel.source_statuses()
            if statuses and statuses[0].state == "running":
                break
            await asyncio.sleep(0.02)
        else:
            pytest.fail("apv_live_file source never reached state=running")

        _append_trace_line(log_path, span="s1", phase="start", kind="tool", name="demo_tool")
        active_delta = await _collect_until(
            subscriber, lambda m: isinstance(m, RuntimeDeltaMessage) and m.status == "active", timeout=0.5
        )
        assert isinstance(active_delta, RuntimeDeltaMessage)
        assert active_delta.node_id == node.id
        # The `end` line hasn't been written yet -- state must still show active.
        assert channel.state_store.snapshot()[0].status == "active"

        _append_trace_line(log_path, span="s1", phase="end", kind="tool", name="demo_tool")
        idle_delta = await _collect_until(
            subscriber, lambda m: isinstance(m, RuntimeDeltaMessage) and m.status == "idle", timeout=0.5
        )
        assert isinstance(idle_delta, RuntimeDeltaMessage)
        assert idle_delta.node_id == node.id
    finally:
        channel.unsubscribe(subscriber)
        await channel.stop()


def _manifest_node(manifest: dict[str, Any], kind: str, name: str, node_type: NodeType) -> Node:
    entry = manifest.get(f"{kind}:{name}")
    if entry is None:
        pytest.skip(f"manifest.json has no entry for {kind}:{name} -- run build_manifest.py first")
    db_id = entry["db_id"]
    return Node(
        id=f"mongo:mock_agent.{kind}s:{db_id}",
        type=node_type,
        name=name,
        provenance=_prov(),
        attributes={"db_id": db_id},
    )


def _simulator_graph() -> Graph:
    if not MANIFEST_PATH.exists():
        pytest.skip(f"{MANIFEST_PATH} not found -- run build_manifest.py from mock-agent-project first")
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    nodes = [_manifest_node(manifest, "workflow", _WORKFLOW_NAME, "workflow")]
    seen_tools: set[str] = set()
    for agent_name, tools in _AGENT_TOOLS.items():
        nodes.append(_manifest_node(manifest, "agent", agent_name, "agent"))
        for tool_name in tools:
            if tool_name not in seen_tools:
                seen_tools.add(tool_name)
                nodes.append(_manifest_node(manifest, "tool", tool_name, "tool"))
    return Graph(nodes=nodes)


async def _run_simulator_subprocess(*, log_path: Path, fail: bool) -> None:
    proc = await asyncio.create_subprocess_exec(
        "uv",
        "run",
        "python",
        "-c",
        _SUBPROCESS_SCRIPT,
        str(log_path),
        str(MANIFEST_PATH),
        "true" if fail else "false",
        cwd=str(MOCK_AGENT_PROJECT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)
    if proc.returncode not in (0, 1):  # 1 is expected for fail=True's uncaught RuntimeError
        pytest.fail(f"simulator subprocess exited {proc.returncode}: {stderr.decode()}")


@pytest.mark.asyncio
async def test_second_run_lights_up_previously_errored_nodes_before_idle(tmp_path: Path) -> None:
    """Item 2 (Task 24's D1 regression guard): run the real simulator with
    `fail=True`, then `fail=False` -- every node that participates in the
    SECOND run, including the ones that failed in the first, must receive
    an `active` delta before any `idle`/`errored` delta for it. Under the
    pre-fix bug, the failed tool/agent/workflow nodes went straight from
    nothing to `errored` in the second run, never active -- see this
    module's own docstring and `.claude/Task 24.md`'s Step 0 findings."""
    log_path = tmp_path / "live.jsonl"
    log_path.write_bytes(b"")
    graph = _simulator_graph()

    channel = RuntimeChannel(batch_window_seconds=_FAST_BATCH_WINDOW)
    await channel.update_graph(graph)
    await channel.apply_config(
        [MethodConfig(id="apv_live_file", settings=ApvLiveFileSettings(path=str(log_path)))]
    )
    subscriber = channel.subscribe()
    await channel.start()
    try:
        # See the single-node test's own comment: wait for the tailer to
        # actually be open before the subprocess can write anything.
        for _ in range(50):
            statuses = await channel.source_statuses()
            if statuses and statuses[0].state == "running":
                break
            await asyncio.sleep(0.02)
        else:
            pytest.fail("apv_live_file source never reached state=running")

        await _run_simulator_subprocess(log_path=log_path, fail=True)
        # Drain whatever the first run produced -- only the SECOND run's
        # ordering is under test here.
        await asyncio.sleep(0.3)
        while not subscriber.empty():
            subscriber.get_nowait()

        await _run_simulator_subprocess(log_path=log_path, fail=False)
        await asyncio.sleep(0.3)  # catch the batch-flush window's trailing deltas

        deltas: list[RuntimeDeltaMessage] = []
        while not subscriber.empty():
            message = subscriber.get_nowait()
            if isinstance(message, RuntimeDeltaMessage):
                deltas.append(message)

        assert deltas, "no deltas arrived for the second run at all"

        first_active_index: dict[str, int] = {}
        first_terminal_index: dict[str, int] = {}
        for index, delta in enumerate(deltas):
            if delta.status == "active" and delta.node_id not in first_active_index:
                first_active_index[delta.node_id] = index
            if delta.status in ("idle", "errored") and delta.node_id not in first_terminal_index:
                first_terminal_index[delta.node_id] = index

        for node_id, terminal_index in first_terminal_index.items():
            active_index = first_active_index.get(node_id)
            assert active_index is not None, (
                f"{node_id} reached a terminal status in the second run without ever going "
                f"active first -- this is Task 24's confirmed bug if it fails"
            )
            assert active_index < terminal_index, f"{node_id} went active AFTER its terminal status"

        # The specific nodes Step 0 confirmed were stuck: file_read tool,
        # Code Reviewer agent, and the workflow itself.
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        for kind, name in (("tool", "file_read"), ("agent", "Code Reviewer"), ("workflow", _WORKFLOW_NAME)):
            db_id = manifest[f"{kind}:{name}"]["db_id"]
            node_id = f"mongo:mock_agent.{kind}s:{db_id}"
            assert node_id in first_active_index, f"{name} ({kind}) never went active in the second run"
    finally:
        channel.unsubscribe(subscriber)
        await channel.stop()

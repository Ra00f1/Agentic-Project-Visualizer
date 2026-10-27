"""Task 24 Step 0: measure the simulator's real live-trace delivery timeline.

Runs `mock-agent-project`'s `research_and_write.run()` twice (once
`fail=True`, then once `fail=False`) in a real subprocess, against a real
`apv_live_file` `FileTailTraceSource` and a real `/ws/runtime` WebSocket
connection, and prints:

  1. One row per WebSocket message received, with the monotonic time since
     the run started, the node id, status, runCount and lastEndedAt.
  2. The raw trace file's own start/end timestamps per span, for comparison
     against when that node's delta actually arrived.

Prefers a real L1 scan of local Mongo `mock_agent` (the way `GET /graph`
does it) for the graph; falls back to a hand-built graph from
`mock-agent-project/simulator/manifest.json`'s `db_id` values when Mongo
isn't reachable, so this script still runs on a machine without it.

Usage (from `apps/backend`):
    uv run python scripts/diagnose_runtime_timeline.py
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import uvicorn
import websockets
from fastapi import FastAPI

from agentic_visualizer.config import Settings
from agentic_visualizer.domain import Graph, Node, NodeType, Provenance
from agentic_visualizer.main import create_app
from agentic_visualizer.runtime.channel import MethodConfig
from agentic_visualizer.runtime.methods.apv_live_file import ApvLiveFileSettings

# apps/backend/scripts/diagnose_runtime_timeline.py -> repo root is 3 parents up.
REPO_ROOT = Path(__file__).resolve().parents[3]
MOCK_AGENT_PROJECT = REPO_ROOT / "mock-agent-project"
MANIFEST_PATH = MOCK_AGENT_PROJECT / "simulator" / "manifest.json"

# The exact node set `research_and_write.run()` touches -- see that
# module's own docstring for why it's this subset of the 7 tagged members.
_WORKFLOW_NAME = "Research and Write"
_AGENT_TOOLS = {
    "Coordinator": ["internal_search", "file_read", "file_write"],
    "Research Agent": ["web_search", "summarize_url", "calculator"],
    "Writer Agent": ["document_query", "index_ingest", "citation_lookup"],
    "Code Reviewer": ["web_search", "file_read"],
}

_SUBPROCESS_SCRIPT = """
import asyncio
import sys
from pathlib import Path

import apv_trace
from simulator.scripts.research_and_write import run

manifest_path = Path(sys.argv[2])
apv_trace.configure(
    log_path=Path(sys.argv[1]),
    run_id="diagnose",
    manifest_path=manifest_path if manifest_path.exists() else None,
)
asyncio.run(run(fail=(sys.argv[3] == "true")))
"""


def _now() -> datetime:
    return datetime.now(UTC)


async def _try_real_mongo_graph(app: FastAPI) -> Graph | None:
    """Preferred graph source: a real L1 scan of local `mock_agent`, same
    as `GET /graph`'s own default params. `None` if Mongo isn't reachable
    or the scan fails for any reason -- the caller falls back to the
    manifest-built graph."""
    try:
        # Host header must be one `TrustedHostMiddleware` accepts (main.py:238) --
        # anything else gets a 400 before the route ever runs.
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            resp = await client.get("/graph", params={"db": "mock_agent"}, timeout=10.0)
        if resp.status_code != 200:
            print(f"[diagnose] /graph returned {resp.status_code}; falling back to manifest graph")
            return None
        return Graph.model_validate(resp.json())
    except Exception as exc:  # noqa: BLE001 - this is a diagnostic fallback decision, not a crash path
        print(f"[diagnose] real Mongo scan unavailable ({exc.__class__.__name__}: {exc}); falling back to manifest graph")
        return None


def _manifest_node(manifest: dict[str, Any], kind: str, name: str, node_type: NodeType) -> Node:
    entry = manifest.get(f"{kind}:{name}")
    if entry is None:
        raise SystemExit(f"manifest has no entry for {kind}:{name} -- is manifest.json stale?")
    db_id = entry["db_id"]
    return Node(
        id=f"mongo:mock_agent.{kind}s:{db_id}",
        type=node_type,
        name=name,
        provenance=Provenance(source="manifest", source_ref=f"{kind}:{name}", scanned_at=_now()),
        attributes={"db_id": db_id},
    )


def _manifest_graph() -> Graph:
    """Hand-built fallback graph: just the nodes `research_and_write.run()`
    touches, resolved by `db_id` the same way a real event would be."""
    if not MANIFEST_PATH.exists():
        raise SystemExit(
            f"{MANIFEST_PATH} not found -- run "
            "'uv run python -m simulator.build_manifest --uri mongodb://localhost:27017 --db mock_agent' "
            "from mock-agent-project first."
        )
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


async def _run_server(app: FastAPI) -> tuple[int, asyncio.Task[None], uvicorn.Server]:
    """Binds a loopback socket and serves `app` on it in-process -- same
    "bind first, hand uvicorn the socket" pattern as `otlp_http.py`'s
    receiver, so a port clash here fails with a clear error rather than
    uvicorn's own `sys.exit`."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.listen(16)
    sock.setblocking(False)

    config = uvicorn.Config(app, log_config=None, access_log=False, lifespan="on")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started and not task.done():
        await asyncio.sleep(0.02)
    if task.done():
        task.result()  # re-raise whatever killed startup
    return port, task, server


async def _collect_ws_messages(
    port: int, started_at: list[float], messages: list[tuple[float, dict[str, Any]]], stop: asyncio.Event
) -> None:
    """Runs for the whole measured window; appends `(t_since_start, message)`
    for every delta this client receives. `started_at` is a 1-element list
    used as an out-param set right after the subprocess launches, so the
    FIRST message (the initial snapshot) can be timestamped relative to a
    `t0` that isn't known yet when this task starts."""
    async with websockets.connect(f"ws://127.0.0.1:{port}/ws/runtime") as ws:
        while not stop.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=0.05)
            except TimeoutError:
                continue
            t = time.monotonic() - started_at[0] if started_at[0] else 0.0
            messages.append((t, json.loads(raw)))


async def _launch_simulator_subprocess(*, log_path: Path, fail: bool) -> None:
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
    _stdout, stderr = await proc.communicate()
    if proc.returncode not in (0, 1):  # 1 is expected for fail=True's uncaught RuntimeError
        print(f"[diagnose] subprocess exited {proc.returncode}; stderr:\n{stderr.decode()}")


def _print_ws_messages(messages: list[tuple[float, dict[str, Any]]]) -> None:
    for t, message in messages:
        if message.get("type") != "delta":
            continue
        print(
            f"  t={t:+.3f}s  nodeId={message['nodeId']!r:50s} status={message['status']:8s} "
            f"runCount={message['runCount']} lastEndedAt={message.get('lastEndedAt')}"
        )


def _print_raw_trace_spans(log_path: Path) -> None:
    print("  -- raw trace file spans --")
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        print(f"  ts={event['ts']}  phase={event['phase']:5s} kind={event['kind']:8s} name={event['ref']['name']}")


async def _run_one(*, port: int, fail: bool, log_path: Path) -> None:
    print(f"\n=== run fail={fail} ===")
    log_path.write_bytes(b"")  # fresh file each run, same convention test fixtures use

    messages: list[tuple[float, dict[str, Any]]] = []
    started_at = [0.0]
    stop = asyncio.Event()
    collector = asyncio.create_task(_collect_ws_messages(port, started_at, messages, stop))
    await asyncio.sleep(0.1)  # let the collector's initial snapshot settle before t0

    started_at[0] = time.monotonic()
    await _launch_simulator_subprocess(log_path=log_path, fail=fail)

    await asyncio.sleep(0.3)  # catch the batch-flush window's trailing deltas
    stop.set()
    await collector

    _print_ws_messages(messages)
    _print_raw_trace_spans(log_path)


async def main() -> None:
    settings = Settings()
    app = create_app(settings)

    with tempfile.TemporaryDirectory(prefix="apv-diagnose-") as tmp_dir:
        log_path = Path(tmp_dir) / "live.jsonl"
        log_path.write_bytes(b"")

        port, serve_task, server = await _run_server(app)
        try:
            runtime_channel = app.state.runtime_channel
            graph = await _try_real_mongo_graph(app)
            graph_source = "real Mongo L1 scan"
            if graph is None:
                graph = _manifest_graph()
                graph_source = "manifest.json fallback"
                await runtime_channel.update_graph(graph)
            print(f"[diagnose] graph source: {graph_source} ({len(graph.nodes)} nodes)")

            await runtime_channel.apply_config(
                [MethodConfig(id="apv_live_file", settings=ApvLiveFileSettings(path=str(log_path)))]
            )

            await _run_one(port=port, fail=True, log_path=log_path)
            await _run_one(port=port, fail=False, log_path=log_path)
        finally:
            server.should_exit = True
            await serve_task


if __name__ == "__main__":
    sys.exit(asyncio.run(main()) or 0)

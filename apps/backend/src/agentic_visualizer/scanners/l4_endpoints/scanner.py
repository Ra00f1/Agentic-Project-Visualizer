"""`L4EndpointScanner` — orchestrates the per-framework extractors into endpoint/exposes graph elements.

**Why extractor-per-framework, not one giant walker.** Each framework has
its own canonical decorator shapes and prefix semantics; mixing them into
one function would make the code an `isinstance` tree keyed on framework.
The `EndpointExtractor` Protocol (`base.py`) gives every framework the
same shape; each of `fastapi.py`/`flask.py` owns only its own quirks.

**Why L4 emits its own function node for each handler, rather than
depending on L2/L3 having already created one.** `Graph.from_results`
dedupes nodes by id, first-writer-wins — so if L2/L3 already emitted a
richer node at the same `code:<file>:<line>:<qualname>` id (decorators,
docstring, `resolution_kind`), theirs wins over this scanner's minimal
version harmlessly. But L3 only emits a node for a function that has
*resolved outgoing calls* (a leaf function is "graph noise" by its own
design) — a simple route handler that just returns a literal has none,
so L3 alone would leave the `exposes` edge dangling for the common case.
Emitting the handler's own node here, keyed identically, means the edge
always resolves regardless of whether L2/L3 ran or what a handler's body
looks like — strictly more robust than depending on upstream output L4
doesn't otherwise need (no `ScanContext`/`upstream` coupling to L1/L2/L3,
matching L3's own "walks the codebase independently" shape).

**Why dedupe by `(method, path, handler_qualname)`.** A route can be
registered more than once (`@app.get("/x")` and
`@app.api_route("/x", methods=["GET"])` on different decorators of the
same handler) — same endpoint, same handler, same effect, one node.

**Why dedup needs a tiebreak, not just "first extractor wins."** FastAPI
and Flask share more decorator-attribute names than their own docs
suggest: Flask's primary `.route(...)` is also a recognized Starlette/
FastAPI attribute, and modern Flask also offers `.get(...)`/`.post(...)`
shortcuts — found by running L4 against the real fixture and seeing a
Flask blueprint route attributed to `framework: "fastapi"` with a bogus
ambiguity warning, not from reading the two extractors' decorator sets
side by side. `FastAPIExtractor` has no way to know `admin_bp` is a
Flask `Blueprint`, so it reports the match as ambiguous (correctly, by
its own lights) while `FlaskExtractor` confirms the SAME decorator
confidently. Both candidates share the same `(method, path,
handler_qualname)` key; keeping "whichever extractor happened to run
first" would keep the wrong one whenever `_DEFAULT_EXTRACTORS`' order
and a decorator's true framework disagree — so `scan()` collects every
candidate across every extractor BEFORE emitting, and `_is_better` lets
a later, confirmed match replace an earlier, merely-ambiguous one.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime

from agentic_visualizer.codebase.base import CodebaseAdapter
from agentic_visualizer.domain import Edge, Node, Provenance, ScanError, ScanResult
from agentic_visualizer.scanners.base import ScanContext

from .base import Endpoint, EndpointExtractor
from .fastapi import FastAPIExtractor
from .flask import FlaskExtractor

_DEFAULT_EXTRACTORS: tuple[EndpointExtractor, ...] = (FastAPIExtractor(), FlaskExtractor())


@dataclasses.dataclass(slots=True, frozen=True)
class L4EndpointScanner:
    """Walk the codebase, run every extractor over every file, emit
    `endpoint` nodes, the handler's own `function` node, and `exposes`
    edges between them.
    """

    codebase: CodebaseAdapter
    name: str = "l4_endpoint"
    extractors: tuple[EndpointExtractor, ...] = _DEFAULT_EXTRACTORS

    async def scan(self, context: ScanContext) -> ScanResult:
        scanned_at = context.now()
        nodes: list[Node] = []
        edges: list[Edge] = []
        errors: list[ScanError] = []
        # (endpoint, file, framework) for every candidate any extractor
        # found, across every file -- collected in full before any
        # dedup/filtering decision, because that decision needs to see
        # ACROSS extractors (an ambiguous match from one needs to know
        # whether another already confirmed the same handler).
        candidates: list[tuple[Endpoint, str, str]] = []

        for py_file in self.codebase.list_files("**/*.py"):
            try:
                source = self.codebase.read_file(py_file)
            except (OSError, UnicodeDecodeError):
                continue  # unreadable file -- same tolerance L3 has for a half-edited project

            rel = py_file.relative_to(self.codebase.root).as_posix()
            module_dotted = _path_to_module_dotted(rel)

            for extractor in self.extractors:
                try:
                    found = await extractor.extract(py_file, source)
                except SyntaxError as exc:
                    errors.append(
                        ScanError(
                            scanner=self.name,
                            source_ref=rel,
                            message=f"could not parse {rel} for {extractor.framework} routes: {exc}",
                        )
                    )
                    continue

                for raw_endpoint in found:
                    endpoint = dataclasses.replace(raw_endpoint, handler_module=module_dotted)
                    candidates.append((endpoint, rel, extractor.framework))

        # A confirmed match for a given (file, handler) means every
        # AMBIGUOUS candidate for that SAME handler is a cross-framework
        # attribute-name collision (Flask's `.route`/`.get` also matches
        # FastAPI's recognized set -- see the module docstring), not a
        # second real route. Drop those before deduping by path, because
        # the ambiguous and confirmed candidates usually carry DIFFERENT
        # paths (the ambiguous extractor doesn't know the other
        # framework's prefix), so they'd never collide on dedup's own key.
        confirmed_handlers = {(rel, ep.handler_qualname) for ep, rel, _ in candidates if ep.ambiguous_reason is None}
        kept_candidates = [
            (ep, rel, framework)
            for ep, rel, framework in candidates
            if ep.ambiguous_reason is None or (rel, ep.handler_qualname) not in confirmed_handlers
        ]

        # key -> the best candidate seen so far for it, plus which file/
        # framework it came from (needed at emission time, but not part
        # of the dedup key itself). See the module docstring for why a
        # later, confirmed match is allowed to replace an earlier,
        # merely-ambiguous one at the same key.
        winners: dict[tuple[str, str, str], tuple[Endpoint, str, str]] = {}
        for endpoint, rel, framework in kept_candidates:
            key = (endpoint.method, endpoint.path, endpoint.handler_qualname)
            existing = winners.get(key)
            if existing is None or _is_better_match(endpoint, existing[0]):
                winners[key] = (endpoint, rel, framework)

        for endpoint, rel, framework in winners.values():
            self._emit_endpoint(
                endpoint=endpoint,
                rel=rel,
                framework=framework,
                scanned_at=scanned_at,
                nodes=nodes,
                edges=edges,
                errors=errors,
            )

        return ScanResult(scanner=self.name, nodes=nodes, edges=edges, errors=errors)

    def _emit_endpoint(
        self,
        *,
        endpoint: Endpoint,
        rel: str,
        framework: str,
        scanned_at: datetime,
        nodes: list[Node],
        edges: list[Edge],
        errors: list[ScanError],
    ) -> None:
        function_id = f"code:{rel}:{endpoint.source_line}:{endpoint.handler_qualname}"
        endpoint_id = f"code:{rel}:{endpoint.source_line}:endpoint:{endpoint.method}:{endpoint.path}"
        source_ref = f"{rel}:{endpoint.source_line}:{endpoint.method} {endpoint.path}"
        prov = Provenance(source=self.name, source_ref=source_ref, scanned_at=scanned_at)

        nodes.append(
            Node(
                id=function_id,
                type="function",
                name=endpoint.handler_qualname,
                provenance=prov,
                attributes={"file_path": rel, "line": endpoint.source_line, "qualname": endpoint.handler_qualname},
            )
        )
        nodes.append(
            Node(
                id=endpoint_id,
                type="endpoint",
                name=f"{endpoint.method} {endpoint.path}",
                provenance=prov,
                attributes={
                    "method": endpoint.method,
                    "path": endpoint.path,
                    "framework": framework,
                    "source_file": rel,
                    "source_line": endpoint.source_line,
                },
            )
        )
        edges.append(Edge(source_id=endpoint_id, target_id=function_id, kind="exposes", provenance=prov))

        if endpoint.ambiguous_reason is not None:
            errors.append(ScanError(scanner=self.name, source_ref=source_ref, message=endpoint.ambiguous_reason))


def _is_better_match(candidate: Endpoint, existing: Endpoint) -> bool:
    """A confirmed match (`ambiguous_reason is None`) replaces an
    ambiguous one at the same `(method, path, handler_qualname)` key;
    otherwise the first one found keeps winning (arbitrary but
    harmless — same endpoint, same handler, same effect either way)."""
    return existing.ambiguous_reason is not None and candidate.ambiguous_reason is None


def _path_to_module_dotted(rel_posix: str) -> str:
    """Same transformation as `l3_calls._path_to_module_dotted` -- not
    imported from there since it's module-private; duplicated rather than
    exposed just for this one caller."""
    if rel_posix.endswith("/__init__.py"):
        rel_posix = rel_posix[: -len("/__init__.py")]
    elif rel_posix.endswith(".py"):
        rel_posix = rel_posix[: -len(".py")]
    return rel_posix.replace("/", ".")

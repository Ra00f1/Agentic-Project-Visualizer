"""`EndpointExtractor` Protocol + `Endpoint` — the per-framework contract L4 orchestrates.

Each extractor owns one framework's decorator shapes and prefix semantics
end to end (see `l4_endpoints/scanner.py`'s module docstring for why one
extractor per framework, not a shared `isinstance` tree). `extract` is a
pure function over one file's already-read source: no filesystem access,
no shared state across files — `L4EndpointScanner` owns the codebase walk.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(slots=True, frozen=True)
class Endpoint:
    """One HTTP route, as found in source — not yet a graph `Node`.

    `ambiguous_reason` is the one field beyond the Design's original five
    (`method`, `path`, `handler_qualname`, `handler_module`, `source_line`):
    it's how an extractor reports "found a route-shaped decorator but
    couldn't confirm the base object is actually a framework app/router"
    through this Protocol's return type, instead of a second side-channel
    `L4EndpointScanner` would have to thread through separately. `None`
    means confirmed.
    """

    method: str
    path: str
    handler_qualname: str
    handler_module: str
    source_line: int
    ambiguous_reason: str | None = None


@runtime_checkable
class EndpointExtractor(Protocol):
    """One framework's route-decorator recognizer.

    `framework` is a short, stable identifier (`"fastapi"`, `"flask"`) —
    becomes `Node.attributes["framework"]` on every endpoint it finds.

    Declared as a read-only `@property`, not a plain `framework: str`
    attribute: mypy treats a plain Protocol attribute as implicitly
    settable, which a frozen dataclass's field (every concrete
    implementation here) structurally can't satisfy — confirmed
    empirically, not assumed. Callers read `extractor.framework` either
    way; nothing about this changes at a call site.
    """

    @property
    def framework(self) -> str: ...

    async def extract(self, module_path: Path, source: str) -> list[Endpoint]:
        """Find every route in one file's source.

        Each extractor parses `source` itself (one `ast.parse` per
        extractor per file, not shared) — simpler than threading a
        pre-parsed tree through the Protocol, and L4's own file count is
        small enough that the extra parse is not worth the coupling.

        Never raises for a malformed decorator shape (unparseable
        `methods=`, an unresolved app/router) — those are either skipped
        or reported via `Endpoint.ambiguous_reason`, never an exception.
        A genuine `SyntaxError` parsing `source` itself IS allowed to
        raise; `L4EndpointScanner` catches it once per file, the same way
        every other scanner treats "can't parse this file" as a per-file
        `ScanError`, not a per-extractor one.
        """
        ...

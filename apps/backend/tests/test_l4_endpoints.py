"""Tests for L4: endpoint discovery (FastAPI + Flask).

Extractor-level tests feed hand-written source strings directly to
`FastAPIExtractor`/`FlaskExtractor` — each asserts one specific decorator
shape in isolation, independent of any fixture file. Orchestrator tests
run `L4EndpointScanner` against the real `mock-agent-project` fixture
(`src/api/routes.py`, `versioned_routes.py`, `flask_admin.py`, `main.py`)
end to end, the same split `test_l3_scanner.py` uses.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from agentic_visualizer.codebase import LocalCodebaseAdapter
from agentic_visualizer.scanners import L4EndpointScanner
from agentic_visualizer.scanners.l4_endpoints import FastAPIExtractor, FlaskExtractor

FIXTURE_ROOT = Path(__file__).parents[3] / "mock-agent-project"
_DUMMY_PATH = Path("module.py")


# ---------------------------------------------------------------------------
# FastAPIExtractor — isolated decorator shapes


@pytest.mark.asyncio
async def test_fastapi_get_and_post_on_app() -> None:
    source = (
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "\n"
        "@app.get('/')\n"
        "def root(): ...\n"
        "\n"
        "@app.post('/items')\n"
        "def create_item(): ...\n"
    )
    endpoints = await FastAPIExtractor().extract(_DUMMY_PATH, source)
    by_handler = {e.handler_qualname: e for e in endpoints}
    assert by_handler["root"].method == "GET"
    assert by_handler["root"].path == "/"
    assert by_handler["create_item"].method == "POST"
    assert by_handler["create_item"].path == "/items"
    assert all(e.ambiguous_reason is None for e in endpoints)


@pytest.mark.asyncio
async def test_fastapi_router_prefix_is_applied() -> None:
    source = (
        "from fastapi import APIRouter\n"
        "router = APIRouter(prefix='/api')\n"
        "\n"
        "@router.get('/documents')\n"
        "def list_documents(): ...\n"
    )
    endpoints = await FastAPIExtractor().extract(_DUMMY_PATH, source)
    assert len(endpoints) == 1
    assert endpoints[0].path == "/api/documents"


@pytest.mark.asyncio
async def test_fastapi_path_parameter_is_preserved_verbatim() -> None:
    source = (
        "from fastapi import APIRouter\n"
        "router = APIRouter(prefix='/api')\n"
        "\n"
        "@router.get('/documents/{doc_id}')\n"
        "def get_document(doc_id: str): ...\n"
    )
    endpoints = await FastAPIExtractor().extract(_DUMMY_PATH, source)
    assert endpoints[0].path == "/api/documents/{doc_id}"


@pytest.mark.asyncio
async def test_fastapi_api_route_with_methods_expands_to_one_per_method() -> None:
    source = (
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "\n"
        "@router.api_route('/multi', methods=['GET', 'POST'])\n"
        "def multi(): ...\n"
    )
    endpoints = await FastAPIExtractor().extract(_DUMMY_PATH, source)
    methods = {e.method for e in endpoints}
    assert methods == {"GET", "POST"}
    assert all(e.path == "/multi" and e.handler_qualname == "multi" for e in endpoints)


@pytest.mark.asyncio
async def test_fastapi_api_route_without_methods_defaults_to_get() -> None:
    source = (
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "\n"
        "@router.api_route('/default')\n"
        "def default_route(): ...\n"
    )
    endpoints = await FastAPIExtractor().extract(_DUMMY_PATH, source)
    assert [e.method for e in endpoints] == ["GET"]


@pytest.mark.asyncio
async def test_fastapi_api_route_with_malformed_methods_is_skipped() -> None:
    """`methods=SOME_VARIABLE` isn't a clean list of string constants --
    resolution failure for this one decorator, not a crash."""
    source = (
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "ALLOWED = ['GET']\n"
        "\n"
        "@router.api_route('/x', methods=ALLOWED)\n"
        "def x(): ...\n"
    )
    endpoints = await FastAPIExtractor().extract(_DUMMY_PATH, source)
    assert endpoints == []


@pytest.mark.asyncio
async def test_fastapi_unresolved_base_is_emitted_with_ambiguous_reason() -> None:
    """A decorator shaped like a route but on an unrecognized base object
    (not a tracked `FastAPI()`/`APIRouter()` assignment) is still emitted
    -- Design's own call: a false positive guarded by a visible warning,
    not a silent drop."""
    source = "@my_cache_decorator.get('/stats')\ndef stats(): ...\n"
    endpoints = await FastAPIExtractor().extract(_DUMMY_PATH, source)
    assert len(endpoints) == 1
    assert endpoints[0].path == "/stats"
    assert endpoints[0].ambiguous_reason is not None
    assert "my_cache_decorator" in endpoints[0].ambiguous_reason


@pytest.mark.asyncio
async def test_fastapi_non_route_decorator_is_ignored() -> None:
    source = (
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "\n"
        "@some_other_decorator\n"
        "def not_a_route(): ...\n"
    )
    endpoints = await FastAPIExtractor().extract(_DUMMY_PATH, source)
    assert endpoints == []


# ---------------------------------------------------------------------------
# FlaskExtractor — isolated decorator shapes


@pytest.mark.asyncio
async def test_flask_route_default_method_is_get() -> None:
    source = (
        "from flask import Flask\n"
        "app = Flask(__name__)\n"
        "\n"
        "@app.route('/ping')\n"
        "def ping(): ...\n"
    )
    endpoints = await FlaskExtractor().extract(_DUMMY_PATH, source)
    assert len(endpoints) == 1
    assert endpoints[0].method == "GET"
    assert endpoints[0].path == "/ping"


@pytest.mark.asyncio
async def test_flask_route_with_explicit_methods() -> None:
    source = (
        "from flask import Blueprint\n"
        "bp = Blueprint('x', __name__)\n"
        "\n"
        "@bp.route('/submit', methods=['POST', 'PUT'])\n"
        "def submit(): ...\n"
    )
    endpoints = await FlaskExtractor().extract(_DUMMY_PATH, source)
    assert {e.method for e in endpoints} == {"POST", "PUT"}


@pytest.mark.asyncio
async def test_flask_blueprint_url_prefix_is_applied() -> None:
    source = (
        "from flask import Blueprint\n"
        "admin_bp = Blueprint('admin', __name__, url_prefix='/admin')\n"
        "\n"
        "@admin_bp.route('/health', methods=['GET'])\n"
        "def health(): ...\n"
    )
    endpoints = await FlaskExtractor().extract(_DUMMY_PATH, source)
    assert endpoints[0].path == "/admin/health"


@pytest.mark.asyncio
async def test_flask_non_route_decorator_is_ignored() -> None:
    source = "@app.before_request\ndef setup(): ...\n"
    endpoints = await FlaskExtractor().extract(_DUMMY_PATH, source)
    assert endpoints == []


# ---------------------------------------------------------------------------
# L4EndpointScanner — orchestration unit tests (hand-built temp files)


@pytest_asyncio.fixture
async def scanner_with_tmp_root(tmp_path: Path, scan_context_factory):
    """Returns `(run, tmp_path)` where `run(source)` writes `source` to a
    file under `tmp_path` and returns the scan result for just that root."""
    adapter = LocalCodebaseAdapter(root=tmp_path)
    scanner = L4EndpointScanner(codebase=adapter)
    context = scan_context_factory(None)

    async def run():
        return await scanner.scan(context)

    return run, tmp_path


@pytest.mark.asyncio
async def test_dedupes_same_endpoint_registered_twice(scanner_with_tmp_root) -> None:
    run, tmp_path = scanner_with_tmp_root
    (tmp_path / "app.py").write_text(
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "\n"
        "@router.get('/x')\n"
        "@router.api_route('/x', methods=['GET'])\n"
        "def x(): ...\n"
    )
    result = await run()
    endpoint_nodes = [n for n in result.nodes if n.type == "endpoint"]
    assert len(endpoint_nodes) == 1


@pytest.mark.asyncio
async def test_emits_endpoint_function_node_and_exposes_edge(scanner_with_tmp_root) -> None:
    run, tmp_path = scanner_with_tmp_root
    (tmp_path / "app.py").write_text(
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "\n"
        "@app.get('/ping')\n"
        "def ping(): ...\n"
    )
    result = await run()

    endpoint_nodes = [n for n in result.nodes if n.type == "endpoint"]
    function_nodes = [n for n in result.nodes if n.type == "function"]
    exposes_edges = [e for e in result.edges if e.kind == "exposes"]
    assert len(endpoint_nodes) == 1
    assert len(function_nodes) == 1
    assert len(exposes_edges) == 1

    endpoint_node = endpoint_nodes[0]
    function_node = function_nodes[0]
    edge = exposes_edges[0]
    assert endpoint_node.attributes["method"] == "GET"
    assert endpoint_node.attributes["path"] == "/ping"
    assert endpoint_node.attributes["framework"] == "fastapi"
    assert endpoint_node.attributes["source_file"] == "app.py"
    assert function_node.attributes["qualname"] == "ping"
    assert function_node.attributes["file_path"] == "app.py"
    assert edge.source_id == endpoint_node.id
    assert edge.target_id == function_node.id
    assert endpoint_node.id == f"code:app.py:{endpoint_node.attributes['source_line']}:endpoint:GET:/ping"
    assert function_node.id == f"code:app.py:{endpoint_node.attributes['source_line']}:ping"


@pytest.mark.asyncio
async def test_ambiguous_endpoint_produces_a_scan_error(scanner_with_tmp_root) -> None:
    run, tmp_path = scanner_with_tmp_root
    (tmp_path / "app.py").write_text("@unknown_thing.get('/x')\ndef x(): ...\n")
    result = await run()
    assert any(e.scanner == "l4_endpoint" for e in result.errors)
    assert len(result.nodes) == 2  # endpoint + function node still emitted


@pytest.mark.asyncio
async def test_unparseable_file_produces_a_scan_error_not_a_crash(scanner_with_tmp_root) -> None:
    run, tmp_path = scanner_with_tmp_root
    (tmp_path / "broken.py").write_text("def broken(:\n")
    result = await run()
    assert result.nodes == []
    assert any("broken.py" in e.source_ref for e in result.errors)


# ---------------------------------------------------------------------------
# L4EndpointScanner — integration against the real mock-agent-project fixture


@pytest.fixture
def adapter() -> LocalCodebaseAdapter:
    assert FIXTURE_ROOT.exists(), f"fixture missing: {FIXTURE_ROOT}"
    return LocalCodebaseAdapter(root=FIXTURE_ROOT)


@pytest_asyncio.fixture
async def l4_result(adapter, scan_context_factory):
    scanner = L4EndpointScanner(codebase=adapter)
    return await scanner.scan(scan_context_factory(None))


@pytest.mark.asyncio
async def test_fixture_fastapi_routes_are_discovered(l4_result) -> None:
    endpoint_nodes = [n for n in l4_result.nodes if n.type == "endpoint"]
    found = {(n.attributes["method"], n.attributes["path"]) for n in endpoint_nodes}
    assert ("GET", "/") in found
    assert ("GET", "/version") in found
    assert ("GET", "/api/documents") in found
    assert ("GET", "/api/documents/{doc_id}") in found
    assert ("POST", "/api/agents/run") in found
    assert ("GET", "/api/documents/by-tag/{tag}") in found


@pytest.mark.asyncio
async def test_fixture_versioned_routers_get_distinct_prefixes(l4_result) -> None:
    endpoint_nodes = [n for n in l4_result.nodes if n.type == "endpoint"]
    found = {(n.attributes["method"], n.attributes["path"]) for n in endpoint_nodes}
    assert ("GET", "/v1/users/{user_id}/orders") in found
    assert ("GET", "/v2/users/{user_id}/orders") in found
    assert ("POST", "/v1/orders") in found
    assert ("POST", "/v2/orders") in found


@pytest.mark.asyncio
async def test_fixture_flask_blueprint_routes_are_discovered(l4_result) -> None:
    """Verifies the EndpointExtractor interface is genuinely
    framework-agnostic -- the failure mode the fixture's own docstring
    warns about (routes invisible if the orchestrator only understands
    FastAPI's attribute names)."""
    endpoint_nodes = [n for n in l4_result.nodes if n.type == "endpoint"]
    found = {(n.attributes["method"], n.attributes["path"], n.attributes["framework"]) for n in endpoint_nodes}
    assert ("GET", "/admin/health", "flask") in found
    assert ("POST", "/admin/cleanup", "flask") in found


@pytest.mark.asyncio
async def test_fixture_every_endpoint_exposes_a_resolvable_function(l4_result) -> None:
    function_ids = {n.id for n in l4_result.nodes if n.type == "function"}
    exposes_edges = [e for e in l4_result.edges if e.kind == "exposes"]
    assert exposes_edges, "expected at least one exposes edge"
    for edge in exposes_edges:
        assert edge.target_id in function_ids, edge


@pytest.mark.asyncio
async def test_fixture_root_endpoint_is_attributed_to_main_not_routes(l4_result) -> None:
    """main.py's own docstring: `app.include_router(router)` must not
    make `root`'s handler look like it lives in routes.py."""
    endpoint_nodes = [n for n in l4_result.nodes if n.type == "endpoint"]
    root_endpoint = next(n for n in endpoint_nodes if n.attributes["path"] == "/" and n.attributes["method"] == "GET")
    assert root_endpoint.attributes["source_file"] == "src/main.py"

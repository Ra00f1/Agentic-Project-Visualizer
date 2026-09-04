"""L1 — Entity ingestion.

Reads user-selected collections from the connected DB and produces one node
per document plus edges for the foreign-key-like references between them.

**Scope, deliberately narrow (see CLAUDE.md §6, L1 layer):**

* We emit nodes for workflows, agents, models, tools, prompts, users, files.
* We emit edges for the RELATIONSHIPS that matter architecturally —
  workflow→agent, agent→model, agent→tool, agent→prompt, agent→sub_agent,
  workflow→file, user→prompt, user→file.
* We do **not** emit edges for audit-trail fields (`created_by`, `updated_by`)
  even though they're present in the fixture. Reason: if we did, users would
  become massive central hubs on the graph and the actual architecture would
  be visually drowned. Those fields are still preserved on the node's
  `attributes` so the detail panel can show them.

**Collection→node-type mapping is hardcoded for v1.** The real product will
let users map their own tables via the UI (see CLAUDE.md §6, "user maps in
the UI"). To make that easy to add later, the mapping lives in one
`CollectionMapping` object rather than being sprinkled through the scanner
body — swap the object, get a differently-shaped graph.

**One deliberate v1 limitation:** we don't dedupe across scanners here; the
`Graph.from_results` merger handles that. So if two rows accidentally hash to
the same node id, both emitted nodes will collide at merge time and the
first-writer-wins rule kicks in. That's the right layer for that decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, TypedDict

from bson import ObjectId  # comes with pymongo, which motor depends on

from ..domain import Edge, EdgeKind, Node, NodeType, Provenance, ScanError, ScanResult
from .base import ScanContext


# Collections whose documents we do NOT read during L1. Instead, if they are
# in the mapping, we run a cheap $group aggregation per foreign key to get
# counts per parent and attach `<collection>_count` attributes to the parent
# nodes. The individual documents are fetched on demand via GET
# /graph/subtree when the user clicks the "Files (N)" aggregator in the UI.
#
# Each lazy collection is mapped to a LIST of `(foreign_key, parent_collection)`
# tuples — a single lazy collection may attach to more than one parent type
# (files can hang off a workflow OR an agent), and each parent type gets
# enriched with its own count.
class LazySpec(TypedDict):
    """How a lazy collection enriches its parents.

    `attr_name` is the attribute added to parent nodes. Kept explicit
    (rather than derived from the collection name) because English plurals
    don't singularize cleanly with a rule — and because a mismatch here
    quietly breaks the UI (attribute-based aggregator discovery fails
    silently and the aggregator just doesn't appear).

    `parents` is the list of (foreign_key_field, parent_collection) tuples
    — a lazy collection may attach to more than one parent type (files
    can hang off a workflow OR an agent).
    """

    attr_name: str
    parents: list[tuple[str, str]]


LAZY_COLLECTIONS: dict[str, LazySpec] = {
    "files": {
        "attr_name": "file_count",
        "parents": [
            ("workflow_id", "workflows"),
            ("agent_id", "agents"),
        ],
    },
}


def _singularize(name: str) -> str:
    """Best-effort singular form of a Mongo collection name.

    Convention across our fixture (and most real projects): plural
    collection name (`files`, `workflows`, `agents`), singular foreign-key
    prefix (`file_id`, `workflow_id`, `agent_id`). Stripping trailing
    's' handles that. Doesn't handle irregular plurals — a collection
    named `people` would fall through as-is; we accept that until it
    hurts us.
    """
    return name[:-1] if name.endswith("s") and len(name) > 1 else name


async def _discover_lazy_collections(
    mapping: CollectionMapping,
    connector: object,
) -> dict[str, LazySpec]:
    """Merge hardcoded LAZY_COLLECTIONS with schema-based auto-detection.

    A collection is treated as lazy if it has a field named
    `<parent_singular>_id` and the parent collection is also in the
    mapping. This lets a user rename `files` to `documents`,
    `attachments`, or anything else — as long as the foreign keys follow
    the standard `<parent>_id` convention, we detect them without any
    config.

    Hardcoded LAZY_COLLECTIONS wins over auto-detection for a given
    collection name (so we can override attr_name / parents explicitly
    when needed, or force a specific collection to be lazy even when
    schema introspection would miss it).

    The connector is duck-typed on `introspect_schema` — auto-detection
    is best-effort. If schema fetch fails or the method is missing, we
    silently fall back to only the hardcoded entries.
    """
    effective: dict[str, LazySpec] = dict(LAZY_COLLECTIONS)
    introspect = getattr(connector, "introspect_schema", None)
    if introspect is None:
        return effective
    try:
        schemas = await introspect()
    except Exception:  # noqa: BLE001 — best-effort discovery
        return effective
    fields_by_coll: dict[str, set[str]] = {
        s.name: set(s.field_names) for s in schemas
    }

    # Precompute which collections are referenced by others via the standard
    # naming convention `<self_singular>_id` or `<self_singular>_ids`.
    # A collection that IS referenced is a first-class entity (agents,
    # models, tools, workflows) — it must NOT be treated as lazy even if
    # it has its own outbound foreign keys. Only true leaves (files,
    # attachments, comments — collections nothing else points at) qualify.
    #
    # Without this guard, the auto-detector treats `agents` as lazy for
    # `models` because agents have a `model_id` field, and then agent
    # documents are skipped in the eager scan — resulting in dangling
    # `workflow.agent_ids` edges and no visible agents in the UI. That
    # exact bug is what this check prevents.
    referenced: set[str] = set()
    for coll_name in mapping:
        fields = fields_by_coll.get(coll_name, set())
        for other in mapping:
            if other == coll_name:
                continue
            singular_other = _singularize(other)
            if (
                f"{singular_other}_id" in fields
                or f"{singular_other}_ids" in fields
            ):
                referenced.add(other)

    for coll_name in mapping:
        if coll_name in effective:
            continue  # hardcoded already; don't overwrite attr_name
        if coll_name in referenced:
            continue  # first-class entity — keep eager
        fields = fields_by_coll.get(coll_name, set())
        parents: list[tuple[str, str]] = []
        for other in mapping:
            if other == coll_name:
                continue
            fk = f"{_singularize(other)}_id"
            if fk in fields:
                parents.append((fk, other))
        if parents:
            effective[coll_name] = {
                "attr_name": f"{_singularize(coll_name)}_count",
                "parents": parents,
            }
    return effective


# --- Mapping from Mongo collection name → semantics ------------------------


@dataclass(slots=True, frozen=True)
class CollectionSpec:
    """How the L1 scanner treats one collection.

    `node_type`  — every doc in the collection becomes a node of this type.
    `name_field` — which doc field holds the human-readable name.
    `refs`       — foreign-key-like fields. Each entry says:
                   "field X on this doc points to collection Y with edge kind Z".
                   Values can be a single ObjectId or a list of ObjectIds; the
                   scanner handles both without configuration.
    """

    node_type: NodeType
    name_field: str
    refs: tuple["RefSpec", ...] = ()


@dataclass(slots=True, frozen=True)
class RefSpec:
    """One foreign-key-like edge from this collection to another."""

    field: str
    """Doc field name holding an ObjectId or a list of ObjectIds."""

    target_collection: str
    """The collection the ObjectId(s) resolve into."""

    kind: EdgeKind
    """Edge kind to emit. Must be one of the L1-vocabulary kinds."""


CollectionMapping = Mapping[str, CollectionSpec]

# Baked-in mapping for our mock fixture. Documented here so future-us doesn't
# have to grep for "why does the scanner know about `agents`?"
DEFAULT_COLLECTION_MAPPING: CollectionMapping = {
    # NOTE (2026-09-XX): users and prompts intentionally dropped from this
    # mapping for the current UX iteration. The UI shows a progressive
    # disclosure starting from workflows (workflow → agents+models →
    # tools+models), and users/prompts aren't reachable from that flow yet.
    # Adding them back is a one-line restore per entry — keep the shape.
    "models": CollectionSpec(node_type="model", name_field="name"),
    "tools": CollectionSpec(node_type="tool", name_field="name"),
    "agents": CollectionSpec(
        node_type="agent",
        name_field="display_name",
        refs=(
            RefSpec(field="model_id", target_collection="models", kind="uses"),
            RefSpec(field="tool_ids", target_collection="tools", kind="uses"),
            RefSpec(field="sub_agent_ids", target_collection="agents", kind="delegates_to"),
        ),
    ),
    "workflows": CollectionSpec(
        node_type="workflow",
        name_field="display_name",
        refs=(
            RefSpec(field="entrypoint_agent_id", target_collection="agents", kind="uses"),
            RefSpec(field="agent_ids", target_collection="agents", kind="uses"),
            # NEW: workflows can directly reference models too (not just via
            # agents). Fixture updated to give workflow #1 two model_ids.
            RefSpec(field="model_ids", target_collection="models", kind="uses"),
        ),
    ),
    "files": CollectionSpec(
        node_type="file",
        name_field="name",
        refs=(
            # workflow_id: reverse-direction edge. The FIELD is on the file
            # but semantically the workflow *contains* the file, so we invert
            # source/target when emitting. Handled explicitly in `_iter_refs`.
            RefSpec(field="workflow_id", target_collection="workflows", kind="contains"),
        ),
    ),
}

# Ref specs whose semantic direction is "target → source" rather than
# "source → target" (i.e. we invert when emitting the edge). Kept as a set
# of `(collection, field)` pairs so the mapping stays declarative above and
# the inversion logic is one lookup here.
_INVERTED_REFS: frozenset[tuple[str, str]] = frozenset(
    {
        ("files", "workflow_id"),
    }
)


# --- Node ID helpers -------------------------------------------------------


def _node_id(db_name: str, collection: str, oid: ObjectId | str) -> str:
    """Produce the namespaced node id for a Mongo doc.

    Format: `mongo:<db>.<collection>:<oid>`. Includes the db name so the
    same visualizer instance can serve graphs from multiple DBs without id
    collisions (a possibility once we add tabbed graphs).
    """
    return f"mongo:{db_name}.{collection}:{oid}"


def _source_ref(db_name: str, collection: str, oid: ObjectId | str) -> str:
    """Provenance source_ref — matches Provenance.source_ref docstring format."""
    return f"{db_name}.{collection}:{oid}"


# --- The scanner -----------------------------------------------------------


class L1EntityScanner:
    """Reads mapped collections and produces entity nodes + reference edges."""

    name = "l1_entity"

    def __init__(
        self,
        db_name: str,
        mapping: CollectionMapping = DEFAULT_COLLECTION_MAPPING,
    ) -> None:
        """
        Args:
            db_name: The DB name — used to construct provenance source_refs
                and namespace node ids. Passed in rather than pulled from the
                connector because the Connector Protocol deliberately doesn't
                expose it (not every connector has a "db name" concept).
            mapping: Which collections to scan and how. Defaults to the
                hardcoded mapping for our mock fixture; the endpoint layer
                will accept a user-supplied mapping in a later slice.
        """
        self._db_name = db_name
        self._mapping = mapping

    async def _count_lazy_by_parent(
        self,
        *,
        context: ScanContext,
        collection: str,
        foreign_key: str,
    ) -> dict[str, int]:
        """Ask the connector for `{parent_oid_str: count}` for a lazy collection.

        Isolated as a method so a future non-Mongo connector can override
        the behavior via a Protocol member without touching the scanner —
        for v1, only MongoConnector implements `count_by_field` (the
        Connector Protocol will gain it when we add the second connector).
        The type-ignore below acknowledges that until then.
        """
        connector = context.connector
        # Duck-typed for now; will graduate to Protocol later.
        count_by_field = getattr(connector, "count_by_field", None)
        if count_by_field is None:
            return {}
        return await count_by_field(collection, foreign_key)  # type: ignore[no-any-return]

    async def scan(self, context: ScanContext) -> ScanResult:
        scanned_at = context.now()
        nodes: list[Node] = []
        edges: list[Edge] = []
        errors: list[ScanError] = []

        # For the "user picked a subset of collections" case (Slice A), any
        # ref pointing to a collection we're NOT scanning becomes an
        # unresolved edge. We aggregate counts and emit one ScanError per
        # (source_collection, field, target_collection) triple at the end
        # of the scan — rather than one error per doc, which would flood
        # the warnings banner.
        scanned_collections = set(self._mapping.keys())
        unresolved_ref_counts: dict[tuple[str, str, str], int] = {}

        # Compute the effective set of lazy collections: hardcoded ones
        # PLUS anything auto-detected from the actual schema. This is
        # done once per scan so a user's collection named `documents`
        # (with `workflow_id` / `agent_id` fields) automatically becomes
        # lazy without any config.
        effective_lazy = await _discover_lazy_collections(
            self._mapping, context.connector
        )

        # Lazy collections still count as "scanned" for ref-resolution
        # purposes (so a workflow→file contains ref doesn't produce an
        # "unscanned" warning), but their documents are NOT streamed here.
        # Handled in the post-scan enrichment step below.
        for collection_name, spec in self._mapping.items():
            if collection_name in effective_lazy:
                continue
            async for doc in context.connector.stream_rows(collection_name):
                doc_id = doc.get("_id")
                if doc_id is None:
                    errors.append(
                        ScanError(
                            scanner=self.name,
                            source_ref=f"{self._db_name}.{collection_name}:<no _id>",
                            message=f"Document in {collection_name!r} has no _id; skipping.",
                        )
                    )
                    continue

                provenance = Provenance(
                    source=self.name,
                    source_ref=_source_ref(self._db_name, collection_name, doc_id),
                    scanned_at=scanned_at,
                )
                node_id = _node_id(self._db_name, collection_name, doc_id)

                # Name resolution: prefer the configured field, fall back to
                # `name`, fall back to the id. Missing names shouldn't hide
                # a node from the graph — they should show up so the user
                # notices the data-quality problem.
                name = doc.get(spec.name_field) or doc.get("name") or str(doc_id)
                if not doc.get(spec.name_field):
                    errors.append(
                        ScanError(
                            scanner=self.name,
                            source_ref=provenance.source_ref,
                            message=(
                                f"Missing name field {spec.name_field!r} on "
                                f"{collection_name!r} doc; used fallback."
                            ),
                        )
                    )

                nodes.append(
                    Node(
                        id=node_id,
                        type=spec.node_type,
                        name=str(name),
                        provenance=provenance,
                        attributes=_sanitize_attributes(doc),
                    )
                )

                # Emit edges for every ref this doc carries.
                for ref in spec.refs:
                    # Ref target isn't in the scanned mapping — record the
                    # unresolved count and skip. We could still emit dangling
                    # edges pointing at nodes that don't exist, but that
                    # would just produce visually broken lines in the UI.
                    if ref.target_collection not in scanned_collections:
                        values = list(_iter_ref_values(doc.get(ref.field)))
                        if values:
                            key = (collection_name, ref.field, ref.target_collection)
                            unresolved_ref_counts[key] = (
                                unresolved_ref_counts.get(key, 0) + len(values)
                            )
                        continue

                    for target_oid in _iter_ref_values(doc.get(ref.field)):
                        target_id = _node_id(self._db_name, ref.target_collection, target_oid)
                        inverted = (collection_name, ref.field) in _INVERTED_REFS
                        source_id, dest_id = (
                            (target_id, node_id) if inverted else (node_id, target_id)
                        )
                        edges.append(
                            Edge(
                                source_id=source_id,
                                target_id=dest_id,
                                kind=ref.kind,
                                provenance=provenance,
                                attributes={"via_field": ref.field},
                            )
                        )

        # --- Lazy-collection enrichment (Slice B2) --------------------------
        # For every (lazy_collection, foreign_key, parent_collection) triple
        # in LAZY_COLLECTIONS, run one $group aggregation to get counts per
        # parent, then rewrite the affected parent nodes with a
        # `<lazy>_count` attribute. We rebuild the node list because Node is
        # frozen — model_copy(update=...) returns a new instance rather than
        # mutating in place. That's the enforced cost of the immutability
        # contract.
        #
        # A single lazy collection can enrich multiple parent types (files
        # under both workflows and agents), so we iterate the (fk, parent)
        # list per lazy collection.
        for lazy_coll, lazy_spec in effective_lazy.items():
            if lazy_coll not in self._mapping:
                continue
            attr_name = lazy_spec["attr_name"]
            for fk_field, parent_coll in lazy_spec["parents"]:
                counts = await self._count_lazy_by_parent(
                    context=context,
                    collection=lazy_coll,
                    foreign_key=fk_field,
                )
                rewritten: list[Node] = []
                for n in nodes:
                    if not n.provenance.source_ref.startswith(
                        f"{self._db_name}.{parent_coll}:"
                    ):
                        rewritten.append(n)
                        continue
                    parent_oid = n.provenance.source_ref.split(":", 1)[1].split(":")[-1]
                    count = counts.get(parent_oid, 0)
                    # `+=` when the attribute already exists — a workflow AND
                    # an agent could each contribute to the same file_count
                    # (but in practice never for the same node). Explicit
                    # add keeps the semantics honest.
                    prev = int(n.attributes.get(attr_name, 0))
                    new_attrs = {**n.attributes, attr_name: prev + count}
                    rewritten.append(n.model_copy(update={"attributes": new_attrs}))
                nodes = rewritten

        # Aggregate unresolved-collection refs into one ScanError per triple.
        for (col, field, target), count in unresolved_ref_counts.items():
            errors.append(
                ScanError(
                    scanner=self.name,
                    source_ref=f"{self._db_name}.{col}.{field}",
                    message=(
                        f"{count} edge(s) to unscanned collection {target!r} skipped. "
                        f"Add {target!r} to the scanned collections to include them."
                    ),
                )
            )

        return ScanResult(scanner=self.name, nodes=nodes, edges=edges, errors=errors)


# --- Helpers ---------------------------------------------------------------


def _iter_ref_values(value: Any) -> Iterable[ObjectId | str]:
    """Yield each ObjectId in a ref field.

    Handles the three shapes a Mongo ref field can take:
      * A single ObjectId (e.g. `model_id`).
      * A list of ObjectIds (e.g. `tool_ids`).
      * `None` (unset reference — yield nothing, no error).

    Anything else (a string, a dict) is coerced to string and yielded as-is,
    so a broken fixture doesn't crash the scanner — it just produces a
    dangling edge the merger surfaces.
    """
    if value is None:
        return
    if isinstance(value, list):
        for v in value:
            if v is None:
                continue
            yield v if isinstance(v, ObjectId) else str(v)
        return
    yield value if isinstance(value, ObjectId) else str(value)


def _sanitize_attributes(doc: dict[str, Any]) -> dict[str, Any]:
    """Copy `doc` into a JSON-serializable dict for the Node.attributes payload.

    `ObjectId` and `datetime` are the two BSON types Pydantic v2 will serialize
    fine (datetime is native; ObjectId gets stringified on `.model_dump_json()`
    via a validator we could add later). For now we stringify ObjectIds
    proactively so the wire format is predictable and doesn't rely on Pydantic
    internals.

    We DROP `_id` from attributes because it's already encoded into `Node.id`
    and echoing it duplicates data in the detail panel.
    """
    out: dict[str, Any] = {}
    for key, value in doc.items():
        if key == "_id":
            continue
        out[key] = _coerce(value)
    return out


def _coerce(value: Any) -> Any:
    """Recursively stringify ObjectIds; leave everything else alone."""
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, list):
        return [_coerce(v) for v in value]
    if isinstance(value, dict):
        return {k: _coerce(v) for k, v in value.items()}
    return value

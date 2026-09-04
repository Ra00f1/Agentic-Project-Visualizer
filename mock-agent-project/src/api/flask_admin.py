"""Flask blueprint — proves the EndpointExtractor interface is framework-agnostic.

If the extractor only understands `@router.get`/`@app.get`, the endpoints in
this file will be invisible in the graph. That's the failure mode we're guarding
against — v1 has to at least recognize `@blueprint.route(...)`.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from ..workers.background import scheduled_cleanup


admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


@admin_bp.route("/health", methods=["GET"])
def health() -> tuple[dict, int]:
    """Simple health probe."""
    return {"status": "ok"}, 200


@admin_bp.route("/cleanup", methods=["POST"])
def trigger_cleanup() -> tuple[dict, int]:
    """Trigger the background cleanup worker synchronously (mock)."""
    dry_run = request.args.get("dry_run", "true").lower() == "true"
    stats = scheduled_cleanup(dry_run=dry_run)
    return jsonify(stats), 200

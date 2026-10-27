"""Data Extraction Pipeline — mirrors mock_agent's `data_extraction_pipeline` workflow."""

from __future__ import annotations

from apv_trace import trace

from . import simulate_agent

WORKFLOW_NAME = "Data Extraction Pipeline"


@trace(kind="workflow", name=WORKFLOW_NAME)
async def run(fail: bool = False) -> None:
    await simulate_agent("Storage Coordinator", tools=["shared_audit_log"], duration=0.4)
    await simulate_agent("Storage Specialist 1", tools=["upload_to_s3", "download_from_s3", "list_bucket", "shared_audit_log"], duration=0.8)
    await simulate_agent("Storage Specialist 2", tools=["download_from_s3", "list_bucket", "kv_get", "shared_audit_log"], duration=0.7)
    await simulate_agent("Storage Specialist 3", tools=["list_bucket", "kv_get", "shared_audit_log"], duration=0.6, fail=fail)

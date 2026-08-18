from pathlib import Path

from celery import shared_task
from django.conf import settings

from apps.findings.engine import run_all_rules

from .services import ingest_tree, ingestion_lock


@shared_task
def ingest_configured_logs() -> dict:
    # A slow first backfill may exceed the Beat interval. Serialize the whole
    # tree job so queued ticks do not split sources and run rules on partial data.
    with ingestion_lock(Path(settings.MCP_LOG_ROOT)) as acquired:
        if not acquired:
            return {"status": "locked", "files": 0, "events": 0, "findings": 0}
        results = ingest_tree(settings.MCP_LOG_ROOT)
        findings = run_all_rules()
        return {"status": "ok", "files": len(results), "events": sum(int(item["ingested"]) for item in results), "findings": findings}

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.findings.engine import run_all_rules
from apps.ingestion.services import ingest_file, ingest_tree


class Command(BaseCommand):
    help = "JSONL telemetry dosyalarını idempotent olarak içeri alır."

    def add_arguments(self, parser):
        parser.add_argument("paths", nargs="*", help="Dosya yolları; boşsa MCP_LOG_ROOT taranır")
        parser.add_argument("--skip-findings", action="store_true")

    def handle(self, *args, **options):
        paths = options["paths"]
        results = [ingest_file(Path(path)) for path in paths] if paths else ingest_tree(settings.MCP_LOG_ROOT)
        for result in results:
            self.stdout.write(f"{result['status']}: +{result['ingested']} event, {result['quarantined']} quarantine — {result['path']}")
        if not options["skip_findings"]:
            self.stdout.write(self.style.SUCCESS(f"{run_all_rules()} yeni finding üretildi."))


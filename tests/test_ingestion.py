import json
from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import TestCase

from apps.findings.engine import run_all_rules
from apps.findings.models import Finding
from apps.ingestion.models import QuarantinedEvent, RawEvent
from apps.ingestion.services import digest, ingest_file
from apps.traces.models import ExternalCall, ModelStep, Session, Span, ToolCall, Turn


class IngestionTests(TestCase):
    def write_jsonl(self, directory: str, name: str, rows: list) -> Path:
        path = Path(directory) / name
        with path.open("w", encoding="utf-8") as stream:
            for row in rows:
                if isinstance(row, str):
                    stream.write(row + "\n")
                else:
                    stream.write(json.dumps(row) + "\n")
        return path

    def test_agent_turn_is_idempotent_and_normalized(self):
        payload = {
            "timestamp": "2026-08-14T10:00:01Z", "session_id": "agent-1",
            "provider": "anthropic", "model": "claude-test", "question": "List workflows",
            "answer": "Done", "duration_ms": 1000, "input_tokens": 10,
            "output_tokens": 5, "cost_usd": 0.001,
            "tool_calls": [{"name": "list_workflows", "arguments": {}, "result": "{\"workflows\": []}", "duration_ms": 100}],
        }
        with TemporaryDirectory() as directory:
            path = self.write_jsonl(directory, "turns.jsonl", [payload])
            first = ingest_file(path)
            second = ingest_file(path)
        self.assertEqual(first["ingested"], 1)
        self.assertEqual(second["ingested"], 0)
        self.assertEqual(RawEvent.objects.count(), 1)
        self.assertEqual(Session.objects.count(), 1)
        self.assertEqual(Turn.objects.count(), 1)
        self.assertEqual(ToolCall.objects.get().argument_hash, digest({}))

    def test_mcp_and_http_started_completed_events_are_paired(self):
        rows = [
            {"timestamp": "2026-08-14T10:00:00Z", "session_id": "mcp-1", "event_type": "mcp.tool_call.started", "request_id": "tool-1", "tool": "get_workflow", "arguments": {"workflow_id": "42"}},
            {"timestamp": "2026-08-14T10:00:00.1Z", "session_id": "mcp-1", "event_type": "jotform.request.started", "request_id": "http-1", "method": "GET", "url": "https://api.jotform.com/workflow/123456/combined", "params": {}},
            {"timestamp": "2026-08-14T10:00:00.3Z", "session_id": "mcp-1", "event_type": "jotform.request.completed", "request_id": "http-1", "method": "GET", "url": "https://api.jotform.com/workflow/123456/combined", "status_code": 200, "duration_ms": 200, "response_text": "{}"},
            {"timestamp": "2026-08-14T10:00:00.4Z", "session_id": "mcp-1", "event_type": "mcp.tool_call.completed", "request_id": "tool-1", "tool": "get_workflow", "duration_ms": 400, "result": {"ok": True}, "is_error": False},
        ]
        with TemporaryDirectory() as directory:
            ingest_file(self.write_jsonl(directory, "mcp.jsonl", rows))
        self.assertEqual(Span.objects.count(), 2)
        tool = ToolCall.objects.get()
        external = ExternalCall.objects.get()
        self.assertEqual(tool.arguments, {"workflow_id": "42"})
        self.assertEqual(external.url_template, "https://api.jotform.com/workflow/{id}/combined")
        self.assertEqual(external.span.parent_id, tool.span_id)
        self.assertEqual(external.span.duration_ms, 200)
        self.assertEqual(external.span.attributes["status_code"], 200)
        self.assertEqual(external.span.attributes["response"], {})

    def test_invalid_json_goes_to_quarantine_without_raw_event(self):
        with TemporaryDirectory() as directory:
            result = ingest_file(self.write_jsonl(directory, "bad.jsonl", ["not-json"]))
        self.assertEqual(result["quarantined"], 1)
        self.assertEqual(QuarantinedEvent.objects.count(), 1)
        self.assertEqual(RawEvent.objects.count(), 0)

    def test_duplicate_rule_is_stable_across_runs(self):
        payload = {
            "timestamp": "2026-08-14T10:00:01Z", "session_id": "agent-duplicate",
            "provider": "anthropic", "question": "Read", "answer": "Done", "duration_ms": 500,
            "tool_calls": [
                {"name": "get_workflow", "arguments": {"workflow_id": "1"}, "result": "{}", "duration_ms": 100},
                {"name": "get_workflow", "arguments": {"workflow_id": "1"}, "result": "{}", "duration_ms": 100},
            ],
        }
        with TemporaryDirectory() as directory:
            ingest_file(self.write_jsonl(directory, "duplicate.jsonl", [payload]))
        self.assertEqual(run_all_rules(), 1)
        self.assertEqual(run_all_rules(), 0)
        finding = Finding.objects.get()
        self.assertEqual(finding.rule_code, "REDUNDANT_READ")
        self.assertEqual(finding.wasted_ms, 100)

    def test_instrumented_agent_and_mcp_share_session_turn_and_parent_chain(self):
        rows = [
            {"timestamp": "2026-08-14T10:00:00Z", "schema_version": 1, "session_id": "shared-1", "turn_id": "turn-1", "trace_id": "trace-1", "span_id": "model-span", "event_type": "model.step.started", "provider": "anthropic", "model": "claude-test", "step_no": 1},
            {"timestamp": "2026-08-14T10:00:00.2Z", "schema_version": 1, "session_id": "shared-1", "turn_id": "turn-1", "trace_id": "trace-1", "span_id": "model-span", "event_type": "model.step.completed", "provider": "anthropic", "model": "claude-test", "step_no": 1, "duration_ms": 200, "input_tokens": 12, "output_tokens": 4},
            {"timestamp": "2026-08-14T10:00:00.3Z", "schema_version": 1, "session_id": "shared-1", "turn_id": "turn-1", "trace_id": "trace-1", "span_id": "tool-span", "parent_span_id": "model-span", "event_type": "mcp.tool_call.started", "request_id": "request-1", "tool": "list_workflows", "arguments": {}},
            {"timestamp": "2026-08-14T10:00:00.4Z", "schema_version": 1, "session_id": "shared-1", "turn_id": "turn-1", "trace_id": "trace-1", "span_id": "tool-span", "parent_span_id": "model-span", "event_type": "mcp.tool_call.completed", "request_id": "request-1", "tool": "list_workflows", "duration_ms": 100, "result": {}},
        ]
        agent = {"timestamp": "2026-08-14T10:00:01Z", "schema_version": 1, "session_id": "shared-1", "task_id": "task-1", "turn_id": "turn-1", "trace_id": "trace-1", "provider": "anthropic", "model": "claude-test", "question": "List", "answer": "Done", "duration_ms": 1000, "tool_calls": [{"name": "list_workflows", "arguments": {}, "result": "{}", "duration_ms": 100}]}
        with TemporaryDirectory() as directory:
            ingest_file(self.write_jsonl(directory, "audit.jsonl", rows))
            ingest_file(self.write_jsonl(directory, "turns.jsonl", [agent]))
        self.assertEqual(Session.objects.count(), 1)
        self.assertEqual(ToolCall.objects.count(), 1)
        turn = Turn.objects.get(external_id="turn-1")
        model = ModelStep.objects.get()
        tool = ToolCall.objects.get()
        self.assertEqual(model.span.turn_id, turn.id)
        self.assertEqual(tool.span.turn_id, turn.id)
        self.assertEqual(tool.span.parent_id, model.span_id)
        self.assertEqual(tool.span.trace_id, "trace-1")

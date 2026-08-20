from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.core.models import Workspace
from apps.core.presentation import payload_fields, pretty_payload, result_summary
from apps.core.templatetags.core_extras import conversation_markdown
from apps.findings.models import Finding
from apps.ingestion.models import IngestionSource, RawEvent
from apps.ingestion.services import digest
from apps.traces.models import ExternalCall, Session, Span, ToolCall, Turn


class ViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        workspace = Workspace.objects.create(name="Test", slug="test")
        cls.session = Session.objects.create(
            workspace=workspace, external_session_id="session-view",
            provider="mcp", status="ok", correlation_confidence="low",
        )

    def test_all_main_pages_render(self):
        urls = [
            reverse("overview"), reverse("session-list"),
            reverse("session-detail", args=[self.session.id]),
            reverse("tool-intelligence"), reverse("template-intelligence"),
            reverse("findings"), reverse("costs"), reverse("data-health"),
        ]
        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)


    def test_trace_api_returns_session_shape(self):
        response = self.client.get(reverse("api-session-trace", args=[self.session.id]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["session"]["external_id"], "session-view")
        self.assertEqual(response.json()["spans"], [])

    def test_overview_and_tools_apis_render(self):
        self.assertEqual(self.client.get(reverse("api-overview")).status_code, 200)
        self.assertEqual(self.client.get(reverse("api-tools")).status_code, 200)

    def test_session_detail_renders_conversation_and_readable_tool_card(self):
        turn = Turn.objects.create(
            session=self.session, sequence_no=1, question="Workflow'ları listele",
            answer="**Üç workflow** bulundu.\n\n- Birinci\n- İkinci", status="ok", started_at=timezone.now(), duration_ms=420,
        )
        arguments = {"workspace_id": "demo", "include_archived": False}
        span = Span.objects.create(
            session=self.session, turn=turn, trace_id="trace", kind="tool",
            name="list_workflows", status="ok", started_at=turn.started_at, duration_ms=120,
        )
        ToolCall.objects.create(
            span=span, tool_name="list_workflows", arguments=arguments,
            result={"workflows": [{"id": "1"}]}, argument_hash=digest(arguments),
            result_hash=digest({"workflows": [{"id": "1"}]}), result_bytes=30,
        )

        response = self.client.get(reverse("session-detail", args=[self.session.id]))

        self.assertContains(response, "Conversation history")
        self.assertContains(response, "Workflow&#x27;ları listele")
        self.assertContains(response, "<strong>Üç workflow</strong>", html=True)
        self.assertContains(response, "<li>Birinci</li>", html=True)
        self.assertContains(response, "MCP TOOL")
        self.assertContains(response, 'data-operation-filter="tool"')
        self.assertContains(response, 'data-operation-kind="tool"')
        self.assertContains(response, "Expand all")
        self.assertContains(response, "MCP client")
        self.assertContains(response, "MCP server")
        self.assertContains(response, "Tool response")
        self.assertContains(response, "include archived")
        self.assertContains(response, "View JSON")

    def test_base_layout_exposes_theme_and_mobile_navigation_controls(self):
        response = self.client.get(reverse("overview"))

        self.assertContains(response, "Jotform Pulse")
        self.assertContains(response, "data-theme-toggle")
        self.assertContains(response, "data-sidebar-open")
        self.assertContains(response, "pulse-theme-change")

    def test_session_list_hides_empty_records_and_places_latest_activity_first(self):
        now = timezone.now()
        older = Session.objects.create(
            workspace=self.session.workspace, external_session_id="older-activity",
            provider="mcp", status="ok", started_at=now, ended_at=now,
        )
        latest = Session.objects.create(
            workspace=self.session.workspace, external_session_id="latest-activity",
            provider="mcp", status="ok", started_at=now - timedelta(hours=1),
            ended_at=now + timedelta(minutes=1),
        )
        for sequence, session in enumerate((older, latest), start=1):
            Span.objects.create(
                session=session, trace_id=f"trace-{sequence}", kind="tool", name="list_forms",
                status="ok", started_at=session.started_at, duration_ms=10, sequence_no=sequence,
            )

        response = self.client.get(reverse("session-list"))
        content = response.content.decode()

        self.assertNotIn("session-view", content)
        self.assertLess(content.index("latest-activ"), content.index("older-activi"))
        self.assertContains(response, "MCP activity")

    def test_session_list_hx_request_returns_live_results_partial(self):
        response = self.client.get(reverse("session-list"), HTTP_HX_REQUEST="true")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="session-results"')
        self.assertContains(response, 'hx-trigger="every 300s"')
        self.assertNotContains(response, "TRACE EXPLORER")

    def test_mcp_only_session_shows_unassigned_tool_activity(self):
        started = timezone.now()
        span = Span.objects.create(
            session=self.session, trace_id="mcp-trace", kind="tool", name="get_workflow",
            status="ok", started_at=started, duration_ms=85,
        )
        arguments = {"workflow_id": "123"}
        result = {"name": "Approval"}
        ToolCall.objects.create(
            span=span, tool_name="get_workflow", arguments=arguments, result=result,
            argument_hash=digest(arguments), result_hash=digest(result), result_bytes=20,
        )

        response = self.client.get(reverse("session-detail", args=[self.session.id]))

        self.assertContains(response, "MCP activity only")
        self.assertContains(response, "get_workflow")
        self.assertContains(response, "workflow id")

    def test_session_detail_pairs_http_request_and_response_events(self):
        started = timezone.now()
        span = Span.objects.create(
            session=self.session, trace_id="mcp-trace", kind="external", name="GET workflow",
            request_id="http-1", status="ok", started_at=started, duration_ms=25,
            attributes={"params": {"apiKey": "[REDACTED]"}},
        )
        ExternalCall.objects.create(
            span=span, service="jotform", method="GET",
            url_template="https://api.jotform.com/workflow/{id}",
            status_code=200, request_bytes=32, response_bytes=25,
        )
        source = IngestionSource.objects.create(path="/tmp/http.jsonl", inode="view-http")
        RawEvent.objects.create(
            source=source, source_offset=1, event_type="jotform.request.started",
            occurred_at=started, schema_version=1, payload_hash="started",
            payload={
                "session_id": "session-view", "event_type": "jotform.request.started",
                "request_id": "http-1", "method": "GET",
                "url": "https://api.jotform.com/workflow/42",
                "params": {"apiKey": "[REDACTED]"},
            },
        )
        RawEvent.objects.create(
            source=source, source_offset=2, event_type="jotform.request.completed",
            occurred_at=started + timedelta(milliseconds=25), schema_version=1, payload_hash="completed",
            payload={
                "session_id": "session-view", "event_type": "jotform.request.completed",
                "request_id": "http-1", "status_code": 200,
                "response_text": "{\"content\":{\"ok\":true}}",
            },
        )

        response = self.client.get(reverse("session-detail", args=[self.session.id]))

        self.assertContains(response, "MCP server")
        self.assertContains(response, "Jotform API")
        self.assertContains(response, "HTTP request")
        self.assertContains(response, "HTTP response")
        self.assertContains(response, "workflow/42")
        self.assertContains(response, "content")

    def test_payload_presentation_is_human_readable_and_bounded(self):
        self.assertEqual(payload_fields({"enabled": True})[0]["value"], "Yes")
        self.assertIn("2 items", result_summary([1, 2]))
        rendered = pretty_payload({"content": "x" * 100}, max_chars=30)
        self.assertIn("more characters hidden", rendered)

    def test_conversation_markdown_escapes_raw_html(self):
        rendered = conversation_markdown("**safe** <script>alert(1)</script>")
        self.assertIn("<strong>safe</strong>", rendered)
        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)

    def test_template_intelligence_htmx_and_modal(self):
        # Default view (Invocations tab)
        response_inv = self.client.get(reverse("template-intelligence") + "?tab=invocations")
        self.assertEqual(response_inv.status_code, 200)
        self.assertContains(response_inv, "MCP Oturum &amp; Sorgu Analizi")

        # Invocations HTMX partial table request
        response_inv_partial = self.client.get(
            reverse("template-intelligence") + "?target=invocations_table&tab=invocations&inv_q=feedback",
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response_inv_partial.status_code, 200)
        self.assertTemplateUsed(response_inv_partial, "templates/_invocations_table.html")

        # Catalog HTMX partial table request
        response = self.client.get(
            reverse("template-intelligence") + "?target=table&q=Leave&category=Human+Resources&sort=clones_desc",
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "templates/_table_rows.html")

        # Analysis tab request
        response_an = self.client.get(reverse("template-intelligence") + "?tab=analysis")
        self.assertEqual(response_an.status_code, 200)
        self.assertContains(response_an, "Çapraz Benzerlik")

        # Analysis HTMX bucket partial request
        response_an_partial = self.client.get(
            reverse("template-intelligence") + "?target=analysis_pairs_table&tab=analysis&bucket=0.95-1.00",
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response_an_partial.status_code, 200)
        self.assertTemplateUsed(response_an_partial, "templates/_analysis_pairs_table.html")

        # Modal endpoint test
        modal_resp = self.client.get(reverse("template-detail-modal", args=["non-existent-id"]))
        self.assertEqual(modal_resp.status_code, 200)
        self.assertTemplateUsed(modal_resp, "templates/_detail_modal.html")


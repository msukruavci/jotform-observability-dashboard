from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import override_settings
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.core.models import Workspace
from apps.core.experiment_metrics import experiment_run_rows
from apps.core.presentation import payload_fields, pretty_payload, result_summary
from apps.core.templatetags.core_extras import conversation_markdown
from apps.findings.models import Finding
from apps.ingestion.models import IngestionSource, RawEvent
from apps.ingestion.services import digest
from apps.traces.models import ExternalCall, Session, Span, ToolCall, Turn


class ViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        workspace, _ = Workspace.objects.get_or_create(slug="test", defaults={"name": "Test"})
        cls.session = Session.objects.create(
            workspace=workspace, external_session_id="session-view",
            provider="mcp", status="ok", correlation_confidence="low",
        )

    def test_all_main_pages_render(self):
        urls = [
            reverse("overview"), reverse("session-list"),
            reverse("session-detail", args=[self.session.id]),
            reverse("tool-intelligence"), reverse("template-intelligence"),
            reverse("experiments"), reverse("findings"), reverse("costs"),
            reverse("data-health"),
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

    def test_session_detail_and_list_render_created_workflows_and_forms(self):
        span = Span.objects.create(
            session=self.session, trace_id="trace-create", kind="tool",
            name="build_workflow_bulk", status="ok", started_at=timezone.now(), duration_ms=500,
        )
        args = {"title": "Onboarding Workflow", "form_prompt": "Employee Onboarding Form"}
        res = {
            "workflow_id": "242943285550056",
            "workflow_url": "https://www.jotform.com/workflow/242943285550056/build",
            "trigger_form_id": "262353379332055",
            "trigger_form_url": "https://www.jotform.com/build/262353379332055",
            "created_steps": {"mgr_approval": "4", "send_welcome": "5"},
            "created_links_count": 2,
            "deleted_steps": ["8"],
        }
        ToolCall.objects.create(
            span=span, tool_name="build_workflow_bulk", arguments=args, result=res,
            argument_hash=digest(args), result_hash=digest(res), result_bytes=100,
        )

        detail_resp = self.client.get(reverse("session-detail", args=[self.session.id]))
        self.assertEqual(detail_resp.status_code, 200)
        self.assertContains(detail_resp, "Created & Mutated Workflows / Forms")
        self.assertContains(detail_resp, "Onboarding Workflow")
        self.assertContains(detail_resp, "242943285550056")
        self.assertContains(detail_resp, "https://www.jotform.com/workflow/242943285550056/build")
        self.assertContains(detail_resp, "https://www.jotform.com/build/262353379332055")
        self.assertContains(detail_resp, "Open in Jotform Workflow Builder")

        list_resp = self.client.get(reverse("session-list") + "?platform=mcp")
        self.assertEqual(list_resp.status_code, 200)
        self.assertContains(list_resp, "242943285550056")
        self.assertContains(list_resp, "262353379332055")

    def test_session_list_and_overview_filter_by_platform(self):
        # Create Claude, Gemini, GPT sessions
        s_claude = Session.objects.create(
            workspace=self.session.workspace, external_session_id="claude-s1",
            provider="anthropic", model="claude-3-7-sonnet", status="ok", duration_ms=800,
        )
        Span.objects.create(session=s_claude, trace_id="t-c", kind="tool", name="list_workflows", status="ok")

        s_gemini = Session.objects.create(
            workspace=self.session.workspace, external_session_id="gemini-s1",
            provider="gemini", model="gemini-3.6-flash", status="ok", duration_ms=400,
        )
        Span.objects.create(session=s_gemini, trace_id="t-g", kind="tool", name="list_workflows", status="ok")

        s_gpt = Session.objects.create(
            workspace=self.session.workspace, external_session_id="gpt-s1",
            provider="openai", model="gpt-4o", status="ok", duration_ms=600,
        )
        Span.objects.create(session=s_gpt, trace_id="t-gpt", kind="tool", name="list_workflows", status="ok")

        # Test overview page platforms cards
        overview_resp = self.client.get(reverse("overview"))
        self.assertEqual(overview_resp.status_code, 200)
        self.assertContains(overview_resp, "Claude (Anthropic)")
        self.assertContains(overview_resp, "Gemini (Google)")
        self.assertContains(overview_resp, "ChatGPT / GPT (OpenAI)")

        # Test filter by platform claude
        claude_resp = self.client.get(reverse("session-list") + "?platform=claude")
        self.assertEqual(claude_resp.status_code, 200)
        self.assertContains(claude_resp, "claude-s1")
        self.assertNotContains(claude_resp, "gemini-s1")
        self.assertNotContains(claude_resp, "gpt-s1")

        # Test filter by platform gemini
        gemini_resp = self.client.get(reverse("session-list") + "?platform=gemini")
        self.assertEqual(gemini_resp.status_code, 200)
        self.assertContains(gemini_resp, "gemini-s1")
        self.assertNotContains(gemini_resp, "claude-s1")

    def test_base_layout_exposes_theme_and_mobile_navigation_controls(self):
        response = self.client.get(reverse("overview"))

        self.assertContains(response, "Jotform Pulse")
        self.assertContains(response, "data-theme-toggle")
        self.assertContains(response, "data-sidebar-open")
        self.assertContains(response, "pulse-theme-change")

    def test_experiments_page_renders_abcd_metrics(self):
        with TemporaryDirectory() as directory:
            image_root = Path(directory) / "mcp_server" / "logs" / "img"
            image_root.mkdir(parents=True)
            (image_root / "A1.png").write_bytes(b"\x89PNG\r\n\x1a\n")
            (image_root / "A2.png").write_bytes(b"\x89PNG\r\n\x1a\n")
            (image_root / "D1.png").write_bytes(b"\x89PNG\r\n\x1a\n")
            started = timezone.now()
            session = Session.objects.create(
                workspace=self.session.workspace,
                external_session_id="abcd-run-d",
                provider="mcp",
                status="ok",
                started_at=started,
                ended_at=started + timedelta(milliseconds=900),
                duration_ms=900,
                metadata={},
            )
            source = IngestionSource.objects.create(
                path="/tmp/abcd-d.jsonl",
                inode="abcd-d",
            )
            RawEvent.objects.create(
                source=source,
                source_offset=0,
                event_type="mcp.list_tools.completed",
                occurred_at=started,
                payload={
                    "session_id": "abcd-run-d",
                    "event_type": "mcp.list_tools.completed",
                    "tool_profile": "ab_d",
                    "experiment_scenario": "D",
                    "tools": ["search_workflow_templates", "inspect_workflow_gaps"],
                    "tool_count": 13,
                },
                payload_hash=digest({"session_id": "abcd-run-d"}),
                correlation_confidence="low",
            )
            for index, tool_name in enumerate(("search_workflow_templates", "build_workflow_bulk", "inspect_workflow_gaps"), start=1):
                span = Span.objects.create(
                    session=session, trace_id="trace-abcd", kind="tool", name=tool_name,
                    status="ok", started_at=started, duration_ms=100, sequence_no=index,
                )
                if tool_name == "inspect_workflow_gaps":
                    result = {"ok_to_publish": True, "issues": []}
                elif tool_name == "build_workflow_bulk":
                    result = {
                        "workflow_id": "wf-d",
                        "workflow_url": "https://www.jotform.com/workflows/wf-d",
                        "trigger_form_id": "262353379332055",
                        "created_steps": {"ack": {"id": "step-1"}},
                        "created_links": [{"from": "start", "to": "ack"}],
                    }
                else:
                    result = {"ok": True}
                arguments = (
                    {"title": "Ekipman Bakım Workflow", "trigger_form_id": "262353379332055", "steps": [{"ref": "ack"}]}
                    if tool_name == "build_workflow_bulk" else {}
                )
                ToolCall.objects.create(
                    span=span, tool_name=tool_name, arguments=arguments, result=result,
                    argument_hash=digest(arguments), result_hash=digest(result), result_bytes=20,
                )

            with override_settings(MCP_LOG_ROOT=Path(directory)):
                # Experiment data/notes must survive the global live-activity
                # filters, which are still shown in the shared header.
                response = self.client.get(reverse("experiments") + "?range=1h&platform=gpt")

        self.assertContains(response, "ABCD Experiments")
        self.assertContains(response, "ab_d")
        self.assertContains(response, "Full System")
        self.assertContains(response, "Open")
        self.assertContains(response, "First build")
        self.assertContains(response, "Final build")
        self.assertContains(response, "To final")
        self.assertContains(
            response,
            "Form was created via ChatGPT plugin/tool; workflow used the pre-created trigger_form_id",
        )

    def test_experiment_image_endpoint_serves_log_image(self):
        with TemporaryDirectory() as directory:
            image_root = Path(directory) / "mcp_server" / "logs" / "img"
            image_root.mkdir(parents=True)
            (image_root / "A1.png").write_bytes(b"\x89PNG\r\n\x1a\n")

            with override_settings(MCP_LOG_ROOT=Path(directory)):
                response = self.client.get(reverse("experiment-image", args=["A1.png"]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/png")

    def test_experiment_image_endpoint_rejects_path_traversal(self):
        response = self.client.get("/experiments/images/../secret.png")

        self.assertEqual(response.status_code, 404)

    def test_experiment_rows_keep_latest_run_per_scenario(self):
        started = timezone.now()
        for index, external_id in enumerate(("abcd-b-old", "abcd-b-new"), start=1):
            session = Session.objects.create(
                workspace=self.session.workspace,
                external_session_id=external_id,
                provider="mcp",
                status="ok",
                started_at=started + timedelta(minutes=index),
                ended_at=started + timedelta(minutes=index, seconds=1),
                duration_ms=1000,
                metadata={"experiment_scenario": "B", "tool_profile": "ab_b"},
            )
            span = Span.objects.create(
                session=session, trace_id=f"trace-{index}", kind="tool", name="build_workflow_bulk",
                status="ok", started_at=session.started_at, duration_ms=100, sequence_no=1,
            )
            arguments = {"title": "Warranty", "trigger_form_id": f"form-{index}", "steps": [{"ref": "ack"}]}
            result = {"workflow_id": f"workflow-{index}", "created_steps": {"ack": {"id": "1"}}}
            ToolCall.objects.create(
                span=span, tool_name="build_workflow_bulk", arguments=arguments, result=result,
                argument_hash=digest(arguments), result_hash=digest(result), result_bytes=20,
            )

        rows = experiment_run_rows()

        self.assertEqual([row["scenario"]["label"] for row in rows], ["B"])
        self.assertEqual(rows[0]["session"].external_session_id, "abcd-b-new")

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

        response = self.client.get(reverse("session-list") + "?platform=mcp")
        content = response.content.decode()

        self.assertNotIn("session-view", content)
        self.assertLess(content.index("latest-activ"), content.index("older-activi"))
        self.assertContains(response, "MCP activity")

    def test_session_list_hides_mcp_direct_by_default_but_shows_when_selected(self):
        now = timezone.now()
        direct = Session.objects.create(
            workspace=self.session.workspace, external_session_id="direct-mcp-demo",
            provider="mcp", status="ok", started_at=now, ended_at=now,
        )
        Span.objects.create(
            session=direct, trace_id="trace-direct", kind="tool", name="list_forms",
            status="ok", started_at=now, duration_ms=10,
        )
        claude = Session.objects.create(
            workspace=self.session.workspace, external_session_id="claude-visible-demo",
            provider="anthropic", model="claude-3-7-sonnet", status="ok",
            started_at=now, ended_at=now,
        )
        Span.objects.create(
            session=claude, trace_id="trace-claude", kind="tool", name="list_forms",
            status="ok", started_at=now, duration_ms=10,
        )

        default_response = self.client.get(reverse("session-list"))
        self.assertContains(default_response, "claude-visib")
        self.assertNotContains(default_response, "direct-mcp-d")

        mcp_response = self.client.get(reverse("session-list") + "?platform=mcp")
        self.assertContains(mcp_response, "direct-mcp-d")

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

    def test_session_detail_hides_internal_function_traces_but_keeps_public_tools_and_apis(self):
        started = timezone.now()
        public_span = Span.objects.create(
            session=self.session, trace_id="mcp-trace", kind="tool", name="get_workflow",
            status="ok", started_at=started, duration_ms=85,
        )
        ToolCall.objects.create(
            span=public_span, tool_name="get_workflow", arguments={"workflow_id": "123"},
            result={"name": "Approval"}, argument_hash=digest({"workflow_id": "123"}),
            result_hash=digest({"name": "Approval"}), result_bytes=20,
        )
        internal_span = Span.objects.create(
            session=self.session, trace_id="mcp-trace", kind="tool",
            name="mcp_server.tree_builder.build_tree", status="ok",
            started_at=started + timedelta(milliseconds=10), duration_ms=12,
        )
        ToolCall.objects.create(
            span=internal_span, tool_name="mcp_server.tree_builder.build_tree",
            arguments={"nodes": 2}, result={"ok": True},
            argument_hash=digest({"nodes": 2}), result_hash=digest({"ok": True}),
            result_bytes=12,
        )
        api_span = Span.objects.create(
            session=self.session, trace_id="mcp-trace", kind="external",
            name="GET https://api.jotform.com/workflow/{id}", status="ok",
            started_at=started + timedelta(milliseconds=20), duration_ms=25,
        )
        ExternalCall.objects.create(
            span=api_span, service="jotform", method="GET",
            url_template="https://api.jotform.com/workflow/{id}",
            status_code=200, request_bytes=32, response_bytes=25,
        )

        response = self.client.get(reverse("session-detail", args=[self.session.id]))

        self.assertContains(response, "get_workflow")
        self.assertContains(response, "Jotform API")
        self.assertNotContains(response, "mcp_server.tree_builder.build_tree")

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
        self.assertContains(response_inv, "MCP Session &amp; Query Analysis")

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
        self.assertContains(response_an, "Cross-Similarity")

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

    def test_tool_intelligence_overview_and_modal(self):
        span = Span.objects.create(
            session=self.session, trace_id="trace-tool-intel", kind="tool",
            name="list_workflows", status="ok", started_at=timezone.now(), duration_ms=250,
        )
        ToolCall.objects.create(
            span=span, tool_name="list_workflows", arguments={"limit": 10},
            result={"workflows": [{"id": "1", "title": "Test"}]},
            argument_hash=digest({"limit": 10}), result_hash=digest({"workflows": []}),
            result_bytes=80,
        )

        # 1. Overview page
        response = self.client.get(reverse("tool-intelligence"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "tools/overview.html")
        self.assertContains(response, "Tool Intelligence &amp; Invocations")
        self.assertContains(response, "list_workflows")

        # 2. Tool Detail Modal endpoint
        modal_resp = self.client.get(reverse("tool-detail-modal", args=["list_workflows"]))
        self.assertEqual(modal_resp.status_code, 200)
        self.assertTemplateUsed(modal_resp, "tools/_detail_modal.html")
        self.assertContains(modal_resp, "list_workflows")
        self.assertContains(modal_resp, "Step-by-Step Invocation History")

        # 3. Tool Detail page endpoint
        detail_resp = self.client.get(reverse("tool-detail", args=["list_workflows"]))
        self.assertEqual(detail_resp.status_code, 200)
        self.assertTemplateUsed(detail_resp, "tools/overview.html")

    def test_tool_intelligence_excludes_internal_function_traces(self):
        public_span = Span.objects.create(
            session=self.session, trace_id="trace-public-tool", kind="tool",
            name="build_workflow_bulk", status="ok", started_at=timezone.now(), duration_ms=250,
        )
        ToolCall.objects.create(
            span=public_span, tool_name="build_workflow_bulk", arguments={"title": "Demo"},
            result={"workflow_id": "123"}, argument_hash=digest({"title": "Demo"}),
            result_hash=digest({"workflow_id": "123"}), result_bytes=30,
        )
        internal_span = Span.objects.create(
            session=self.session, trace_id="trace-internal-tool", kind="tool",
            name="mcp_server.tree_builder.build_tree", status="ok", started_at=timezone.now(), duration_ms=12,
        )
        ToolCall.objects.create(
            span=internal_span, tool_name="mcp_server.tree_builder.build_tree", arguments={"nodes": 2},
            result={"ok": True}, argument_hash=digest({"nodes": 2}),
            result_hash=digest({"ok": True}), result_bytes=12,
        )

        response = self.client.get(reverse("tool-intelligence"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "build_workflow_bulk")
        self.assertNotContains(response, "mcp_server.tree_builder.build_tree")

    def test_time_filter_presets_across_pages(self):
        # Create an older session (5 days ago) and a recent session (10 minutes ago)
        now = timezone.now()
        s_old = Session.objects.create(
            workspace=self.session.workspace, external_session_id="s-old-5d",
            provider="claude", model="claude-3-7-sonnet", status="ok", duration_ms=800,
            started_at=now - timedelta(days=5),
        )
        s_recent = Session.objects.create(
            workspace=self.session.workspace, external_session_id="s-recent-10m",
            provider="gpt", model="gpt-4o", status="ok", duration_ms=300,
            started_at=now - timedelta(minutes=10),
        )
        # Create spans for tools
        Span.objects.create(session=s_old, trace_id="t-old", kind="tool", name="list_workflows", status="ok", started_at=s_old.started_at)
        Span.objects.create(session=s_recent, trace_id="t-rec", kind="tool", name="list_workflows", status="ok", started_at=s_recent.started_at)

        # 1. Test Overview with 1h filter (should contain recent, exclude old)
        resp_1h = self.client.get(reverse("overview") + "?range=1h")
        self.assertEqual(resp_1h.status_code, 200)
        self.assertEqual(resp_1h.context["active_time_range"], "1h")
        self.assertContains(resp_1h, "Last 1 Hour")

        # 2. Test Sessions list with 24h filter
        resp_sessions_24h = self.client.get(reverse("session-list") + "?range=24h")
        self.assertEqual(resp_sessions_24h.status_code, 200)
        self.assertContains(resp_sessions_24h, "s-recent-10m")
        self.assertNotContains(resp_sessions_24h, "s-old-5d")

        # 3. Test Sessions list with 7d filter (both should be present)
        resp_sessions_7d = self.client.get(reverse("session-list") + "?range=7d")
        self.assertEqual(resp_sessions_7d.status_code, 200)
        self.assertContains(resp_sessions_7d, "s-recent-10m")
        self.assertContains(resp_sessions_7d, "s-old-5d")

        # 4. Test Tool Intelligence with 1h vs 7d
        resp_tools_1h = self.client.get(reverse("tool-intelligence") + "?range=1h")
        self.assertEqual(resp_tools_1h.status_code, 200)
        self.assertEqual(resp_tools_1h.context["active_time_range"], "1h")

    def test_tool_multi_field_filtering_and_partial(self):
        span1 = Span.objects.create(
            session=self.session, trace_id="trace-f1", kind="tool",
            name="build_workflow_bulk", status="ok", duration_ms=250,
        )
        ToolCall.objects.create(
            span=span1, tool_name="build_workflow_bulk", arguments={"title": "Leave Workflow"},
            result={"workflow_id": "111"}, argument_hash="h1", result_hash="r1", is_error=False,
        )
        span2 = Span.objects.create(
            session=self.session, trace_id="trace-f2", kind="tool",
            name="build_workflow_bulk", status="error", duration_ms=6200,
        )
        ToolCall.objects.create(
            span=span2, tool_name="build_workflow_bulk", arguments={"title": "Expense Approval"},
            result={"error": "Failed step"}, argument_hash="h2", result_hash="r2", is_error=True,
        )

        # 1. Filter by status=error
        resp_err = self.client.get(reverse("tool-detail-modal", args=["build_workflow_bulk"]) + "?status=error")
        self.assertEqual(resp_err.status_code, 200)
        self.assertContains(resp_err, "Expense Approval")
        self.assertNotContains(resp_err, "Leave Workflow")

        # 2. Filter by status=ok
        resp_ok = self.client.get(reverse("tool-detail-modal", args=["build_workflow_bulk"]) + "?status=ok")
        self.assertEqual(resp_ok.status_code, 200)
        self.assertContains(resp_ok, "Leave Workflow")
        self.assertNotContains(resp_ok, "Expense Approval")

        # 3. Filter by search query in JSON
        resp_q = self.client.get(reverse("tool-detail-modal", args=["build_workflow_bulk"]) + "?q=Leave")
        self.assertEqual(resp_q.status_code, 200)
        self.assertContains(resp_q, "Leave Workflow")
        self.assertNotContains(resp_q, "Expense Approval")

        # 4. HTMX partial request for invocations_list
        resp_partial = self.client.get(
            reverse("tool-detail-modal", args=["build_workflow_bulk"]) + "?target=invocations_list&q=Expense",
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp_partial.status_code, 200)
        self.assertTemplateUsed(resp_partial, "tools/_invocations_list.html")
        self.assertContains(resp_partial, "Expense Approval")

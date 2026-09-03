from __future__ import annotations

import json
import mimetypes
from pathlib import Path

import markdown
import google.generativeai as genai

from django.contrib import messages
from django.conf import settings
from django.core.paginator import Paginator
from django.db.models import Avg, Count, F, Q, Sum
from django.http import FileResponse, Http404, HttpResponse, HttpResponseRedirect, JsonResponse
from django.urls import reverse
from django.utils import timezone
from django.views import View
from django.views.generic import DetailView, TemplateView

from apps.findings.models import Annotation, Finding
from apps.ingestion.models import IngestionSource, QuarantinedEvent, RawEvent
from apps.traces.models import Session, Span, ToolCall, Turn
from apps.core.models import AITelemetryUsage

from .experiment_metrics import IMAGE_EXTENSIONS, experiment_image_rows, experiment_run_rows, experiment_summary, scenario_rows
from .metrics import cost_by_model, overview_metrics, platform_breakdown_metrics, session_timeseries, tool_metrics
from .presentation import operation_card, pretty_payload, reconstruct_synthetic_turns, session_created_resources, session_timing_breakdown
from .template_analytics import (
    filter_templates,
    get_cross_similarity_analysis_data,
    get_mcp_template_invocations,
    get_template_by_id,
    get_template_charts_data,
    get_templates_overview_metrics,
)
from .platform_filters import parse_platform_filter
from .time_filters import parse_time_filter
from .tool_analytics import get_tool_detail_data, get_tools_overview_metrics



class OverviewView(TemplateView):
    template_name = "dashboard/overview.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        since, active_range, time_ctx = parse_time_filter(self.request)
        platform_q, active_platform, platform_ctx = parse_platform_filter(self.request)
        
        context.update(time_ctx)
        context.update(platform_ctx)
        
        context.update(overview_metrics(since=since, platform_q=platform_q))
        context["platforms"] = platform_breakdown_metrics(since=since, platform_q=platform_q)
        context["timeseries_json"] = json.dumps(session_timeseries(since=since, platform_q=platform_q))
        
        slow_qs = Session.objects.exclude(duration_ms=None)
        if since is not None:
            slow_qs = slow_qs.filter(started_at__gte=since)
        if platform_q:
            slow_qs = slow_qs.filter(platform_q)
        context["slow_sessions"] = slow_qs.order_by("-duration_ms")[:6]
        
        findings_qs = Finding.objects.filter(status="open")
        if since is not None:
            findings_qs = findings_qs.filter(created_at__gte=since)
        if platform_q:
            findings_qs = findings_qs.filter(session__in=Session.objects.filter(platform_q))
        context["top_findings"] = findings_qs.select_related("session").order_by("-severity", "-created_at")[:6]
        context["active_nav"] = "overview"
        return context


class SessionListView(TemplateView):
    template_name = "sessions/list.html"

    def get_template_names(self):
        if self.request.headers.get("HX-Request") == "true":
            return ["sessions/_results.html"]
        return super().get_template_names()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        since, active_range, time_ctx = parse_time_filter(self.request)
        platform_q, active_platform, platform_ctx = parse_platform_filter(self.request)
        
        context.update(time_ctx)
        context.update(platform_ctx)

        base_query = Session.objects.annotate(
            turn_count=Count("turns", distinct=True),
            span_count=Count("spans", distinct=True),
            tool_count=Count("spans", filter=Q(spans__kind="tool"), distinct=True),
            finding_count=Count("findings", distinct=True),
        ).filter(Q(turn_count__gt=0) | Q(span_count__gt=0))
        if since is not None:
            base_query = base_query.filter(started_at__gte=since)
        
        query = base_query
        if platform_q:
            query = query.filter(platform_q)
            
        params = self.request.GET
        if params.get("q"):
            query = query.filter(Q(external_session_id__icontains=params["q"]) | Q(model__icontains=params["q"]) | Q(turns__question__icontains=params["q"])).distinct()
        for field in ("status", "provider", "model", "correlation_confidence"):
            if params.get(field):
                query = query.filter(**{field: params[field]})
        if params.get("tool"):
            query = query.filter(spans__tool_call__tool_name=params["tool"]).distinct()
        if params.get("finding"):
            query = query.filter(findings__rule_code=params["finding"]).distinct()
        paginator = Paginator(query.order_by(
            F("ended_at").desc(nulls_last=True),
            F("started_at").desc(nulls_last=True),
        ), 50)
        page_obj = paginator.get_page(params.get("page"))
        session_ids = [item.id for item in page_obj]
        spans_by_session: dict = {}
        if session_ids:
            for span in Span.objects.filter(session_id__in=session_ids).order_by("started_at"):
                spans_by_session.setdefault(span.session_id, []).append(span)

        for item in page_obj:
            item.created_resources = session_created_resources(item)
            item.timing_breakdown = session_timing_breakdown(item, spans_by_session.get(item.id, []))
        context["page_obj"] = page_obj
        
        from apps.core.platform_filters import PLATFORM_PRESETS
        platform_counts = {"all": base_query.count()}
        for p in PLATFORM_PRESETS:
            if not p.get("is_all"):
                platform_counts[p["key"]] = base_query.filter(p["filter"]).count()
                
        context["platform_counts"] = platform_counts
        context["providers"] = Session.objects.exclude(provider="").values_list("provider", flat=True).distinct().order_by("provider")
        context["models"] = Session.objects.exclude(model="").values_list("model", flat=True).distinct().order_by("model")
        context["tools"] = ToolCall.objects.values_list("tool_name", flat=True).distinct().order_by("tool_name")
        context["rule_codes"] = Finding.objects.values_list("rule_code", flat=True).distinct().order_by("rule_code")
        context["active_nav"] = "sessions"
        return context


class SessionDetailView(DetailView):
    model = Session
    template_name = "sessions/detail.html"
    context_object_name = "session"

    def post(self, request, *args, **kwargs):
        finding = Finding.objects.get(pk=request.POST.get("finding_id"), session_id=kwargs["pk"])
        action = request.POST.get("action")
        if action in {"accepted", "false_positive", "snoozed"}:
            finding.status = action
            finding.save(update_fields=["status", "updated_at"])
            Annotation.objects.create(finding=finding, label=action, comment=request.POST.get("comment", ""), created_by=request.user if request.user.is_authenticated else None)
            messages.success(request, "Finding feedback saved.")
        return HttpResponseRedirect(reverse("session-detail", kwargs={"pk": kwargs["pk"]}))

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        session = self.object
        context["created_resources"] = session_created_resources(session)
        context["ai_model_stats"] = AITelemetryUsage.get_today_stats()
        context["ai_model_stats_list"] = list(context["ai_model_stats"].values())
        spans = list(session.spans.select_related("parent", "turn", "tool_call", "external_call", "model_step").order_by("started_at", "sequence_no"))
        origin = session.started_at or next((span.started_at for span in spans if span.started_at), None)
        waterfall = []
        measured_total = max(
            (
                (span.ended_at - origin).total_seconds() * 1000
                if origin and span.ended_at
                else float(span.duration_ms or 0)
            )
            for span in spans
        ) if spans else 0
        total = max(float(session.duration_ms or measured_total or 1), 1)
        for span in spans:
            offset = (span.started_at - origin).total_seconds() * 1000 if origin and span.started_at else 0
            waterfall.append({"span": span, "offset_pct": max(offset / total * 100, 0), "width_pct": max(float(span.duration_ms or 1) / total * 100, .35)})
        context["waterfall"] = waterfall
        request_ids = {span.request_id for span in spans if span.request_id}
        raw_events_by_request: dict[str, list[dict]] = {}
        if request_ids:
            for event in RawEvent.objects.filter(payload__session_id=session.external_session_id).order_by("occurred_at", "id"):
                request_id = event.payload.get("request_id")
                if request_id in request_ids:
                    raw_events_by_request.setdefault(request_id, []).append(event.payload)
        if session.turns.exists():
            operations_by_turn = {}
            children_by_parent = {}
            top_level_spans = []
            
            for span in spans:
                if span.parent_id:
                    children_by_parent.setdefault(span.parent_id, []).append(span)
                else:
                    top_level_spans.append(span)
                    
            def build_op_card(s):
                child_spans = children_by_parent.get(s.id, [])
                child_cards = [build_op_card(c) for c in child_spans]
                return operation_card(s, raw_events_by_request.get(s.request_id), child_cards)

            for span in top_level_spans:
                if span.turn_id:
                    operations_by_turn.setdefault(span.turn_id, []).append(build_op_card(span))
                    
            context["conversation"] = [
                {"turn": turn, "operations": operations_by_turn.get(turn.id, [])}
                for turn in session.turns.all()
            ]
            all_unassigned = [
                build_op_card(span) for span in top_level_spans if not span.turn_id
            ]
        else:
            context["conversation"] = reconstruct_synthetic_turns(spans, raw_events_by_request)
            all_unassigned = []
        op_paginator = Paginator(all_unassigned, 50)
        op_page_obj = op_paginator.get_page(self.request.GET.get("op_page"))
        context["total_operations_count"] = len(all_unassigned)
        context["unassigned_operations"] = op_page_obj.object_list
        context["operations_page_obj"] = op_page_obj
        context["findings"] = session.findings.select_related("span", "turn").order_by("-created_at")
        context["raw_events"] = [
            {"event": event, "pretty_payload": pretty_payload(event.payload)}
            for event in RawEvent.objects.filter(payload__session_id=session.external_session_id).order_by("occurred_at")
        ]
        context["timing_breakdown"] = session_timing_breakdown(session, spans)
        context["active_nav"] = "sessions"
        return context


class ToolIntelligenceView(TemplateView):
    template_name = "tools/overview.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        since, active_range, time_ctx = parse_time_filter(self.request)
        platform_q, active_platform, platform_ctx = parse_platform_filter(self.request)
        context.update(time_ctx)
        context.update(platform_ctx)

        platform_filter = active_platform if active_platform != "all" else ""
        category_filter = self.request.GET.get("category", "")
        status_filter = self.request.GET.get("status", "")
        search_q = self.request.GET.get("q", "")

        overview = get_tools_overview_metrics(
            since=since,
            platform_filter=platform_filter,
            category_filter=category_filter,
            status_filter=status_filter,
            search_q=search_q,
        )
        context.update(overview)
        context["search_q"] = search_q
        context["active_category"] = category_filter or "all"
        context["active_status"] = status_filter or "all"

        selected_tool = self.kwargs.get("tool_name") or self.request.GET.get("tool")
        if selected_tool:
            page = self.request.GET.get("page", 1)
            tool_data = get_tool_detail_data(
                selected_tool,
                page=page,
                search_q=search_q,
                status_filter=status_filter,
                platform_filter=platform_filter,
                since=since,
            )
            context["selected_tool"] = selected_tool
            context["selected_tool_data"] = tool_data

        context["active_nav"] = "tools"
        return context


class ToolDetailModalView(TemplateView):
    template_name = "tools/_detail_modal.html"

    def get_template_names(self):
        target = self.request.GET.get("target")
        if self.request.headers.get("HX-Request") == "true" and target == "invocations_list":
            return ["tools/_invocations_list.html"]
        return super().get_template_names()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        since, active_range, time_ctx = parse_time_filter(self.request)
        platform_q, active_platform, platform_ctx = parse_platform_filter(self.request)
        context.update(time_ctx)
        context.update(platform_ctx)

        tool_name = self.kwargs.get("tool_name", "")
        page = self.request.GET.get("page", 1)
        search_q = self.request.GET.get("q", "")
        status_filter = self.request.GET.get("status", "")
        platform_filter = active_platform if active_platform != "all" else ""
        speed_filter = self.request.GET.get("speed", "")
        http_filter = self.request.GET.get("http", "")

        tool_data = get_tool_detail_data(
            tool_name,
            page=page,
            search_q=search_q,
            status_filter=status_filter,
            platform_filter=platform_filter,
            speed_filter=speed_filter,
            http_filter=http_filter,
            since=since,
        )
        context["tool_data"] = tool_data
        context["tool_name"] = tool_name
        return context


class ExperimentABCDView(TemplateView):
    template_name = "experiments/index.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        since, active_range, time_ctx = parse_time_filter(self.request)
        platform_q, active_platform, platform_ctx = parse_platform_filter(self.request)
        context.update(time_ctx)
        context.update(platform_ctx)
        
        rows = scenario_rows()
        chart_rows = [
            {
                "label": row["label"],
                "profile": row["profile"],
                "avg_duration_ms": row["avg_duration_ms"],
                "avg_first_build_ms": row["avg_first_build_ms"],
                "avg_final_build_ms": row["avg_final_build_ms"],
                "avg_time_to_first_build_ms": row["avg_time_to_first_build_ms"],
                "avg_time_to_final_build_ms": row["avg_time_to_final_build_ms"],
                "avg_tool_calls": row["avg_tool_calls"],
                "avg_api_calls": row["avg_api_calls"],
                "quality_issues": row["quality_issues"],
                "finding_count": row["finding_count"],
                "build_errors": row["build_errors"],
                "missing_builds": row["missing_builds"],
            }
            for row in rows
        ]
        context["rows"] = rows
        context["run_rows"] = experiment_run_rows()
        context["image_rows"] = experiment_image_rows()
        context["summary"] = experiment_summary(rows)
        context["chart_json"] = json.dumps(chart_rows, ensure_ascii=False)
        context["active_nav"] = "experiments"
        return context


class ExperimentImageView(View):
    def get(self, request, filename):
        requested = Path(filename)
        if requested.name != filename or requested.suffix.lower() not in IMAGE_EXTENSIONS:
            raise Http404("Image not found")

        image_root = settings.MCP_LOG_ROOT / "mcp_server" / "logs" / "img"
        image_path = (image_root / requested.name).resolve()
        try:
            image_path.relative_to(image_root.resolve())
        except ValueError as exc:
            raise Http404("Image not found") from exc
        if not image_path.is_file():
            raise Http404("Image not found")

        content_type = mimetypes.guess_type(image_path.name)[0] or "application/octet-stream"
        return FileResponse(image_path.open("rb"), content_type=content_type)


class FindingsView(TemplateView):
    template_name = "findings/list.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        since, active_range, time_ctx = parse_time_filter(self.request)
        platform_q, active_platform, platform_ctx = parse_platform_filter(self.request)
        context.update(time_ctx)
        context.update(platform_ctx)
        
        findings = Finding.objects.select_related("session", "span", "turn")
        if since is not None:
            findings = findings.filter(created_at__gte=since)
        if platform_q:
            findings = findings.filter(session__in=Session.objects.filter(platform_q))
            
        if self.request.GET.get("status"):
            findings = findings.filter(status=self.request.GET["status"])
        if self.request.GET.get("severity"):
            findings = findings.filter(severity=self.request.GET["severity"])
        if self.request.GET.get("rule"):
            findings = findings.filter(rule_code=self.request.GET["rule"])
        context["page_obj"] = Paginator(findings.order_by("-created_at"), 50).get_page(self.request.GET.get("page"))
        context["rule_codes"] = Finding.objects.values_list("rule_code", flat=True).distinct()
        context["active_nav"] = "findings"
        return context


class CostView(TemplateView):
    template_name = "costs/index.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        since, active_range, time_ctx = parse_time_filter(self.request)
        platform_q, active_platform, platform_ctx = parse_platform_filter(self.request)
        context.update(time_ctx)
        context.update(platform_ctx)
        
        rows = cost_by_model(since=since, platform_q=platform_q)
        context["rows"] = rows
        context["chart_json"] = json.dumps(rows)
        context["metrics"] = overview_metrics(since=since, platform_q=platform_q)
        context["active_nav"] = "costs"
        return context


class DataHealthView(TemplateView):
    template_name = "data_health/index.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        since, active_range, time_ctx = parse_time_filter(self.request)
        platform_q, active_platform, platform_ctx = parse_platform_filter(self.request)
        context.update(time_ctx)
        context.update(platform_ctx)
        
        sources_qs = IngestionSource.objects.annotate(quarantine_count=Count("quarantined_events")).order_by("-last_seen_at")
        quarantine_qs = QuarantinedEvent.objects.select_related("source").order_by("-created_at")
        events_qs = RawEvent.objects.all()
        if since is not None:
            quarantine_qs = quarantine_qs.filter(created_at__gte=since)
            events_qs = events_qs.filter(occurred_at__gte=since)
            
        # DataHealth is a bit tricky with platforms, we'll filter events if they are tied to sessions
        # But wait, RawEvent's `payload__session_id` can be used. For simplicity, just provide the context here.
        
        context["sources"] = sources_qs
        context["quarantined"] = quarantine_qs[:50]
        context["event_count"] = events_qs.count()
        context["high_correlation"] = events_qs.filter(correlation_confidence="high").count()
        context["active_nav"] = "data-health"
        return context


class TemplateIntelligenceView(TemplateView):
    template_name = "templates/index.html"

    def get_template_names(self):
        target = self.request.GET.get("target")
        if self.request.headers.get("HX-Request") == "true":
            if target == "invocations_table":
                return ["templates/_invocations_table.html"]
            if target in ("table", "catalog_table"):
                return ["templates/_table_rows.html"]
            if target == "analysis_pairs_table":
                return ["templates/_analysis_pairs_table.html"]
        return super().get_template_names()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        since, active_range, time_ctx = parse_time_filter(self.request)
        platform_q, active_platform, platform_ctx = parse_platform_filter(self.request)
        context.update(time_ctx)
        context.update(platform_ctx)
        
        active_tab = self.request.GET.get("tab", "invocations")
        if active_tab not in ("invocations", "analysis", "catalog"):
            active_tab = "invocations"
        context["active_tab"] = active_tab
        context["active_nav"] = "templates"

        # 1. Invocations filter & pagination
        inv_q = self.request.GET.get("inv_q", "")
        inv_tool = self.request.GET.get("inv_tool", "")
        try:
            inv_page = int(self.request.GET.get("inv_page", 1))
        except (ValueError, TypeError):
            inv_page = 1

        invocations_result = get_mcp_template_invocations(
            query_filter=inv_q,
            tool_filter=inv_tool,
            page=inv_page,
            per_page=12,
        )

        # 2. Similarity & Uniqueness Analysis
        selected_bucket = self.request.GET.get("bucket", "")
        try:
            an_page = int(self.request.GET.get("an_page", 1))
        except (ValueError, TypeError):
            an_page = 1

        analysis_result = get_cross_similarity_analysis_data(
            selected_bucket=selected_bucket,
            page=an_page,
            per_page=15,
        )

        # 3. Catalog filter & pagination
        q = self.request.GET.get("q", "")
        category = self.request.GET.get("category", "")
        sort_by = self.request.GET.get("sort", "clones_desc")
        try:
            page = int(self.request.GET.get("page", 1))
        except (ValueError, TypeError):
            page = 1

        overview = get_templates_overview_metrics()
        charts_data = get_template_charts_data()
        table_result = filter_templates(
            query=q,
            category=category,
            sort_by=sort_by,
            page=page,
            per_page=15,
        )

        context.update(overview)
        context["charts_json"] = json.dumps(charts_data)
        context["templates_page"] = table_result
        context["current_q"] = q
        context["current_category"] = category
        context["current_sort"] = sort_by

        context["active_tab"] = active_tab
        context["invocations_page"] = invocations_result
        context["inv_stats"] = invocations_result.get("stats", {})
        context["inv_q"] = inv_q
        context["inv_tool"] = inv_tool
        context["inv_page"] = inv_page

        context["analysis_data"] = analysis_result
        context["analysis_json"] = json.dumps({
            "buckets": analysis_result.get("buckets", []),
            "stats": {
                "total_pairs": analysis_result.get("total_pairs", 0),
                "min": analysis_result.get("min_similarity", 0.0),
                "max": analysis_result.get("max_similarity", 0.0),
                "mean": analysis_result.get("mean_similarity", 0.0),
                "median": analysis_result.get("median_similarity", 0.0),
                "std_dev": analysis_result.get("std_dev", 0.0),
            }
        })
        context["selected_bucket"] = analysis_result.get("selected_bucket", "")

        context["active_nav"] = "templates"
        return context


class TemplateDetailModalView(TemplateView):
    template_name = "templates/_detail_modal.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        template_id = self.kwargs.get("template_id", "")
        template = get_template_by_id(template_id)
        context["template"] = template
        if template:
            context["pretty_snapshot"] = json.dumps(
                {"elements": template.get("elements", []), "links": template.get("links", [])},
                indent=2,
                ensure_ascii=False,
            )
        return context


class SessionExportPDFView(TemplateView):
    """Clean, print-optimized standalone HTML report for PDF export."""
    template_name = "sessions/export_report.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        session_id = self.kwargs.get("pk")
        session = Session.objects.get(pk=session_id)
        spans = list(session.spans.select_related("parent", "turn", "tool_call", "external_call", "model_step").order_by("started_at", "sequence_no"))
        origin = session.started_at or next((span.started_at for span in spans if span.started_at), None)
        
        waterfall = []
        measured_total = max(
            (
                (span.ended_at - origin).total_seconds() * 1000
                if origin and span.ended_at
                else float(span.duration_ms or 0)
            )
            for span in spans
        ) if spans else 0
        total = max(float(session.duration_ms or measured_total or 1), 1)
        for span in spans:
            offset = (span.started_at - origin).total_seconds() * 1000 if origin and span.started_at else 0
            waterfall.append({"span": span, "offset_pct": max(offset / total * 100, 0), "width_pct": max(float(span.duration_ms or 1) / total * 100, .35)})

        request_ids = {span.request_id for span in spans if span.request_id}
        raw_events_by_request: dict[str, list[dict]] = {}
        if request_ids:
            for event in RawEvent.objects.filter(payload__session_id=session.external_session_id).order_by("occurred_at", "id"):
                request_id = event.payload.get("request_id")
                if request_id in request_ids:
                    raw_events_by_request.setdefault(request_id, []).append(event.payload)

        operations_by_turn = {}
        for span in spans:
            if span.turn_id:
                operations_by_turn.setdefault(span.turn_id, []).append(operation_card(span, raw_events_by_request.get(span.request_id)))

        context["session"] = session
        context["waterfall"] = waterfall
        context["conversation"] = [
            {"turn": turn, "operations": operations_by_turn.get(turn.id, [])}
            for turn in session.turns.all()
        ]
        context["unassigned_operations"] = [
            operation_card(span, raw_events_by_request.get(span.request_id)) for span in spans if not span.turn_id
        ]
        context["findings"] = session.findings.select_related("span", "turn").order_by("-created_at")
        return context


class SessionExportJSONView(View):
    """Exports complete structured session trace as a JSON file attachment."""

    def get(self, request, pk):
        session = Session.objects.get(pk=pk)
        spans = list(session.spans.select_related("parent", "turn", "tool_call", "external_call", "model_step").order_by("started_at", "sequence_no"))
        
        request_ids = {span.request_id for span in spans if span.request_id}
        raw_events = list(RawEvent.objects.filter(payload__session_id=session.external_session_id).order_by("occurred_at", "id"))
        raw_events_by_request: dict[str, list[dict]] = {}
        for event in raw_events:
            request_id = event.payload.get("request_id")
            if request_id:
                raw_events_by_request.setdefault(request_id, []).append(event.payload)

        turns_data = []
        for turn in session.turns.all():
            turn_spans = [s for s in spans if s.turn_id == turn.id]
            turn_ops = [operation_card(s, raw_events_by_request.get(s.request_id)) for s in turn_spans]
            turns_data.append({
                "sequence_no": turn.sequence_no,
                "started_at": turn.started_at.isoformat() if turn.started_at else None,
                "duration_ms": turn.duration_ms,
                "question": turn.question,
                "answer": turn.answer,
                "input_tokens": turn.input_tokens,
                "output_tokens": turn.output_tokens,
                "cost_usd": float(turn.cost_usd) if turn.cost_usd is not None else None,
                "operations": [
                    {
                        "name": op.get("title"),
                        "kind": op.get("kind"),
                        "status": op.get("span").status,
                        "duration_ms": op.get("span").duration_ms,
                        "fields": op.get("fields", []),
                        "exchanges": op.get("exchanges", []),
                        "raw": op.get("raw"),
                    }
                    for op in turn_ops
                ],
            })

        unassigned_spans = [s for s in spans if not s.turn_id]
        unassigned_ops = [
            {
                "name": op.get("title"),
                "kind": op.get("kind"),
                "status": op.get("span").status,
                "duration_ms": op.get("span").duration_ms,
                "fields": op.get("fields", []),
                "exchanges": op.get("exchanges", []),
                "raw": op.get("raw"),
            }
            for op in [operation_card(s, raw_events_by_request.get(s.request_id)) for s in unassigned_spans]
        ]

        findings_data = [
            {
                "rule_code": f.rule_code,
                "severity": f.severity,
                "status": f.status,
                "title": f.display_title,
                "description": f.display_description,
                "recommendation": f.display_recommendation,
                "confidence": f.confidence,
                "wasted_ms": f.wasted_ms,
                "created_at": f.created_at.isoformat() if f.created_at else None,
            }
            for f in session.findings.all()
        ]

        export_payload = {
            "meta": {
                "exporter": "Jotform MCP Observability Suite",
                "version": "1.0",
                "exported_at": timezone.now().isoformat(),
            },
            "session": {
                "id": str(session.id),
                "external_session_id": session.external_session_id,
                "provider": session.provider,
                "model": session.model,
                "status": session.status,
                "started_at": session.started_at.isoformat() if session.started_at else None,
                "ended_at": session.ended_at.isoformat() if session.ended_at else None,
                "duration_ms": session.duration_ms,
                "correlation_confidence": session.correlation_confidence,
                "turns_count": session.turns.count(),
                "spans_count": len(spans),
                "findings_count": len(findings_data),
            },
            "conversation": turns_data,
            "unassigned_operations": unassigned_ops,
            "findings": findings_data,
            "raw_events": [e.payload for e in raw_events],
        }

        short_id = session.external_session_id[:12] if session.external_session_id else str(session.id)[:8]
        response = HttpResponse(
            json.dumps(export_payload, indent=2, ensure_ascii=False, default=str),
            content_type="application/json; charset=utf-8",
        )
        response["Content-Disposition"] = f'attachment; filename="jotform_mcp_session_{short_id}.json"'
        return response


class SessionExportMarkdownView(View):
    """Exports structured markdown summary of the session."""

    def get(self, request, pk):
        session = Session.objects.get(pk=pk)
        spans = list(session.spans.select_related("parent", "turn", "tool_call", "external_call", "model_step").order_by("started_at", "sequence_no"))
        
        request_ids = {span.request_id for span in spans if span.request_id}
        raw_events_by_request: dict[str, list[dict]] = {}
        if request_ids:
            for event in RawEvent.objects.filter(payload__session_id=session.external_session_id).order_by("occurred_at", "id"):
                request_id = event.payload.get("request_id")
                if request_id in request_ids:
                    raw_events_by_request.setdefault(request_id, []).append(event.payload)

        lines = [
            f"# Jotform MCP Session Report: `{session.external_session_id}`",
            "",
            "## Summary",
            f"- **Provider / Model**: `{session.provider or 'MCP'}` / `{session.model or 'Direct'}`",
            f"- **Status**: `{session.status}`",
            f"- **Duration**: `{session.duration_ms or 0:.0f} ms`",
            f"- **Turns**: `{session.turns.count()}`",
            f"- **Total Operations**: `{len(spans)}`",
            f"- **Started**: `{session.started_at.strftime('%Y-%m-%d %H:%M:%S') if session.started_at else '—'}`",
            "",
        ]

        if session.turns.exists():
            lines.append("## Conversation & Workflow Flow")
            for turn in session.turns.all():
                lines.append(f"### Turn #{turn.sequence_no}")
                lines.append(f"**User Prompt:**\n> {turn.question}\n")
                
                turn_spans = [s for s in spans if s.turn_id == turn.id]
                if turn_spans:
                    lines.append("#### MCP Operations:")
                    for s in turn_spans:
                        card = operation_card(s, raw_events_by_request.get(s.request_id))
                        lines.append(f"- **{card['title']}** (`{s.kind}`, {s.duration_ms or 0:.1f}ms, {s.status}):")
                        for ex in card.get("exchanges", []):
                            lines.append(f"  - *{ex['label']}*:\n```json\n{ex['raw']}\n```")
                    lines.append("")
                
                if turn.answer:
                    lines.append(f"**Agent Answer:**\n\n{turn.answer}\n")
                lines.append("---")

        unassigned_spans = [s for s in spans if not s.turn_id]
        if unassigned_spans:
            lines.append("## Standalone MCP Operations")
            for s in unassigned_spans:
                card = operation_card(s, raw_events_by_request.get(s.request_id))
                lines.append(f"- **{card['title']}** (`{s.kind}`, {s.duration_ms or 0:.1f}ms, {s.status})")

        short_id = session.external_session_id[:12] if session.external_session_id else str(session.id)[:8]
        response = HttpResponse("\n".join(lines), content_type="text/markdown; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="jotform_mcp_session_{short_id}.md"'
        return response


class SessionAIAnalysisView(View):
    def get(self, request, pk, *args, **kwargs):
        session = Session.objects.get(pk=pk)
        tools = ToolCall.objects.filter(span__session_id=session.id).select_related("span").order_by("span__started_at")
        
        tm_list = tool_metrics()
        tm_dict = {item["name"]: item for item in tm_list}
        tool_calls_payload = []
        for tc in tools:
            base = tm_dict.get(tc.tool_name, {})
            p50 = base.get("p50", 0)
            p95 = base.get("p95", 0)
            comp = "Yok"
            if p50 and tc.span.duration_ms:
                if tc.span.duration_ms > p95:
                    comp = "p95 sınırını aştı (Çok yavaş)"
                elif tc.span.duration_ms > p50:
                    comp = f"p50'den {tc.span.duration_ms - p50:.1f}ms yavaş"
                else:
                    comp = "p50'den daha hızlı"
                    
            tool_calls_payload.append({
                "tool": tc.tool_name,
                "duration_ms": tc.span.duration_ms,
                "status": tc.span.status,
                "historical_baseline": {"p50_ms": p50, "p95_ms": p95},
                "comparison": comp
            })
            
        findings = list(Finding.objects.filter(session=session).values("rule_code", "severity", "title"))
        timing = session_timing_breakdown(session.id)
        
        payload = {
            "session_id": session.external_session_id or str(session.id),
            "platform": session.platform or session.provider,
            "wall_duration_ms": timing.get("wall_duration_ms", 0),
            "active_duration_ms": timing.get("active_duration_ms", 0),
            "idle_gap_ms": timing.get("idle_gap_ms", 0),
            "tool_calls": tool_calls_payload,
            "http_breakdown": {
                "total_http_ms": timing.get("jotform_api_ms", 0),
                "llm_ms": timing.get("llm_active_ms", 0),
                "mcp_ms": timing.get("mcp_internal_ms", 0)
            },
            "findings": findings
        }
        
        prompt = f"""Sen Jotform MCP ve Agent sistemleri için uzman bir Observability & Telemetry Analistisin.

GÖREV:
Aşağıda verilen oturum telemetri verisini inceleyerek somut, verilere dayalı bir performans ve kök neden analizi sunmak.

ANALİZ KURALLARI:
1. Tahmin veya varsayım yapma; sadece sağlanan log, süre ve durum kodlarına dayan.
2. 50 ms altı süren başarısız tool çağrılarını "Hızlı Validasyon Hatası (Fast Rejection)" olarak işaretle ve tarihsel p50/p95 ortalamalarından muaf tut.
3. Tool sürelerini tarihsel p50 (medyan) ile kıyasla.
4. Dış API (Jotform Cloud) süresi ile yerel MCP/LLM süresini net olarak ayrıştır.
5. Varsa finding'lerin neden oluştuğunu ve nasıl önleneceğini belirt.

ÇIKTI FORMATI (Lütfen tam olarak bu Markdown başlıklarını ve madde işaretlerini kullan):
### ⏱️ Gecikme & Kıyaslama
* Tool'ların geçmiş p50 değerine göre durumu (Madde imleri ile listeleyin)

### 🌐 Sistem & API Durumu
* Zamanın nereye harcandığı, HTTP ve LLM süre kırılımları

### ⚠️ Anomali & Validasyon
* Varsa hızlı hatalar, gereksiz tekrarlar veya açık finding'ler

### 💡 Mühendislik Önerisi
* Somut olarak neyin optimize edilmesi gerektiği

PAYLOAD:
{json.dumps(payload, indent=2, ensure_ascii=False)}
"""
        
        user_question = request.GET.get("question", "").strip()
        if user_question:
            prompt += f"\n\nKULLANICI ÖZEL SORUSU:\n{user_question}\n(Lütfen analizine ek olarak bu soruya da spesifik bir yanıt ver.)"


        api_key = getattr(settings, "GEMINI_API_KEY", "")
        if not api_key:
            html = "<div class='notice error' style='margin-bottom:0;'><strong>GEMINI_API_KEY bulunamadı.</strong> Lütfen <code>config/settings.py</code> veya ortam değişkenlerini kontrol edin.</div>"
            return HttpResponse(html)

        selected_model = request.GET.get("model", "gemini-3.5-flash-lite").strip() or "gemini-3.5-flash-lite"
        try:
            AITelemetryUsage.increment(selected_model)
            genai.configure(api_key=api_key, transport="rest")
            model = genai.GenerativeModel(selected_model)
            response = model.generate_content(prompt)
            md_text = response.text
            html_content = markdown.markdown(md_text, extensions=['fenced_code', 'tables'])
        except Exception as e:
            html_content = f"<div class='notice error' style='margin-bottom:0;'><strong>Analiz Hatası ({selected_model}):</strong> {e}</div>"

        return HttpResponse(html_content)
class FeatureRequestsView(TemplateView):
    template_name = "feature_requests/index.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        try:
            score_threshold = float(self.request.GET.get("threshold", "0.68"))
        except ValueError:
            score_threshold = 0.68

        try:
            cluster_threshold = float(self.request.GET.get("cluster_threshold", "0.50"))
        except ValueError:
            cluster_threshold = 0.50

        tool_calls = ToolCall.objects.filter(tool_name="search_workflow_templates").select_related("span")
        
        gaps = []
        for tc in tool_calls:
            args = tc.arguments or {}
            query = args.get("query", "")
            if not query:
                continue
                
            res = tc.result or {}
            score = 1.0
            structured = res.get("structured_content")
            if structured and isinstance(structured, dict):
                templates = structured.get("templates", [])
                if templates and isinstance(templates, list):
                    score = templates[0].get("score", 1.0)
            
            if score < score_threshold:
                gaps.append({
                    "query": query,
                    "score": score,
                    "timestamp": tc.span.started_at,
                    "session_id": tc.span.session.id if tc.span and tc.span.session else None,
                })

        unique_queries = list(set(gap["query"] for gap in gaps))
        embeddings_map = {}
        try:
            from fastembed import TextEmbedding
            model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
            vectors = list(model.embed(unique_queries))
            for i, q in enumerate(unique_queries):
                embeddings_map[q] = vectors[i]
        except Exception:
            pass

        def get_words(s):
            return set(w.lower() for w in str(s).split() if len(w) > 2)

        def similarity(a, b):
            sa, sb = get_words(a), get_words(b)
            if not sa and not sb: 
                j_sim = 1.0
            else:
                intersection = len(sa.intersection(sb))
                union = len(sa) + len(sb) - intersection
                j_sim = intersection / union if union else 0.0
            
            e_sim = 0.0
            if a in embeddings_map and b in embeddings_map:
                vec_a = embeddings_map[a]
                vec_b = embeddings_map[b]
                dot_product = sum(x * y for x, y in zip(vec_a, vec_b))
                norm_a = sum(x*x for x in vec_a) ** 0.5
                norm_b = sum(x*x for x in vec_b) ** 0.5
                if norm_a and norm_b:
                    e_sim = dot_product / (norm_a * norm_b)
            else:
                return j_sim, j_sim, 0.0

            return (j_sim * 0.3) + (e_sim * 0.7), j_sim, e_sim

        clusters = []
        for gap in gaps:
            matched = False
            for cluster in clusters:
                sim_score, j_sim, e_sim = similarity(cluster["base_query"], gap["query"])
                if sim_score >= cluster_threshold:
                    # We create a copy of gap to avoid modifying the original if we need it, but here it's fine.
                    gap_with_sim = gap.copy()
                    gap_with_sim["hybrid_sim"] = sim_score
                    gap_with_sim["j_sim"] = j_sim
                    gap_with_sim["e_sim"] = e_sim
                    cluster["items"].append(gap_with_sim)
                    matched = True
                    break
            if not matched:
                gap_with_sim = gap.copy()
                gap_with_sim["hybrid_sim"] = 1.0
                gap_with_sim["j_sim"] = 1.0
                gap_with_sim["e_sim"] = 1.0
                clusters.append({
                    "base_query": gap["query"],
                    "items": [gap_with_sim]
                })

        clusters.sort(key=lambda c: len(c["items"]), reverse=True)

        # Calculate new statistics
        total_clusters = len(clusters)
        avg_cluster_size = len(gaps) / total_clusters if total_clusters > 0 else 0
        total_score = sum(g["score"] for g in gaps)
        avg_score = total_score / len(gaps) if gaps else 0.0
        unique_sessions = len(set(g["session_id"] for g in gaps if g["session_id"]))

        # Add cluster-level stats
        for cluster in clusters:
            cluster_scores = [item["score"] for item in cluster["items"]]
            cluster["avg_score"] = sum(cluster_scores) / len(cluster_scores) if cluster_scores else 0
            cluster["unique_sessions"] = len(set(item["session_id"] for item in cluster["items"] if item["session_id"]))

        # Prepare chart data for ECharts (Top 10 biggest clusters)
        chart_data = {
            "categories": [c["base_query"][:30] + ("..." if len(c["base_query"]) > 30 else "") for c in clusters[:10]][::-1],
            "values": [len(c["items"]) for c in clusters[:10]][::-1]
        }
        
        import json

        context.update({
            "active_nav": "feature-requests",
            "score_threshold": score_threshold,
            "cluster_threshold": cluster_threshold,
            "clusters": clusters,
            "total_gaps": len(gaps),
            "total_clusters": total_clusters,
            "avg_cluster_size": avg_cluster_size,
            "avg_score": avg_score,
            "unique_sessions": unique_sessions,
            "chart_data_json": json.dumps(chart_data),
        })
        return context


class FunctionTracesView(TemplateView):
    template_name = "function_traces/index.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        
        q = self.request.GET.get("q", "").strip()
        status_filter = self.request.GET.get("status", "")
        model_filter = self.request.GET.get("model", "")
        
        from django.db.models import Case, When, Value, CharField
        
        platform_case = Case(
            When(Q(session__provider__icontains="test") | Q(session__provider__icontains="ci") | Q(session__model__icontains="test") | Q(session__model__icontains="pytest"), then=Value("🧪 Tests / CI")),
            When(Q(session__provider__icontains="anthropic") | Q(session__provider__icontains="claude") | Q(session__model__icontains="claude"), then=Value("Claude")),
            When(Q(session__provider__icontains="gemini") | Q(session__provider__icontains="google") | Q(session__model__icontains="gemini"), then=Value("Gemini")),
            When(Q(session__provider__icontains="openai") | Q(session__provider__icontains="chatgpt") | Q(session__provider__icontains="gpt") | Q(session__model__icontains="gpt") | Q(session__model__icontains="o1") | Q(session__model__icontains="o3"), then=Value("ChatGPT / GPT")),
            default=Value("MCP Direct"),
            output_field=CharField()
        )
        
        spans = Span.objects.filter(kind="tool").annotate(platform_display=platform_case)
        
        if q:
            spans = spans.filter(name__icontains=q)
        if status_filter:
            spans = spans.filter(status=status_filter)
        if model_filter:
            spans = spans.filter(platform_display=model_filter)
            
        total_calls = spans.count()
        total_errors = spans.filter(status="error").count()
        error_rate = (total_errors / total_calls * 100) if total_calls > 0 else 0
        aggs = spans.aggregate(Avg("duration_ms"), Sum("duration_ms"))
        avg_duration = aggs["duration_ms__avg"] or 0
        total_duration = aggs["duration_ms__sum"] or 0
        
        stats = spans.values("name").annotate(
            count=Count("id"),
            total_duration_ms=Sum("duration_ms"),
            avg_duration_ms=Avg("duration_ms"),
            error_count=Count("id", filter=Q(status="error"))
        ).order_by("-count")

        model_stats = spans.values("platform_display").annotate(
            count=Count("id"),
            avg_duration_ms=Avg("duration_ms"),
            error_count=Count("id", filter=Q(status="error"))
        ).order_by("-count")
        
        for m in model_stats:
            m["display"] = m["platform_display"]
            
        all_models = ["Claude", "Gemini", "ChatGPT / GPT", "MCP Direct", "🧪 Tests / CI"]

        recent_calls = spans.select_related("session", "tool_call").order_by("-started_at")[:200]
        
        context.update({
            "active_nav": "function-traces",
            "stats": stats,
            "model_stats": model_stats,
            "recent_calls": recent_calls,
            "total_calls": total_calls,
            "error_rate": error_rate,
            "avg_duration": avg_duration,
            "total_duration": total_duration,
            "q": q,
            "status_filter": status_filter,
            "model_filter": model_filter,
            "all_models": all_models,
        })
        return context


from collections import Counter
from datetime import timedelta
from django.utils import timezone

class GeneratedElementsView(TemplateView):
    template_name = "generated_elements/index.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        
        time_filter = self.request.GET.get("time", "all")
        model_filter = self.request.GET.get("model", "")
        
        from django.db.models import Case, When, Value, CharField, Q
        
        platform_case = Case(
            When(Q(span__session__provider__icontains="test") | Q(span__session__provider__icontains="ci") | Q(span__session__model__icontains="test") | Q(span__session__model__icontains="pytest"), then=Value("🧪 Tests / CI")),
            When(Q(span__session__provider__icontains="anthropic") | Q(span__session__provider__icontains="claude") | Q(span__session__model__icontains="claude"), then=Value("Claude")),
            When(Q(span__session__provider__icontains="gemini") | Q(span__session__provider__icontains="google") | Q(span__session__model__icontains="gemini"), then=Value("Gemini")),
            When(Q(span__session__provider__icontains="openai") | Q(span__session__provider__icontains="chatgpt") | Q(span__session__provider__icontains="gpt") | Q(span__session__model__icontains="gpt") | Q(span__session__model__icontains="o1") | Q(span__session__model__icontains="o3"), then=Value("ChatGPT / GPT")),
            default=Value("MCP Direct"),
            output_field=CharField()
        )
        
        tool_calls = ToolCall.objects.filter(tool_name="build_workflow_bulk").select_related("span__session").annotate(platform_display=platform_case)
        
        if time_filter == "24h":
            tool_calls = tool_calls.filter(span__started_at__gte=timezone.now() - timedelta(hours=24))
        elif time_filter == "7d":
            tool_calls = tool_calls.filter(span__started_at__gte=timezone.now() - timedelta(days=7))
        elif time_filter == "30d":
            tool_calls = tool_calls.filter(span__started_at__gte=timezone.now() - timedelta(days=30))
            
        if model_filter:
            tool_calls = tool_calls.filter(platform_display=model_filter)
            
        tool_calls = tool_calls.order_by("-span__started_at")
        
        type_counts = Counter()
        recent_workflows = []
        
        for tc in tool_calls:
            args = tc.arguments or {}
            steps = args.get("steps", [])
            intent = args.get("intent", "No intent provided")
            reason = args.get("reason", "No reason provided")
            
            workflow_types = []
            if isinstance(steps, list):
                for step in steps:
                    if isinstance(step, dict) and "type" in step:
                        step_type = step["type"]
                        type_counts[step_type] += 1
                        workflow_types.append(step_type)
            
            if workflow_types and len(recent_workflows) < 100:
                platform = tc.span.session.platform_display if tc.span and tc.span.session else "Unknown"
                model_name = tc.span.session.model if tc.span and tc.span.session else ""
                if "gpt-4o" in model_name.lower(): platform = "GPT-4o"
                elif "claude-3-5" in model_name.lower(): platform = "Claude 3.5 Sonnet"
                
                recent_workflows.append({
                    "timestamp": tc.span.started_at if tc.span else None,
                    "platform": platform,
                    "intent": intent,
                    "reason": reason,
                    "step_count": len(workflow_types),
                    "types": workflow_types,
                })

        total_elements = sum(type_counts.values())
        chart_data = [{"name": name, "value": count} for name, count in type_counts.items()]
        chart_data.sort(key=lambda x: x["value"], reverse=True)
        
        # Unique models for the dropdown
        all_models = ["Claude", "Gemini", "ChatGPT / GPT", "MCP Direct", "🧪 Tests / CI"]
        
        import json
        
        context.update({
            "active_nav": "generated-elements",
            "type_counts": chart_data,
            "total_elements": total_elements,
            "recent_workflows": recent_workflows,
            "chart_data_json": json.dumps(chart_data),
            "time_filter": time_filter,
            "model_filter": model_filter,
            "all_models": all_models,
        })
        return context

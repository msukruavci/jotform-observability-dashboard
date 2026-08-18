from __future__ import annotations

import json

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, F, Q, Sum
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.views.generic import DetailView, TemplateView

from apps.findings.models import Annotation, Finding
from apps.ingestion.models import IngestionSource, QuarantinedEvent, RawEvent
from apps.traces.models import Session, Span, ToolCall, Turn

from .metrics import cost_by_model, overview_metrics, session_timeseries, tool_metrics
from .presentation import operation_card, pretty_payload


class OverviewView(TemplateView):
    template_name = "dashboard/overview.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(overview_metrics())
        context["timeseries_json"] = json.dumps(session_timeseries())
        context["slow_sessions"] = Session.objects.exclude(duration_ms=None).order_by("-duration_ms")[:6]
        context["top_findings"] = Finding.objects.filter(status="open").select_related("session").order_by("-severity", "-created_at")[:6]
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
        query = Session.objects.annotate(
            turn_count=Count("turns", distinct=True),
            span_count=Count("spans", distinct=True),
            tool_count=Count("spans", filter=Q(spans__kind="tool"), distinct=True),
            finding_count=Count("findings", distinct=True),
        ).filter(Q(turn_count__gt=0) | Q(span_count__gt=0))
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
        ), 25)
        context["page_obj"] = paginator.get_page(params.get("page"))
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
        operations_by_turn = {}
        for span in spans:
            if span.turn_id:
                operations_by_turn.setdefault(span.turn_id, []).append(operation_card(span, raw_events_by_request.get(span.request_id)))
        context["conversation"] = [
            {"turn": turn, "operations": operations_by_turn.get(turn.id, [])}
            for turn in session.turns.all()
        ]
        context["unassigned_operations"] = [
            operation_card(span, raw_events_by_request.get(span.request_id)) for span in spans if not span.turn_id
        ][-50:]
        context["findings"] = session.findings.select_related("span", "turn").order_by("-created_at")
        context["raw_events"] = [
            {"event": event, "pretty_payload": pretty_payload(event.payload)}
            for event in RawEvent.objects.filter(payload__session_id=session.external_session_id).order_by("occurred_at")[:50]
        ]
        context["active_nav"] = "sessions"
        return context


class ToolIntelligenceView(TemplateView):
    template_name = "tools/overview.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        metrics = tool_metrics()
        context["tool_metrics"] = metrics
        context["chart_json"] = json.dumps(metrics)
        context["active_nav"] = "tools"
        return context


class FindingsView(TemplateView):
    template_name = "findings/list.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        findings = Finding.objects.select_related("session", "span", "turn")
        if self.request.GET.get("status"):
            findings = findings.filter(status=self.request.GET["status"])
        if self.request.GET.get("severity"):
            findings = findings.filter(severity=self.request.GET["severity"])
        if self.request.GET.get("rule"):
            findings = findings.filter(rule_code=self.request.GET["rule"])
        context["page_obj"] = Paginator(findings.order_by("-created_at"), 30).get_page(self.request.GET.get("page"))
        context["rule_codes"] = Finding.objects.values_list("rule_code", flat=True).distinct()
        context["active_nav"] = "findings"
        return context


class CostView(TemplateView):
    template_name = "costs/index.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        rows = cost_by_model()
        context["rows"] = rows
        context["chart_json"] = json.dumps(rows)
        context["metrics"] = overview_metrics()
        context["active_nav"] = "costs"
        return context


class DataHealthView(TemplateView):
    template_name = "data_health/index.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["sources"] = IngestionSource.objects.annotate(quarantine_count=Count("quarantined_events")).order_by("-last_seen_at")
        context["quarantined"] = QuarantinedEvent.objects.select_related("source").order_by("-created_at")[:30]
        context["event_count"] = RawEvent.objects.count()
        context["high_correlation"] = RawEvent.objects.filter(correlation_confidence="high").count()
        context["active_nav"] = "data-health"
        return context

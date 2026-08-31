from __future__ import annotations

import math
from collections import Counter, defaultdict
from decimal import Decimal

from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncDate

from apps.findings.models import Finding
from apps.ingestion.models import QuarantinedEvent, RawEvent
from apps.traces.models import Session, ToolCall, Turn, UsageRecord


def percentile(values, percent: float) -> float | None:
    ordered = sorted(float(value) for value in values if value is not None)
    if not ordered:
        return None
    index = (len(ordered) - 1) * percent
    lower, upper = math.floor(index), math.ceil(index)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def overview_metrics(since: datetime | None = None, platform_q: Q | None = None) -> dict:
    sessions = Session.objects.all()
    turns = Turn.objects.all()
    findings = Finding.objects.filter(status="open")
    raw_events = RawEvent.objects.all()
    quarantined = QuarantinedEvent.objects.all()

    if since is not None:
        sessions = sessions.filter(started_at__gte=since)
        turns = turns.filter(started_at__gte=since)
        findings = findings.filter(created_at__gte=since)
        raw_events = raw_events.filter(occurred_at__gte=since)
        quarantined = quarantined.filter(created_at__gte=since)

    if platform_q:
        sessions = sessions.filter(platform_q)
        turns = turns.filter(session__in=Session.objects.filter(platform_q))
        findings = findings.filter(session__in=Session.objects.filter(platform_q))

    durations = list(sessions.values_list("duration_ms", flat=True))
    known_cost = turns.exclude(cost_usd=None).aggregate(value=Sum("cost_usd"))["value"] or Decimal("0")
    total_turns = turns.count()
    known_cost_turns = turns.exclude(cost_usd=None).count()
    return {
        "session_count": sessions.count(),
        "success_rate": round(sessions.filter(status="ok").count() * 100 / sessions.count(), 1) if sessions.count() else 0,
        "p95_duration_ms": percentile(durations, .95) or 0,
        "total_cost_usd": known_cost,
        "open_findings": findings.count(),
        "event_count": raw_events.count(),
        "quarantine_count": quarantined.count(),
        "cost_coverage": round(known_cost_turns * 100 / total_turns, 1) if total_turns else 0,
    }


def session_timeseries(since: datetime | None = None, platform_q: Q | None = None) -> list[dict]:
    sessions = Session.objects.exclude(started_at=None)
    if since is not None:
        sessions = sessions.filter(started_at__gte=since)
    if platform_q:
        sessions = sessions.filter(platform_q)

    rows = sessions.annotate(day=TruncDate("started_at")).values("day").annotate(count=Count("id")).order_by("day")
    return [{"day": row["day"].isoformat(), "count": row["count"]} for row in rows]


def tool_metrics(since: datetime | None = None, platform_q: Q | None = None) -> list[dict]:
    tool_calls = ToolCall.objects.select_related("span", "span__session")
    if since is not None:
        tool_calls = tool_calls.filter(span__started_at__gte=since)
    if platform_q:
        tool_calls = tool_calls.filter(span__session__in=Session.objects.filter(platform_q))

    grouped: dict[str, list[ToolCall]] = defaultdict(list)
    for call in tool_calls:
        grouped[call.tool_name].append(call)
    findings_qs = Finding.objects.filter(status="open").exclude(span=None)
    if since is not None:
        findings_qs = findings_qs.filter(created_at__gte=since)
    if platform_q:
        findings_qs = findings_qs.filter(session__in=Session.objects.filter(platform_q))

    finding_counts = Counter(findings_qs.values_list("span__name", flat=True))
    result = []
    for name, calls in grouped.items():
        durations = [call.span.duration_ms for call in calls if call.span and call.span.duration_ms is not None]
        errors = sum(call.is_error for call in calls)
        result.append({
            "name": name, "count": len(calls), "p50": percentile(durations, .5) or 0,
            "p95": percentile(durations, .95) or 0,
            "error_rate": round(errors * 100 / len(calls), 1) if calls else 0,
            "findings": finding_counts[name],
            "avg_result_bytes": round(sum(call.result_bytes for call in calls) / len(calls)) if calls else 0,
        })
    return sorted(result, key=lambda item: (-item["count"], item["name"]))


def cost_by_model(since: datetime | None = None, platform_q: Q | None = None) -> list[dict]:
    turns = Turn.objects.exclude(cost_usd=None)
    if since is not None:
        turns = turns.filter(started_at__gte=since)
    if platform_q:
        turns = turns.filter(session__in=Session.objects.filter(platform_q))

    rows = turns.values("session__provider", "session__model").annotate(cost=Sum("cost_usd"), turns=Count("id")).order_by("-cost")
    return [{"provider": row["session__provider"], "model": row["session__model"], "cost": float(row["cost"]), "turns": row["turns"]} for row in rows]


def platform_breakdown_metrics(since: datetime | None = None, platform_q: Q | None = None) -> list[dict]:
    platforms = [
        {
            "key": "claude",
            "name": "Claude (Anthropic)",
            "icon": "🟣",
            "badge_bg": "rgba(217, 119, 6, 0.15)",
            "badge_color": "#f59e0b",
            "filter": Q(provider__icontains="anthropic") | Q(provider__icontains="claude") | Q(model__icontains="claude"),
        },
        {
            "key": "gemini",
            "name": "Gemini (Google)",
            "icon": "🔵",
            "badge_bg": "rgba(37, 99, 235, 0.15)",
            "badge_color": "#3b82f6",
            "filter": Q(provider__icontains="gemini") | Q(provider__icontains="google") | Q(model__icontains="gemini"),
        },
        {
            "key": "gpt",
            "name": "ChatGPT / GPT (OpenAI)",
            "icon": "🟢",
            "badge_bg": "rgba(22, 163, 74, 0.15)",
            "badge_color": "#22c55e",
            "filter": Q(provider__icontains="openai") | Q(provider__icontains="gpt") | Q(provider__icontains="chatgpt") | Q(model__icontains="gpt") | Q(model__icontains="o1") | Q(model__icontains="o3"),
        },
        {
            "key": "mcp",
            "name": "Direct MCP",
            "icon": "⚙️",
            "badge_bg": "rgba(148, 163, 184, 0.15)",
            "badge_color": "#94a3b8",
            "filter": (Q(provider="mcp") | Q(provider="")) & ~Q(provider__icontains="test") & ~Q(model__icontains="test") & ~Q(model__icontains="claude") & ~Q(model__icontains="gemini") & ~Q(model__icontains="gpt"),
        },
        {
            "key": "test",
            "name": "Tests / CI (Mock)",
            "icon": "🧪",
            "badge_bg": "rgba(239, 68, 68, 0.15)",
            "badge_color": "#ef4444",
            "filter": Q(provider__icontains="test") | Q(provider__icontains="ci") | Q(model__icontains="test") | Q(model__icontains="pytest"),
        },
    ]

    base_qs = Session.objects.all()
    if since is not None:
        base_qs = base_qs.filter(started_at__gte=since)
    if platform_q:
        base_qs = base_qs.filter(platform_q)

    out = []
    total_cost_all = Turn.objects.all()
    if since is not None:
        total_cost_all = total_cost_all.filter(started_at__gte=since)
    if platform_q:
        total_cost_all = total_cost_all.filter(session__in=Session.objects.filter(platform_q))

    for p in platforms:
        q_filter = p["filter"]
        qs = base_qs.filter(q_filter)
        count = qs.count()
        err_count = qs.filter(status="error").count()
        success_rate = round((count - err_count) * 100 / count, 1) if count else 0
        durations = [d for d in qs.values_list("duration_ms", flat=True) if d is not None]
        p95 = percentile(durations, 0.95) or 0
        
        turn_qs = total_cost_all.filter(session__in=qs)
        cost = turn_qs.aggregate(value=Sum("cost_usd"))["value"] or Decimal("0")
        
        out.append({
            "key": p["key"],
            "name": p["name"],
            "icon": p["icon"],
            "badge_bg": p["badge_bg"],
            "badge_color": p["badge_color"],
            "session_count": count,
            "success_rate": success_rate,
            "avg_duration_ms": round(sum(durations) / len(durations), 0) if durations else 0,
            "turns_count": turn_qs.count(),
            "cost_usd": float(cost),
        })

    out.sort(key=lambda x: x["session_count"], reverse=True)
    return out

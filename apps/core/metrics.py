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


def overview_metrics() -> dict:
    sessions = Session.objects.all()
    durations = list(sessions.values_list("duration_ms", flat=True))
    known_cost = Turn.objects.exclude(cost_usd=None).aggregate(value=Sum("cost_usd"))["value"] or Decimal("0")
    total_turns = Turn.objects.count()
    known_cost_turns = Turn.objects.exclude(cost_usd=None).count()
    return {
        "session_count": sessions.count(),
        "success_rate": round(sessions.filter(status="ok").count() * 100 / sessions.count(), 1) if sessions.count() else 0,
        "p95_duration_ms": percentile(durations, .95) or 0,
        "total_cost_usd": known_cost,
        "open_findings": Finding.objects.filter(status="open").count(),
        "event_count": RawEvent.objects.count(),
        "quarantine_count": QuarantinedEvent.objects.count(),
        "cost_coverage": round(known_cost_turns * 100 / total_turns, 1) if total_turns else 0,
    }


def session_timeseries() -> list[dict]:
    rows = Session.objects.exclude(started_at=None).annotate(day=TruncDate("started_at")).values("day").annotate(count=Count("id")).order_by("day")
    return [{"day": row["day"].isoformat(), "count": row["count"]} for row in rows]


def tool_metrics() -> list[dict]:
    grouped: dict[str, list[ToolCall]] = defaultdict(list)
    for call in ToolCall.objects.select_related("span"):
        grouped[call.tool_name].append(call)
    finding_counts = Counter(Finding.objects.filter(status="open").exclude(span=None).values_list("span__name", flat=True))
    result = []
    for name, calls in grouped.items():
        durations = [call.span.duration_ms for call in calls]
        errors = sum(call.is_error for call in calls)
        result.append({
            "name": name, "count": len(calls), "p50": percentile(durations, .5) or 0,
            "p95": percentile(durations, .95) or 0,
            "error_rate": round(errors * 100 / len(calls), 1),
            "findings": finding_counts[name],
            "avg_result_bytes": round(sum(call.result_bytes for call in calls) / len(calls)),
        })
    return sorted(result, key=lambda item: (-item["count"], item["name"]))


def cost_by_model() -> list[dict]:
    rows = Turn.objects.exclude(cost_usd=None).values("session__provider", "session__model").annotate(cost=Sum("cost_usd"), turns=Count("id")).order_by("-cost")
    return [{"provider": row["session__provider"], "model": row["session__model"], "cost": float(row["cost"]), "turns": row["turns"]} for row in rows]


def platform_breakdown_metrics() -> list[dict]:
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

    result = []
    base_sessions = Session.objects.filter(Q(turns__isnull=False) | Q(spans__isnull=False)).distinct()
    for p in platforms:
        sess = base_sessions.filter(p["filter"])
        count = sess.count()
        durations = [d for d in sess.exclude(duration_ms=None).values_list("duration_ms", flat=True) if d]
        ok_count = sess.filter(status="ok").count()
        cost = Turn.objects.filter(session__in=sess).exclude(cost_usd=None).aggregate(total=Sum("cost_usd"))["total"] or Decimal("0")
        turns_count = Turn.objects.filter(session__in=sess).count()
        result.append({
            "key": p["key"],
            "name": p["name"],
            "icon": p["icon"],
            "badge_bg": p["badge_bg"],
            "badge_color": p["badge_color"],
            "session_count": count,
            "success_rate": round(ok_count * 100 / count, 1) if count else 0.0,
            "avg_duration_ms": round(sum(durations) / len(durations), 0) if durations else 0,
            "turns_count": turns_count,
            "cost_usd": cost,
        })
    return result



from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from decimal import Decimal
from typing import Any

from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.db.models import Count, Max, Q, Sum

from apps.findings.models import Finding
from apps.core.cache_utils import get_cached_value, set_cached_value
from apps.traces.models import ExternalCall, Session, Span, ToolCall, Turn
from apps.core.metrics import percentile
from apps.core.presentation import clean_and_normalize_data, human_value, pretty_payload, result_summary


TOOL_METADATA: dict[str, dict[str, Any]] = {
    "build_workflow_bulk": {
        "title": "Build Workflow Bulk",
        "category": "Workflow Creation & Mutation",
        "icon": "⚡",
        "badge_color": "#ff6100",
        "description": "Primary high-performance tool for building complete workflows in one bulk operation. Generates trigger form via AI, creates workflow draft, resolves field bindings, auto-lays out graph (DAG), and connects all steps.",
    },
    "search_workflow_templates": {
        "title": "Search Workflow Templates",
        "category": "Template Discovery & RAG",
        "icon": "🔍",
        "badge_color": "#3f6ad8",
        "description": "Semantic search over 300+ pre-built workflow blueprints using vector embeddings and cosine similarity. Discovers domain architectures and best practices.",
    },
    "show_workflow": {
        "title": "Show Workflow",
        "category": "UI Visualizer",
        "icon": "🎨",
        "badge_color": "#2da44e",
        "description": "Opens the interactive read-only workflow preview visualizer. Called as the final presentation step to display the authoritative workflow canvas.",
    },
    "show_workflows": {
        "title": "Show Workflows Catalog",
        "category": "UI Visualizer",
        "icon": "🗂️",
        "badge_color": "#7c5ac7",
        "description": "Displays the user's workflow catalog in an interactive card grid with search, tags, and status filtering.",
    },
    "get_workflow": {
        "title": "Get Workflow",
        "category": "Workflow Inspection",
        "icon": "📖",
        "badge_color": "#0ea5e9",
        "description": "Fetches complete live workflow graph, elements, links, and trigger form fields from Jotform Cloud API.",
    },
    "create_form_with_ai": {
        "title": "Create Form with AI",
        "category": "Form Management",
        "icon": "📝",
        "badge_color": "#10b981",
        "description": "Generates a standalone Jotform form with questions, options, and validation based on a natural language prompt.",
    },
    "create_workflow": {
        "title": "Create Workflow Draft",
        "category": "Workflow Creation & Mutation",
        "icon": "➕",
        "badge_color": "#f59e0b",
        "description": "Creates an empty unpublished workflow draft on Jotform Cloud.",
    },
    "create_workflow_with_ai_form": {
        "title": "Create Workflow with AI Form",
        "category": "Workflow Creation & Mutation",
        "icon": "🪄",
        "badge_color": "#ec4899",
        "description": "Creates both an AI trigger form and an associated workflow shell in a single step.",
    },
    "get_form_fields": {
        "title": "Get Form Fields",
        "category": "Form Management",
        "icon": "📋",
        "badge_color": "#8b5cf6",
        "description": "Retrieves questions and field structures from a specific Jotform form ID.",
    },
    "list_workflows": {
        "title": "List Workflows",
        "category": "Workflow Inspection",
        "icon": "📜",
        "badge_color": "#64748b",
        "description": "Lists all workflows in the user's Jotform account with ID, title, and status.",
    },
    "list_forms": {
        "title": "List Forms",
        "category": "Form Management",
        "icon": "📑",
        "badge_color": "#64748b",
        "description": "Lists available forms in the user's Jotform account.",
    },
    "publish_workflow": {
        "title": "Publish Workflow",
        "category": "Workflow Safety & Release",
        "icon": "🚀",
        "badge_color": "#16a34a",
        "description": "Publishes a workflow draft, activating triggers and live email notifications.",
    },
    "delete_workflow": {
        "title": "Delete Workflow",
        "category": "Workflow Safety & Release",
        "icon": "🗑️",
        "badge_color": "#dc2626",
        "description": "Deletes a workflow from Jotform Cloud with safety audit and snapshot backup.",
    },
    "delete_step": {
        "title": "Delete Step",
        "category": "Workflow Creation & Mutation",
        "icon": "✂️",
        "badge_color": "#ef4444",
        "description": "Deletes an individual step and cleans up dangling transitions.",
    },
    "add_step": {
        "title": "Add Step",
        "category": "Workflow Creation & Mutation",
        "icon": "➕",
        "badge_color": "#f97316",
        "description": "Low-level tool to insert a single step into an existing workflow canvas.",
    },
    "connect_steps": {
        "title": "Connect Steps",
        "category": "Workflow Creation & Mutation",
        "icon": "🔗",
        "badge_color": "#06b6d4",
        "description": "Low-level tool to create a directed transition link between two workflow steps.",
    },
    "disconnect_steps": {
        "title": "Disconnect Steps",
        "category": "Workflow Creation & Mutation",
        "icon": "⛓️‍💥",
        "badge_color": "#f43f5e",
        "description": "Low-level tool to remove a transition link between steps.",
    },
    "update_step": {
        "title": "Update Step",
        "category": "Workflow Creation & Mutation",
        "icon": "✏️",
        "badge_color": "#eab308",
        "description": "Updates configuration, approvers, emails, or rules for an existing step.",
    },
    "list_step_types": {
        "title": "List Step Types",
        "category": "Discovery & Schemas",
        "icon": "🧩",
        "badge_color": "#8b5cf6",
        "description": "Returns the registry of available workflow step types and supported capabilities.",
    },
    "get_step_schema": {
        "title": "Get Step Schema",
        "category": "Discovery & Schemas",
        "icon": "📐",
        "badge_color": "#6366f1",
        "description": "Retrieves JSON schema and configuration requirements for specific step types.",
    },
    "get_step_details": {
        "title": "Get Step Details",
        "category": "Workflow Inspection",
        "icon": "🔍",
        "badge_color": "#0284c7",
        "description": "Reads full configuration details of a single step in a workflow.",
    },
    "get_workflow_template": {
        "title": "Get Workflow Template",
        "category": "Template Discovery & RAG",
        "icon": "📦",
        "badge_color": "#4f46e5",
        "description": "Inspects full blueprint snapshot, element layout, and links for a specific template.",
    },
    "list_workflow_revisions": {
        "title": "List Workflow Revisions",
        "category": "Revisions & Safety",
        "icon": "🕰️",
        "badge_color": "#059669",
        "description": "Lists local backup revision snapshots and restore points for a workflow.",
    },
    "restore_workflow_revision": {
        "title": "Restore Workflow Revision",
        "category": "Revisions & Safety",
        "icon": "⏪",
        "badge_color": "#10b981",
        "description": "Rolls back a workflow to an earlier revision snapshot atomically.",
    },
    "inspect_workflow_gaps": {
        "title": "Inspect Workflow Gaps",
        "category": "Quality & Diagnostics",
        "icon": "🩺",
        "badge_color": "#9333ea",
        "description": "Performs preflight checks and semantic validation to spot missing links or orphaned nodes.",
    },
}
_TOOLS_OVERVIEW_CACHE: dict[tuple, dict[str, Any]] = {}


def is_public_mcp_tool_name(tool_name: str) -> bool:
    return bool(tool_name) and not tool_name.startswith("mcp_server.")


def _tool_calls_signature() -> tuple:
    calls = ToolCall.objects.aggregate(count=Count("span_id"), max_span_started=Max("span__started_at"))
    findings = Finding.objects.aggregate(count=Count("id"), newest=Max("created_at"))
    return (
        calls["count"] or 0,
        calls["max_span_started"],
        findings["count"] or 0,
        findings["newest"],
    )


def get_tool_meta(tool_name: str) -> dict[str, Any]:
    if tool_name in TOOL_METADATA:
        return {"name": tool_name, **TOOL_METADATA[tool_name]}
        
    if tool_name.startswith("mcp_server."):
        parts = tool_name.split(".")
        clean_title = parts[-1].replace("_", " ").strip().title()
        package = parts[1] if len(parts) > 1 else "core"
        if package == "tools" and len(parts) > 2:
            package = parts[2]
            
        return {
            "name": tool_name,
            "title": clean_title,
            "category": f"Internal Python: {package.title()}",
            "icon": "🔧",
            "badge_color": "#475569",
            "description": (
                f"Internal Python execution trace for '{tool_name}'. "
                "INPUT/RETURN: Captures raw Python function arguments and return values rather than LLM payloads. "
                "PURPOSE: Measures internal code latency and isolates sub-component errors. "
                "IMPACT: High latency or errors here indicate a bottleneck/bug within the MCP server codebase, not an LLM hallucination."
            ),
        }

    # Fallback for dynamic/custom tools
    clean_title = tool_name.replace("_", " ").title()
    return {
        "name": tool_name,
        "title": clean_title,
        "category": "General MCP Tool",
        "icon": "⚙️",
        "badge_color": "#64748b",
        "description": f"MCP tool: {tool_name}",
    }


def format_bytes_human(num_bytes: int | float | None) -> str:
    if not num_bytes or num_bytes <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB"]
    idx = 0
    val = float(num_bytes)
    while val >= 1024.0 and idx < len(units) - 1:
        val /= 1024.0
        idx += 1
    return f"{val:.1f} {units[idx]}" if idx > 0 else f"{int(val)} B"


def get_tools_overview_metrics(
    since: datetime | None = None,
    platform_filter: str = "",
    category_filter: str = "",
    status_filter: str = "",
    search_q: str = "",
) -> dict[str, Any]:
    cache_key = (
        _tool_calls_signature(),
        since.isoformat() if since else "",
        platform_filter or "",
        category_filter or "",
        status_filter or "",
        search_q or "",
    )
    cached = _TOOLS_OVERVIEW_CACHE.get(cache_key)
    if cached is not None:
        return cached
    db_cached = get_cached_value("tools_overview_metrics_v2_public_only", cache_key)
    if db_cached is not None:
        _TOOLS_OVERVIEW_CACHE[cache_key] = db_cached
        return db_cached

    grouped: dict[str, list[ToolCall]] = defaultdict(list)
    tool_calls_qs = ToolCall.objects.exclude(tool_name__startswith="mcp_server.").select_related("span", "span__session")
    if since is not None:
        tool_calls_qs = tool_calls_qs.filter(span__started_at__gte=since)

    # Status filter
    if status_filter == "error":
        tool_calls_qs = tool_calls_qs.filter(Q(is_error=True) | Q(span__status="error"))
    elif status_filter == "ok":
        tool_calls_qs = tool_calls_qs.filter(is_error=False).exclude(span__status="error")

    # Platform filter
    p_filter = (platform_filter or "").lower().strip()
    if p_filter and p_filter != "all":
        from apps.core.platform_filters import PLATFORM_MAP
        if p_filter in PLATFORM_MAP:
            tool_calls_qs = tool_calls_qs.filter(span__session__in=Session.objects.filter(PLATFORM_MAP[p_filter]["filter"]))

    # Search filter in tool name or session
    if search_q:
        s_lower = search_q.lower().strip()
        tool_calls_qs = tool_calls_qs.filter(
            Q(tool_name__icontains=s_lower)
            | Q(span__session__external_session_id__icontains=s_lower)
        )

    for call in tool_calls_qs:
        grouped[call.tool_name].append(call)

    # Category filter
    c_filter = (category_filter or "").strip()
    if c_filter and c_filter != "all":
        grouped = {k: v for k, v in grouped.items() if get_tool_meta(k)["category"].lower() == c_filter.lower()}

    findings_qs = Finding.objects.filter(status="open").exclude(span=None)
    if since is not None:
        findings_qs = findings_qs.filter(created_at__gte=since)
    finding_counts = Counter(findings_qs.values_list("span__name", flat=True))

    tool_rows = []
    all_durations = []
    total_calls_count = 0
    total_errors_count = 0

    for name, calls in grouped.items():
        durations = [float(call.span.duration_ms or 0) for call in calls if call.span and call.span.duration_ms is not None]
        errors = sum(1 for call in calls if call.is_error or (call.span and call.span.status == "error"))
        all_durations.extend(durations)
        total_calls_count += len(calls)
        total_errors_count += errors

        p50 = percentile(durations, 0.5) or 0
        p95 = percentile(durations, 0.95) or 0
        avg_dur = sum(durations) / len(durations) if durations else 0
        meta = get_tool_meta(name)

        tool_rows.append({
            "name": name,
            "title": meta["title"],
            "category": meta["category"],
            "icon": meta["icon"],
            "badge_color": meta["badge_color"],
            "description": meta["description"],
            "count": len(calls),
            "p50": round(p50, 1),
            "p95": round(p95, 1),
            "avg_duration_ms": round(avg_dur, 1),
            "error_rate": round((errors * 100.0) / len(calls), 1) if calls else 0.0,
            "error_count": errors,
            "findings": finding_counts[name],
            "avg_result_bytes": round(sum(call.result_bytes for call in calls) / len(calls)) if calls else 0,
            "avg_result_bytes_formatted": format_bytes_human(
                sum(call.result_bytes for call in calls) / len(calls) if calls else 0
            ),
        })

    tool_rows = sorted(tool_rows, key=lambda x: (-x["count"], x["name"]))

    # Categories breakdown
    cat_counts = Counter(row["category"] for row in tool_rows)

    result = {
        "total_tools_count": len(grouped),
        "total_invocations": total_calls_count,
        "total_errors": total_errors_count,
        "overall_success_rate": round(((total_calls_count - total_errors_count) * 100.0) / total_calls_count, 1) if total_calls_count else 100.0,
        "overall_p50_ms": round(percentile(all_durations, 0.5) or 0, 1),
        "overall_p95_ms": round(percentile(all_durations, 0.95) or 0, 1),
        "tool_metrics": tool_rows,
        "category_counts": dict(cat_counts),
        "chart_json": json.dumps(tool_rows),
        "active_platform": p_filter or "all",
        "active_category": c_filter or "all",
        "active_status": status_filter or "all",
    }
    if len(_TOOLS_OVERVIEW_CACHE) > 16:
        _TOOLS_OVERVIEW_CACHE.clear()
    _TOOLS_OVERVIEW_CACHE[cache_key] = result
    set_cached_value("tools_overview_metrics_v2_public_only", cache_key, result)
    return result


def get_tool_detail_data(
    tool_name: str,
    page: int = 1,
    page_size: int = 10,
    search_q: str = "",
    status_filter: str = "",
    platform_filter: str = "",
    speed_filter: str = "",
    http_filter: str = "",
    since: datetime | None = None,
) -> dict[str, Any]:
    """
    Computes detailed telemetry, sequence flow, error breakdown, child HTTP activity,
    and step-by-step invocation history for a specific tool with rich multi-field filtering.
    """
    tool_meta = get_tool_meta(tool_name)
    if not is_public_mcp_tool_name(tool_name):
        return {
            "meta": tool_meta,
            "total_calls": 0,
            "filtered_calls_count": 0,
            "invocations": [],
            "page_obj": None,
            "active_filters": {
                "q": search_q,
                "status": status_filter or "all",
                "platform": platform_filter or "all",
                "speed": speed_filter or "all",
                "http": http_filter or "all",
            },
        }
    all_calls_qs = (
        ToolCall.objects.filter(tool_name=tool_name)
        .select_related("span", "span__session")
        .order_by("-span__started_at")
    )
    if since is not None:
        all_calls_qs = all_calls_qs.filter(span__started_at__gte=since)
    all_calls = list(all_calls_qs)

    total_calls = len(all_calls)
    if total_calls == 0:
        return {
            "meta": tool_meta,
            "total_calls": 0,
            "filtered_calls_count": 0,
            "invocations": [],
            "page_obj": None,
            "active_filters": {
                "q": search_q,
                "status": status_filter or "all",
                "platform": platform_filter or "all",
                "speed": speed_filter or "all",
                "http": http_filter or "all",
            },
        }

    durations = [float(c.span.duration_ms or 0) for c in all_calls if c.span and c.span.duration_ms is not None]
    error_calls = [c for c in all_calls if c.is_error or (c.span and c.span.status == "error")]
    errors_count = len(error_calls)
    success_count = total_calls - errors_count
    error_rate = round((errors_count * 100.0) / total_calls, 1)
    success_rate = round((success_count * 100.0) / total_calls, 1)

    # Latency stats
    min_ms = round(min(durations), 1) if durations else 0
    avg_ms = round(sum(durations) / len(durations), 1) if durations else 0
    p50_ms = round(percentile(durations, 0.5) or 0, 1)
    p90_ms = round(percentile(durations, 0.9) or 0, 1)
    p95_ms = round(percentile(durations, 0.95) or 0, 1)
    p99_ms = round(percentile(durations, 0.99) or 0, 1)
    max_ms = round(max(durations), 1) if durations else 0

    # Data Transfer
    total_bytes = sum(c.result_bytes for c in all_calls)
    avg_bytes = round(total_bytes / total_calls) if total_calls else 0

    # Platforms & Models with Base-Rate Normalization
    global_platform_sessions = Counter()
    for s in Session.objects.all():
        global_platform_sessions[s.platform] += 1

    global_platform_tool_calls = Counter()
    for c in ToolCall.objects.select_related("span", "span__session"):
        if c.span and c.span.session:
            global_platform_tool_calls[c.span.session.platform] += 1

    platform_counter = Counter()
    platform_session_sets = defaultdict(set)
    model_counter = Counter()
    for c in all_calls:
        if c.span and c.span.session:
            p = c.span.session.platform
            platform_counter[p] += 1
            platform_session_sets[p].add(c.span.session_id)
            model_counter[c.span.session.model or "unknown"] += 1

    platforms_breakdown = []
    for p, cnt in platform_counter.most_common():
        total_p_sess = global_platform_sessions.get(p, 0)
        total_p_calls = global_platform_tool_calls.get(p, 0)
        sess_used = len(platform_session_sets.get(p, set()))

        session_pen_pct = round((sess_used * 100.0) / total_p_sess, 1) if total_p_sess else 0.0
        share_of_platform_calls = round((cnt * 100.0) / total_p_calls, 1) if total_p_calls else 0.0
        avg_calls_per_sess = round(cnt / total_p_sess, 2) if total_p_sess else 0.0

        platforms_breakdown.append({
            "platform": p,
            "display": Session(provider=p).platform_display,
            "count": cnt,
            "share_of_this_tool_pct": round((cnt * 100.0) / total_calls, 1),
            "sessions_used": sess_used,
            "total_platform_sessions": total_p_sess,
            "session_penetration_pct": session_pen_pct,
            "total_platform_tool_calls": total_p_calls,
            "share_of_platform_calls_pct": share_of_platform_calls,
            "avg_calls_per_session": avg_calls_per_sess,
        })

    # Latency Distribution Buckets
    buckets = [
        {"label": "< 200 ms", "sub": "Instant / Local", "count": 0, "color": "#22c55e"},
        {"label": "200 ms - 1s", "sub": "Fast API", "count": 0, "color": "#3b82f6"},
        {"label": "1s - 5s", "sub": "Standard Multi-API", "count": 0, "color": "#a855f7"},
        {"label": "5s - 15s", "sub": "Heavy DAG / AI", "count": 0, "color": "#f97316"},
        {"label": "> 15s", "sub": "Tail Latency", "count": 0, "color": "#ef4444"},
    ]
    for d in durations:
        if d < 200:
            buckets[0]["count"] += 1
        elif d < 1000:
            buckets[1]["count"] += 1
        elif d < 5000:
            buckets[2]["count"] += 1
        elif d < 15000:
            buckets[3]["count"] += 1
        else:
            buckets[4]["count"] += 1

    for b in buckets:
        b["pct"] = round((b["count"] * 100.0) / total_calls, 1) if total_calls else 0.0

    # Sequence Analysis (Preceding & Subsequent tools in sessions)
    preceding_counter = Counter()
    subsequent_counter = Counter()

    sample_calls = all_calls[:60]
    session_ids = list({c.span.session_id for c in sample_calls if c.span and c.span.session_id})
    all_sess_spans = defaultdict(list)
    for sp in Span.objects.filter(session_id__in=session_ids, kind="tool").order_by("started_at", "sequence_no"):
        all_sess_spans[sp.session_id].append(sp)

    for c in sample_calls:
        if not c.span:
            continue
        s_spans = all_sess_spans.get(c.span.session_id, [])
        s_ids = [s.id for s in s_spans]
        if c.span_id in s_ids:
            idx = s_ids.index(c.span_id)
            if idx > 0:
                preceding_counter[s_spans[idx - 1].name] += 1
            if idx < len(s_spans) - 1:
                subsequent_counter[s_spans[idx + 1].name] += 1

    preceding_list = [{"name": name, "count": cnt, "meta": get_tool_meta(name)} for name, cnt in preceding_counter.most_common(5)]
    subsequent_list = [{"name": name, "count": cnt, "meta": get_tool_meta(name)} for name, cnt in subsequent_counter.most_common(5)]

    # Child HTTP Jotform API calls
    tool_span_ids = [c.span_id for c in all_calls]
    child_ext_calls = list(
        ExternalCall.objects.filter(span__parent_id__in=tool_span_ids)
        .select_related("span")
        .order_by("span__started_at")
    )
    total_http_count = len(child_ext_calls)
    avg_http_count = round(total_http_count / total_calls, 1) if total_calls else 0.0

    endpoint_counter = Counter()
    for ext in child_ext_calls:
        endpoint_key = f"{ext.method} {ext.url_template}"
        endpoint_counter[endpoint_key] += 1

    top_endpoints = [
        {"endpoint": ep, "count": cnt, "pct": round((cnt * 100.0) / total_http_count, 1) if total_http_count else 0.0}
        for ep, cnt in endpoint_counter.most_common(8)
    ]

    # Error messages breakdown
    error_counter = Counter()
    for c in error_calls:
        res = clean_and_normalize_data(c.result)
        err_msg = ""
        if isinstance(res, dict):
            err_msg = str(res.get("error") or res.get("message") or "")
        elif isinstance(res, str):
            err_msg = res
        if not err_msg:
            err_msg = "Unknown execution error / is_error flag set"
        error_counter[err_msg[:160]] += 1

    top_errors = [{"message": msg, "count": cnt} for msg, cnt in error_counter.most_common(6)]

    # Associated rule findings
    findings = list(
        Finding.objects.filter(span__name=tool_name, status="open")
        .select_related("session")
        .order_by("-created_at")[:10]
    )

    # Invocations list with multi-field filtering
    invocations_raw = all_calls

    # 1. Search Query
    if search_q:
        search_lower = search_q.lower().strip()
        invocations_raw = [
            c for c in invocations_raw
            if search_lower in str(c.arguments).lower()
            or search_lower in str(c.result).lower()
            or (c.span and c.span.session and search_lower in c.span.session.external_session_id.lower())
            or (c.span and c.span.session and search_lower in (c.span.session.model or "").lower())
        ]

    # 2. Status Filter
    status_filter = (status_filter or "").lower().strip()
    if status_filter and status_filter != "all":
        if status_filter == "error":
            invocations_raw = [c for c in invocations_raw if c.is_error or (c.span and c.span.status == "error")]
        elif status_filter == "ok":
            invocations_raw = [c for c in invocations_raw if not c.is_error and (not c.span or c.span.status != "error")]

    # 3. Platform Filter
    platform_filter = (platform_filter or "").lower().strip()
    if platform_filter and platform_filter != "all":
        invocations_raw = [
            c for c in invocations_raw
            if c.span and c.span.session and c.span.session.platform == platform_filter
        ]

    # 4. Speed / Latency Tier Filter
    speed_filter = (speed_filter or "").lower().strip()
    if speed_filter and speed_filter != "all":
        def check_speed(c):
            dur = float(c.span.duration_ms or 0) if c.span else 0
            if speed_filter == "fast":
                return dur < 1000
            if speed_filter == "normal":
                return 1000 <= dur < 5000
            if speed_filter == "slow":
                return 5000 <= dur < 15000
            if speed_filter == "critical":
                return dur >= 15000
            return True
        invocations_raw = [c for c in invocations_raw if check_speed(c)]

    filtered_count = len(invocations_raw)

    paginator = Paginator(invocations_raw, page_size)
    try:
        page_obj = paginator.page(page)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    # Fetch child HTTP calls and parent turn for items in the current page
    page_span_ids = [c.span_id for c in page_obj.object_list]
    page_child_spans = defaultdict(list)
    for sp in (
        Span.objects.filter(parent_id__in=page_span_ids, kind="external")
        .select_related("external_call")
        .order_by("started_at")
    ):
        page_child_spans[sp.parent_id].append(sp)

    invocations = []
    for c in page_obj.object_list:
        span = c.span
        session = span.session if span else None
        c_children = page_child_spans.get(c.span_id, [])

        # Duration speed rating
        dur = float(span.duration_ms or 0) if span else 0
        if dur < 1000:
            speed_badge = "fast"
        elif dur < 5000:
            speed_badge = "normal"
        elif dur < 15000:
            speed_badge = "slow"
        else:
            speed_badge = "critical"

        http_list = []
        for ch in c_children:
            ext = getattr(ch, "external_call", None)
            if ext:
                http_list.append({
                    "method": ext.method,
                    "endpoint": ext.url_template,
                    "status_code": ext.status_code,
                    "duration_ms": ch.duration_ms,
                })

        invocations.append({
            "id": str(c.span_id),
            "span_id": str(span.id) if span else None,
            "session": session,
            "session_id": str(session.id) if session else "",
            "session_display": session.display_name if session else "—",
            "platform": session.platform if session else "mcp",
            "platform_display": session.platform_display if session else "MCP",
            "model": session.model if session else "—",
            "timestamp": span.started_at if span else None,
            "duration_ms": dur,
            "speed_badge": speed_badge,
            "is_error": c.is_error or (span.status == "error" if span else False),
            "status": span.status if span else ("error" if c.is_error else "ok"),
            "arguments_summary": human_value(c.arguments, max_chars=140),
            "arguments_formatted": pretty_payload(c.arguments, max_chars=3000),
            "result_summary": result_summary(c.result),
            "result_formatted": pretty_payload(c.result, max_chars=3000),
            "http_calls": http_list,
            "http_count": len(http_list),
            "result_bytes": c.result_bytes,
            "result_bytes_formatted": format_bytes_human(c.result_bytes),
        })

    return {
        "meta": tool_meta,
        "total_calls": total_calls,
        "success_count": success_count,
        "success_rate": success_rate,
        "error_count": errors_count,
        "error_rate": error_rate,
        "latency": {
            "min_ms": min_ms,
            "avg_ms": avg_ms,
            "p50_ms": p50_ms,
            "p90_ms": p90_ms,
            "p95_ms": p95_ms,
            "p99_ms": p99_ms,
            "max_ms": max_ms,
        },
        "data_transferred": {
            "avg_bytes": avg_bytes,
            "avg_bytes_formatted": format_bytes_human(avg_bytes),
            "total_bytes": total_bytes,
            "total_bytes_formatted": format_bytes_human(total_bytes),
        },
        "platforms_breakdown": platforms_breakdown,
        "models_breakdown": [{"model": m, "count": cnt} for m, cnt in model_counter.most_common(5)],
        "latency_buckets": buckets,
        "sequence_flow": {
            "preceding": preceding_list,
            "subsequent": subsequent_list,
        },
        "http_stats": {
            "total_http_count": total_http_count,
            "avg_http_count": avg_http_count,
            "top_endpoints": top_endpoints,
        },
        "top_errors": top_errors,
        "findings": findings,
        "invocations": invocations,
        "page_obj": page_obj,
        "search_q": search_q,
        "filtered_calls_count": filtered_count,
        "active_filters": {
            "q": search_q,
            "status": status_filter or "all",
            "platform": platform_filter or "all",
            "speed": speed_filter or "all",
        },
    }

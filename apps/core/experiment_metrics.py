from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
import json
import re

from django.conf import settings
from django.db.models import Count, Max, Prefetch, Q

from apps.findings.models import Finding
from apps.ingestion.models import RawEvent
from apps.traces.models import Session, Span, ToolCall, Turn

AB_SCENARIOS = [
    {
        "profile": "ab_a",
        "label": "A",
        "name": "No Templates & No Gap Control",
        "template_enabled": False,
        "gap_enabled": False,
        "marker": "[ABCD-A]",
        "prompt": "I want to set up a workflow for new customer return requests.",
    },
    {
        "profile": "ab_b",
        "label": "B",
        "name": "Template Support Only",
        "template_enabled": True,
        "gap_enabled": False,
        "marker": "[ABCD-B]",
        "prompt": "I want to set up a workflow for new warranty service requests.",
    },
    {
        "profile": "ab_c",
        "label": "C",
        "name": "Gap Control Only",
        "template_enabled": False,
        "gap_enabled": True,
        "marker": "[ABCD-C]",
        "prompt": "I want to set up a workflow for new dealer application requests.",
    },
    {
        "profile": "ab_d",
        "label": "D",
        "name": "Full System",
        "template_enabled": True,
        "gap_enabled": True,
        "marker": "[ABCD-D]",
        "prompt": "I want to set up a workflow for new equipment maintenance requests.",
    },
]

SCENARIO_BY_PROFILE = {scenario["profile"]: scenario for scenario in AB_SCENARIOS}
SCENARIO_BY_LABEL = {scenario["label"]: scenario for scenario in AB_SCENARIOS}
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")
DISPLAY_ONLY_TOOLS = frozenset({"show_workflow", "show_workflows"})
_EXPERIMENT_CACHE: dict[tuple, list[dict]] = {}
_EXPERIMENT_BASE_CACHE: dict[tuple, tuple[list[Session], dict[str, dict], dict[str, list[ToolCall]]]] = {}


def _experiment_signature(since=None, platform_q: Q | None = None) -> tuple:
    sessions_qs = Session.objects.all()
    if since is not None:
        sessions_qs = sessions_qs.filter(started_at__gte=since)
    if platform_q:
        sessions_qs = sessions_qs.filter(platform_q)
    sessions = sessions_qs.aggregate(count=Count("id"), newest=Max("started_at"))
    calls = ToolCall.objects.aggregate(count=Count("span_id"))
    events = RawEvent.objects.aggregate(count=Count("id"), newest=Max("occurred_at"))
    return (
        sessions["count"] or 0,
        sessions["newest"],
        calls["count"] or 0,
        events["count"] or 0,
        events["newest"],
    )


def _experiment_candidate_ids(base_qs) -> list:
    """Cheap first pass: find which sessions belong to an A/B/C/D experiment scenario using only
    inexpensive fields — `Session.metadata`, the `mcp.list_tools.completed` RawEvent payload, and
    `Turn.question` text (all plain columns/small JSON, no ToolCall payloads).

    This intentionally skips the tool-call-argument/result marker-text fallback that
    `_scenario_for_session` also supports: decoding every ToolCall's `arguments`/`result` JSON for
    all sessions just to look for a handful of matches is exactly the cost we're avoiding (that
    JSON decode is the dominant cost of this page — see the profiling notes in `views.py`). On the
    current dataset every real experiment session is tagged via metadata or the list_tools raw
    event, so this fast path is exhaustive in practice; a session whose *only* signal is a marker
    buried inside a tool call's JSON body (never observed here) would not be picked up.
    """
    light_sessions = list(
        base_qs
        .only("id", "external_session_id", "metadata")
        .prefetch_related(
            Prefetch("turns", queryset=Turn.objects.only("id", "session_id", "question")),
        )
    )
    raw_list_tools = _list_tools_payloads_for_sessions(light_sessions)
    return [
        session.id
        for session in light_sessions
        if _scenario_for_session(session, calls=[], raw_metadata=raw_list_tools.get(session.external_session_id)) is not None
    ]


def _experiment_base_data(
    signature: tuple, since=None, platform_q: Q | None = None,
) -> tuple[list[Session], dict[str, dict], dict[str, list[ToolCall]]]:
    cached = _EXPERIMENT_BASE_CACHE.get(signature)
    if cached is not None:
        return cached

    base_qs = Session.objects.all()
    if since is not None:
        base_qs = base_qs.filter(started_at__gte=since)
    if platform_q:
        base_qs = base_qs.filter(platform_q)

    candidate_ids = _experiment_candidate_ids(base_qs)

    sessions = list(
        Session.objects
        .filter(id__in=candidate_ids)
        .only(
            "id",
            "external_session_id",
            "provider",
            "model",
            "status",
            "started_at",
            "ended_at",
            "duration_ms",
            "metadata",
        )
        .prefetch_related(
            Prefetch(
                "turns",
                queryset=Turn.objects.only("id", "session_id", "sequence_no", "question").order_by("sequence_no"),
            ),
            Prefetch(
                "spans",
                queryset=Span.objects.only(
                    "id",
                    "session_id",
                    "kind",
                    "started_at",
                    "ended_at",
                    "duration_ms",
                    "sequence_no",
                ).order_by("started_at", "sequence_no"),
            ),
            Prefetch(
                "findings",
                queryset=Finding.objects.only("id", "session_id", "wasted_ms"),
            ),
        )
        .order_by("-started_at", "-id")
    )
    list_tools_by_session = _list_tools_payloads_for_sessions(sessions)
    calls_by_session: dict[str, list[ToolCall]] = defaultdict(list)
    for call in (
        ToolCall.objects
        .filter(span__session__in=sessions)
        .select_related("span")
        .only(
            "span_id",
            "tool_name",
            "arguments",
            "result",
            "argument_hash",
            "is_error",
            "span__id",
            "span__session_id",
            "span__kind",
            "span__started_at",
            "span__ended_at",
            "span__duration_ms",
            "span__sequence_no",
        )
        .order_by("span__started_at", "span__sequence_no")
    ):
        calls_by_session[str(call.span.session_id)].append(call)

    result = (sessions, list_tools_by_session, calls_by_session)
    if len(_EXPERIMENT_BASE_CACHE) > 4:
        _EXPERIMENT_BASE_CACHE.clear()
    _EXPERIMENT_BASE_CACHE[signature] = result
    return result


def _result_error(call: ToolCall) -> str:
    result = _result_payload(call)
    return str(result.get("error") or "")


def _gap_issue_count(call: ToolCall) -> int:
    result = call.result if isinstance(call.result, dict) else {}
    issues = result.get("issues") or []
    return len(issues) if isinstance(issues, list) else 0


def _ok_to_publish(call: ToolCall) -> bool | None:
    result = call.result if isinstance(call.result, dict) else {}
    return result.get("ok_to_publish") if "ok_to_publish" in result else None


def _span_end_at(call: ToolCall):
    span = call.span
    if span.ended_at:
        return span.ended_at
    if span.started_at and span.duration_ms is not None:
        return span.started_at + timedelta(milliseconds=float(span.duration_ms or 0))
    return None


def _successful_build_call(calls: list[ToolCall]) -> ToolCall | None:
    return next(iter(_successful_build_calls(calls)), None)


def _successful_build_calls(calls: list[ToolCall]) -> list[ToolCall]:
    successful = []
    for call in calls:
        if call.tool_name != "build_workflow_bulk" or call.is_error or _result_error(call):
            continue
        payload = _result_payload(call)
        if any(payload.get(key) for key in ("workflow_id", "workflow_url", "created_steps", "created_links", "trigger_form_id")):
            successful.append(call)
    return successful


def _workflow_id(call: ToolCall) -> str:
    return str(_result_payload(call).get("workflow_id") or "")


def _trigger_form_id(call: ToolCall) -> str:
    payload = _result_payload(call)
    return str(payload.get("trigger_form_id") or payload.get("form_id") or "")


def _created_form_builds(build_calls: list[ToolCall]) -> list[ToolCall]:
    return [
        call for call in _successful_build_calls(build_calls)
        if call.arguments.get("form_prompt") and _trigger_form_id(call)
    ]


def _call_duration_ms(call: ToolCall | None) -> float:
    if not call:
        return 0
    return round(float(call.span.duration_ms or 0), 1)


def _time_to_call_end_ms(session: Session, call: ToolCall | None, spans: list | None = None) -> float:
    if not call:
        return 0
    span_list = spans or list(session.spans.all())
    origin = session.started_at or next((span.started_at for span in span_list if span.started_at), None)
    ended_at = _span_end_at(call)
    if origin and ended_at:
        return round(max((ended_at - origin).total_seconds() * 1000, 0), 1)
    return _call_duration_ms(call)


def _post_call_ms(session: Session, call: ToolCall | None) -> float:
    if not call or not session.ended_at:
        return 0
    ended_at = _span_end_at(call)
    if not ended_at:
        return 0
    return round(max((session.ended_at - ended_at).total_seconds() * 1000, 0), 1)


def _health_issue_count(call: ToolCall) -> int:
    result = call.result if isinstance(call.result, dict) else {}
    health = result.get("health") or {}
    if not isinstance(health, dict):
        return 0
    keys = (
        "unreachable_steps",
        "dead_end_steps",
        "unknown_types",
        "dangling_links",
        "unconnected_branches",
        "invalid_branch_links",
        "unlabelled_branching_steps",
    )
    return sum(len(health.get(key) or []) for key in keys)


def _json_text(value) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        return str(value)


def _list_tools_payloads_for_sessions(sessions: list[Session]) -> dict[str, dict]:
    external_ids = [session.external_session_id for session in sessions]
    list_tools_by_session = {}
    for event in RawEvent.objects.filter(
        payload__session_id__in=external_ids,
        event_type="mcp.list_tools.completed",
    ).order_by("-occurred_at"):
        session_id = str(event.payload.get("session_id") or "")
        list_tools_by_session.setdefault(session_id, event.payload)
    return list_tools_by_session


def _scenario_for_session(
    session: Session,
    calls: list[ToolCall],
    raw_metadata: dict | None = None,
) -> dict | None:
    metadata = {**(raw_metadata or {}), **(session.metadata or {})}
    profile = str(metadata.get("tool_profile") or "").lower()
    if profile in SCENARIO_BY_PROFILE:
        return SCENARIO_BY_PROFILE[profile]

    label = str(metadata.get("experiment_scenario") or "").strip().upper()
    if label in SCENARIO_BY_LABEL:
        return SCENARIO_BY_LABEL[label]

    text_parts = [session.external_session_id]
    text_parts.extend(_json_text(value) for value in metadata.values())
    text_parts.extend(turn.question for turn in session.turns.all())
    for call in calls:
        text_parts.append(_json_text(call.arguments))
        text_parts.append(_json_text(call.result))
    haystack = "\n".join(text_parts).upper()
    for scenario in AB_SCENARIOS:
        if scenario["marker"].upper() in haystack:
            return scenario
    return None


def scenario_rows(since=None, platform_q: Q | None = None, active_range: str = "all", platform_key: str = "all") -> list[dict]:
    signature = _experiment_signature(since, platform_q)
    cache_key = ("scenario_rows", active_range, platform_key, signature)
    if cache_key in _EXPERIMENT_CACHE:
        return _EXPERIMENT_CACHE[cache_key]

    sessions, list_tools_by_session, calls_by_session = _experiment_base_data(signature, since, platform_q)

    sessions_by_profile: dict[str, list[Session]] = defaultdict(list)
    for session in sessions:
        calls = calls_by_session.get(str(session.id), [])
        scenario = _scenario_for_session(
            session,
            calls,
            list_tools_by_session.get(session.external_session_id),
        )
        has_relevant_call = any(call.tool_name not in DISPLAY_ONLY_TOOLS for call in calls)
        if scenario and has_relevant_call:
            sessions_by_profile[scenario["profile"]].append(session)

    rows = []
    for scenario in AB_SCENARIOS:
        profile = scenario["profile"]
        profile_sessions = sessions_by_profile.get(profile, [])
        tool_counts = []
        api_counts = []
        durations = []
        first_build_ms = []
        final_build_ms = []
        time_to_first_build_ms = []
        time_to_final_build_ms = []
        post_build_ms = []
        tool_ms = []
        api_ms = []
        template_used = gap_used = build_errors = missing_builds = quality_issues = publishable = 0
        finding_count = finding_wasted_ms = 0
        latest_session = profile_sessions[0] if profile_sessions else None

        for session in profile_sessions:
            spans = list(session.spans.all())
            calls = calls_by_session.get(str(session.id), [])
            durations.append(float(session.duration_ms or 0))
            successful_builds = _successful_build_calls(calls)
            first_build = successful_builds[0] if successful_builds else None
            final_build = successful_builds[-1] if successful_builds else None
            if first_build:
                first_build_ms.append(_call_duration_ms(first_build))
                time_to_first_build_ms.append(_time_to_call_end_ms(session, first_build, spans))
                post_build_ms.append(_post_call_ms(session, first_build))
            if final_build:
                final_build_ms.append(_call_duration_ms(final_build))
                time_to_final_build_ms.append(_time_to_call_end_ms(session, final_build, spans))
            tool_spans = [span for span in spans if span.kind == "tool"]
            api_spans = [span for span in spans if span.kind == "external"]
            tool_counts.append(len(tool_spans))
            api_counts.append(len(api_spans))
            tool_ms.append(sum(float(span.duration_ms or 0) for span in tool_spans))
            api_ms.append(sum(float(span.duration_ms or 0) for span in api_spans))

            if any(call.tool_name == "search_workflow_templates" for call in calls):
                template_used += 1
            if any(call.tool_name == "inspect_workflow_gaps" for call in calls):
                gap_used += 1
            if any(call.tool_name == "build_workflow_bulk" and _result_error(call) for call in calls):
                build_errors += 1
            if not any(call.tool_name == "build_workflow_bulk" for call in calls):
                missing_builds += 1
            gap_calls = [call for call in calls if call.tool_name == "inspect_workflow_gaps"]
            workflow_calls = [call for call in calls if call.tool_name == "get_workflow"]
            quality_issues += sum(_gap_issue_count(call) for call in gap_calls)
            quality_issues += sum(_health_issue_count(call) for call in workflow_calls)
            session_findings = list(session.findings.all())
            finding_count += len(session_findings)
            finding_wasted_ms += sum(float(finding.wasted_ms or 0) for finding in session_findings)
            if any(_ok_to_publish(call) is True for call in gap_calls):
                publishable += 1

        count = len(profile_sessions)
        rows.append({
            **scenario,
            "session_count": count,
            "avg_duration_ms": round(sum(durations) / count, 1) if count else 0,
            "avg_first_build_ms": round(sum(first_build_ms) / len(first_build_ms), 1) if first_build_ms else 0,
            "avg_final_build_ms": round(sum(final_build_ms) / len(final_build_ms), 1) if final_build_ms else 0,
            "avg_time_to_first_build_ms": round(sum(time_to_first_build_ms) / len(time_to_first_build_ms), 1) if time_to_first_build_ms else 0,
            "avg_time_to_final_build_ms": round(sum(time_to_final_build_ms) / len(time_to_final_build_ms), 1) if time_to_final_build_ms else 0,
            "avg_post_build_ms": round(sum(post_build_ms) / len(post_build_ms), 1) if post_build_ms else 0,
            "avg_tool_calls": round(sum(tool_counts) / count, 1) if count else 0,
            "avg_api_calls": round(sum(api_counts) / count, 1) if count else 0,
            "avg_tool_ms": round(sum(tool_ms) / count, 1) if count else 0,
            "avg_api_ms": round(sum(api_ms) / count, 1) if count else 0,
            "template_used": template_used,
            "gap_used": gap_used,
            "build_errors": build_errors,
            "missing_builds": missing_builds,
            "quality_issues": quality_issues,
            "finding_count": finding_count,
            "finding_wasted_ms": round(finding_wasted_ms, 1),
            "publishable": publishable,
            "latest_session": latest_session,
        })
    if len(_EXPERIMENT_CACHE) > 8:
        _EXPERIMENT_CACHE.clear()
    _EXPERIMENT_CACHE[cache_key] = rows
    return rows


def _result_payload(call: ToolCall) -> dict:
    result = call.result if isinstance(call.result, dict) else {}
    structured = result.get("structured_content")
    return structured if isinstance(structured, dict) else result


def _repeated_tool_notes(calls: list[ToolCall]) -> list[str]:
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for call in calls:
        counts[(call.tool_name, call.argument_hash)] += 1
    notes = []
    for (tool_name, _argument_hash), count in sorted(counts.items()):
        if count > 1:
            notes.append(f"{tool_name} was called {count} times with the same argument.")
    return notes


def experiment_run_rows(
    limit: int = 25,
    latest_per_scenario: bool = True,
    since=None,
    platform_q: Q | None = None,
    active_range: str = "all",
    platform_key: str = "all",
) -> list[dict]:
    signature = _experiment_signature(since, platform_q)
    cache_key = ("experiment_run_rows", int(limit), bool(latest_per_scenario), active_range, platform_key, signature)
    if cache_key in _EXPERIMENT_CACHE:
        return _EXPERIMENT_CACHE[cache_key]

    sessions, list_tools_by_session, calls_by_session = _experiment_base_data(signature, since, platform_q)

    rows = []
    seen_profiles = set()
    for session in sessions:
        calls = calls_by_session.get(str(session.id), [])
        raw_list_tools = list_tools_by_session.get(session.external_session_id) or {}
        scenario = _scenario_for_session(session, calls, raw_list_tools)
        if not scenario:
            continue
        if not any(call.tool_name not in DISPLAY_ONLY_TOOLS for call in calls):
            continue
        if latest_per_scenario and scenario["profile"] in seen_profiles:
            continue
        seen_profiles.add(scenario["profile"])

        spans = list(session.spans.all())
        tool_spans = [span for span in spans if span.kind == "tool"]
        api_spans = [span for span in spans if span.kind == "external"]
        build_calls = [call for call in calls if call.tool_name == "build_workflow_bulk"]
        successful_builds = _successful_build_calls(build_calls)
        successful_workflow_ids = [
            workflow_id
            for workflow_id in (_workflow_id(call) for call in successful_builds)
            if workflow_id
        ]
        distinct_workflow_ids = list(dict.fromkeys(successful_workflow_ids))
        first_build = successful_builds[0] if successful_builds else None
        final_build = successful_builds[-1] if successful_builds else None
        build_arguments = build_calls[-1].arguments if build_calls else {}
        build_payload = _result_payload(build_calls[-1]) if build_calls else {}
        warnings = build_payload.get("warnings") or []
        created_steps = build_payload.get("created_steps") or {}
        created_links = build_payload.get("created_links") or []
        advertised_tools = raw_list_tools.get("tools") or []
        template_used = any(call.tool_name == "search_workflow_templates" for call in calls)
        gap_used = any(call.tool_name == "inspect_workflow_gaps" for call in calls)
        template_result_counts = [
            len((_result_payload(call).get("results") or _result_payload(call).get("templates") or []))
            for call in calls
            if call.tool_name == "search_workflow_templates"
        ]
        build_errors = sum(1 for call in build_calls if _result_error(call))
        session_findings = list(session.findings.all())
        finding_wasted_ms = sum(float(finding.wasted_ms or 0) for finding in session_findings)
        template_exposed = "search_workflow_templates" in advertised_tools
        gap_exposed = "inspect_workflow_gaps" in advertised_tools
        exposure_ok = (
            template_exposed == scenario["template_enabled"]
            and gap_exposed == scenario["gap_enabled"]
        ) if advertised_tools else None
        behavior_ok = (
            (scenario["template_enabled"] or not template_used)
            and (scenario["gap_enabled"] or not gap_used)
            and build_errors == 0
            and bool(_successful_build_calls(build_calls))
        )
        notes = []
        if exposure_ok is True:
            notes.append("Tool list matches the scenario.")
        elif exposure_ok is False:
            notes.append("Tool list does not match the scenario expectation.")
        if not build_calls:
            notes.append(
                "Workflow build stage was not reached; build_workflow_bulk was never called."
            )
            if any(call.tool_name == "list_forms" for call in calls):
                notes.append(
                    "Diverged to existing form list in new workflow test; this B result should be considered incomplete."
                )
        form_prompt_attempts = [call for call in build_calls if call.arguments.get("form_prompt")]
        created_form_builds = _created_form_builds(build_calls)
        final_trigger_form_id = str(build_arguments.get("trigger_form_id") or build_payload.get("trigger_form_id") or "")
        mcp_created_trigger = next((
            call for call in created_form_builds
            if not final_trigger_form_id or _trigger_form_id(call) == final_trigger_form_id
        ), None)
        if build_arguments.get("trigger_form_id") and not build_arguments.get("form_prompt"):
            if mcp_created_trigger:
                notes.append(
                    "Trigger form was created with form_prompt in the same session; "
                    "final build used this ready trigger_form_id."
                )
            else:
                notes.append(
                    "Form was created via ChatGPT plugin/tool; "
                    "workflow used the pre-created trigger_form_id in final build."
                )
            if form_prompt_attempts:
                notes.append(
                    "Tried form_prompt earlier in session; "
                    "final workflow build attached to a ready trigger_form_id after this try."
                )
        elif build_arguments.get("form_prompt"):
            notes.append("Trigger form was created with form_prompt in MCP build_workflow_bulk.")
        if template_result_counts and max(template_result_counts) == 0:
            notes.append("Template search was called but returned no matching templates.")
        elif template_result_counts:
            notes.append(f"Template search returned {max(template_result_counts)} matches.")
        empty_build_attempts = [
            call for call in build_calls
            if not (call.arguments.get("steps") or [])
        ]
        if empty_build_attempts:
            notes.append(f"{len(empty_build_attempts)} build_workflow_bulk calls were made without steps.")
        if build_errors:
            notes.append(f"{build_errors} build_workflow_bulk attempts returned an error field.")
        if len(distinct_workflow_ids) > 1:
            final_workflow_id = str(build_payload.get("workflow_id") or "")
            abandoned = [workflow_id for workflow_id in distinct_workflow_ids if workflow_id != final_workflow_id]
            notes.append(
                f"{len(distinct_workflow_ids)} distinct workflows created in session; "
                f"abandoned candidates: {', '.join(abandoned) or 'none'}."
            )
        notes.extend(_repeated_tool_notes(calls))
        if session_findings:
            notes.append(f"{len(session_findings)} dashboard findings generated.")
        if warnings:
            notes.append(f"{len(warnings)} normalization warnings present.")
        rows.append({
            "session": session,
            "scenario": scenario,
            "tool_profile": (session.metadata or {}).get("tool_profile") or raw_list_tools.get("tool_profile") or "",
            "tool_count": len(tool_spans),
            "api_count": len(api_spans),
            "first_build_ms": _call_duration_ms(first_build),
            "final_build_ms": _call_duration_ms(final_build),
            "time_to_first_build_ms": _time_to_call_end_ms(session, first_build, spans),
            "time_to_final_build_ms": _time_to_call_end_ms(session, final_build, spans),
            "post_build_ms": _post_call_ms(session, first_build),
            "build_attempt_count": len(build_calls),
            "successful_build_count": len(successful_builds),
            "abandoned_workflow_count": max(len(distinct_workflow_ids) - 1, 0),
            "tool_ms": round(sum(float(span.duration_ms or 0) for span in tool_spans), 1),
            "api_ms": round(sum(float(span.duration_ms or 0) for span in api_spans), 1),
            "template_used": template_used,
            "gap_used": gap_used,
            "build_errors": build_errors,
            "finding_count": len(session_findings),
            "finding_wasted_ms": round(finding_wasted_ms, 1),
            "workflow_id": build_payload.get("workflow_id") or "",
            "workflow_url": build_payload.get("workflow_url") or "",
            "trigger_form_id": build_payload.get("trigger_form_id") or "",
            "created_step_count": len(created_steps) if isinstance(created_steps, dict) else len(created_steps or []),
            "created_link_count": int(build_payload.get("created_links_count") or len(created_links or [])),
            "warning_count": len(warnings) if isinstance(warnings, list) else 0,
            "tool_sequence": [call.tool_name for call in calls],
            "advertised_tool_count": int(raw_list_tools.get("tool_count") or len(advertised_tools or [])),
            "template_exposed": template_exposed,
            "gap_exposed": gap_exposed,
            "exposure_ok": exposure_ok,
            "behavior_ok": behavior_ok,
            "notes": notes,
        })
        if len(rows) >= limit:
            break
    if len(_EXPERIMENT_CACHE) > 8:
        _EXPERIMENT_CACHE.clear()
    _EXPERIMENT_CACHE[cache_key] = rows
    return rows


def experiment_summary(rows: list[dict]) -> dict:
    completed = sum(row["session_count"] for row in rows)
    measured_rows = [row for row in rows if row["session_count"] and row["avg_first_build_ms"]]
    return {
        "completed_runs": completed,
        "profiles_ready": sum(1 for row in rows if row["session_count"]),
        "avg_first_build_ms": round(
            sum(row["avg_first_build_ms"] * row["session_count"] for row in measured_rows)
            / sum(row["session_count"] for row in measured_rows),
            1,
        ) if measured_rows else 0,
        "total_quality_issues": sum(row["quality_issues"] for row in rows),
        "total_findings": sum(row["finding_count"] for row in rows),
        "total_finding_wasted_ms": round(sum(row["finding_wasted_ms"] for row in rows), 1),
        "total_build_errors": sum(row["build_errors"] for row in rows),
        "total_missing_builds": sum(row["missing_builds"] for row in rows),
    }


def experiment_image_rows() -> list[dict]:
    image_root = settings.MCP_LOG_ROOT / "mcp_server" / "logs" / "img"
    rows = []
    for scenario in AB_SCENARIOS:
        label = scenario["label"]
        matches = []
        if image_root.exists():
            for suffix in IMAGE_EXTENSIONS:
                matches.extend(image_root.glob(f"{label}*{suffix}"))
                matches.extend(image_root.glob(f"{label.lower()}*{suffix}"))
        files = sorted(
            {path for path in matches if path.is_file()},
            key=lambda path: (_image_round(path.name), path.name.lower()),
            reverse=True,
        )
        rows.append({
            "scenario": scenario,
            "filename": files[0].name if files else "",
            "filenames": [path.name for path in files],
            "image_root": str(image_root),
        })
    return rows


def _image_round(filename: str) -> int:
    match = re.search(r"(\d+)(?=\.[^.]+$)", filename)
    return int(match.group(1)) if match else 0

from __future__ import annotations

import json
from typing import Any


import re


def parse_json_string(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    trimmed = value.strip()
    if (trimmed.startswith("{") and trimmed.endswith("}")) or (trimmed.startswith("[") and trimmed.endswith("]")):
        try:
            return json.loads(trimmed)
        except (json.JSONDecodeError, TypeError):
            return value
    return value


def clean_and_normalize_data(value: Any, depth: int = 0) -> Any:
    """
    Recursively normalizes data by parsing nested JSON strings, decoding MCP tool response
    wrappers, and recovering clean previews from truncated audit log entries.
    """
    if depth > 12:
        return value

    value = parse_json_string(value)

    if isinstance(value, str):
        trimmed = value.strip()
        if (trimmed.startswith("{") and trimmed.endswith("}")) or (trimmed.startswith("[") and trimmed.endswith("]")):
            try:
                parsed = json.loads(trimmed)
                return clean_and_normalize_data(parsed, depth + 1)
            except Exception:
                pass
        return value

    if isinstance(value, dict):
        # Handle truncated audit log preview objects: {"truncated": True, "chars": ..., "preview": "..."}
        if value.get("truncated") and "preview" in value and len(value) <= 4:
            preview_val = value["preview"]
            if isinstance(preview_val, str):
                # Try direct json decode
                trimmed_p = preview_val.strip()
                if (trimmed_p.startswith("{") and trimmed_p.endswith("}")) or (trimmed_p.startswith("[") and trimmed_p.endswith("]")):
                    try:
                        parsed = json.loads(trimmed_p)
                        return clean_and_normalize_data(parsed, depth + 1)
                    except Exception:
                        pass
                # Check for MCP text wrapper inside preview: "text": "{\n  ...
                m = re.search(r"\"text\":\s*\"(.*)", preview_val, re.DOTALL)
                if m:
                    raw_inner = m.group(1).replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")
                    try:
                        parsed_inner = json.loads(raw_inner)
                        return clean_and_normalize_data(parsed_inner, depth + 1)
                    except Exception:
                        return raw_inner
                # Check for escaped JSON string starting with "{\"
                if preview_val.startswith('"{\\') or preview_val.startswith('"{'):
                    try:
                        unescaped = json.loads(preview_val)
                        return clean_and_normalize_data(unescaped, depth + 1)
                    except Exception:
                        pass

        # Handle MCP content wrapper: {"content": [{"type": "text", "text": "..."}]}
        if "content" in value and isinstance(value["content"], list) and len(value["content"]) == 1:
            item = value["content"][0]
            if isinstance(item, dict) and item.get("type") == "text" and "text" in item:
                text_val = item["text"]
                if isinstance(text_val, str):
                    trimmed_t = text_val.strip()
                    if (trimmed_t.startswith("{") and trimmed_t.endswith("}")) or (trimmed_t.startswith("[") and trimmed_t.endswith("]")):
                        try:
                            parsed_inner = json.loads(trimmed_t)
                            return clean_and_normalize_data(parsed_inner, depth + 1)
                        except Exception:
                            pass
                    return text_val

        return {k: clean_and_normalize_data(v, depth + 1) for k, v in value.items()}

    if isinstance(value, (list, tuple)):
        return [clean_and_normalize_data(item, depth + 1) for item in value]

    return value


def pretty_payload(value: Any, *, max_chars: int = 60000) -> str:
    if value is None or value == "":
        return "{}"

    normalized = clean_and_normalize_data(value)

    if isinstance(normalized, str):
        rendered = normalized
    else:
        rendered = json.dumps(normalized, ensure_ascii=False, indent=2, default=str)

    if len(rendered) <= max_chars:
        return rendered

    hidden = len(rendered) - max_chars
    return f"{rendered[:max_chars]}\n\n... {hidden:,} more characters hidden"


def human_value(value: Any, *, max_chars: int = 8000) -> str:
    normalized = clean_and_normalize_data(value)
    if normalized is None or normalized == "":
        return "—"
    if isinstance(normalized, bool):
        return "Yes" if normalized else "No"
    if isinstance(normalized, (int, float)):
        return str(normalized)
    if isinstance(normalized, list) and all(not isinstance(item, (dict, list)) for item in normalized):
        rendered = ", ".join(str(item) for item in normalized) or "—"
    elif isinstance(normalized, dict) and not normalized:
        return "No parameters"
    elif isinstance(normalized, (dict, list)):
        rendered = json.dumps(normalized, ensure_ascii=False, indent=2, default=str)
    else:
        rendered = str(normalized)
    return rendered if len(rendered) <= max_chars else f"{rendered[:max_chars]}…"


def compact_nested_value(value: Any) -> str:
    normalized = clean_and_normalize_data(value)
    if isinstance(normalized, dict):
        keys = [str(key).replace("_", " ") for key in normalized.keys()]
        preview = ", ".join(keys[:6])
        suffix = f": {preview}" if preview else ""
        return f"Object · {len(normalized)} fields{suffix}"
    if isinstance(normalized, list):
        return f"List · {len(normalized)} items"
    return human_value(normalized)


def payload_fields(value: Any, *, max_items: int = 50, compact_nested: bool = False) -> list[dict[str, str]]:
    normalized = clean_and_normalize_data(value)
    if not isinstance(normalized, dict):
        return [{"name": "Value", "value": human_value(normalized)}]
    render = compact_nested_value if compact_nested else human_value
    fields = [{"name": str(key).replace("_", " "), "value": render(item)} for key, item in list(normalized.items())[:max_items]]
    if len(normalized) > max_items:
        fields.append({"name": "More", "value": f"{len(normalized) - max_items} fields hidden"})
    return fields


def result_summary(value: Any) -> str:
    normalized = clean_and_normalize_data(value)
    if isinstance(normalized, dict):
        if normalized.get("error"):
            return human_value(normalized["error"], max_chars=400)
        for key in ("message", "hint", "title", "status"):
            if normalized.get(key):
                return human_value(normalized[key], max_chars=400)
        non_empty = [key for key, item in normalized.items() if item not in (None, "", [], {})]
        if non_empty:
            return f"Returned {len(non_empty)} fields: {', '.join(non_empty[:6])}"
        return "Empty successful response"
    if isinstance(normalized, list):
        return f"Returned {len(normalized)} items"
    text = human_value(normalized, max_chars=400)
    return text or "Response received"


def event_by_phase(raw_events: list[dict[str, Any]] | None, phase: str) -> dict[str, Any]:
    for event in raw_events or []:
        if str(event.get("event_type") or "").endswith(f".{phase}"):
            return event
    return {}


def clean_payload(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item not in (None, "", [], {})}


def exchange_message(*, sender: str, receiver: str, label: str, payload: Any, role: str, meta: str = "") -> dict[str, Any]:
    return {
        "sender": sender,
        "receiver": receiver,
        "label": label,
        "role": role,
        "meta": meta,
        "fields": payload_fields(payload, max_items=8, compact_nested=True),
        "raw": pretty_payload(payload, max_chars=30000),
    }


def response_from_event(event: dict[str, Any], fallback: Any) -> Any:
    if event.get("error"):
        return {"error": event["error"]}
    if "response_text" in event:
        return parse_json_string(event.get("response_text"))
    if "result" in event:
        return parse_json_string(event.get("result"))
    return fallback


def operation_card(span, raw_events: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    card: dict[str, Any] = {
        "span": span,
        "kind": span.kind,
        "title": span.name,
        "label": {"model": "MODEL STEP", "tool": "MCP TOOL", "external": "JOTFORM API"}.get(span.kind, "OPERATION"),
        "summary": "Operation completed" if span.status == "ok" else "Operation in progress" if span.status == "running" else "Operation failed",
        "method": "",
        "endpoint": "",
        "fields": [],
        "exchanges": [],
        "raw": pretty_payload(span.attributes),
    }
    tool = getattr(span, "tool_call", None)
    external = getattr(span, "external_call", None)
    model = getattr(span, "model_step", None)
    started_event = event_by_phase(raw_events, "started")
    completed_event = event_by_phase(raw_events, "completed") or event_by_phase(raw_events, "failed")
    if tool:
        card["title"] = tool.tool_name
        card["summary"] = result_summary(tool.result)
        card["fields"] = payload_fields(tool.arguments)
        card["raw"] = pretty_payload(tool.result)
        card["raw_label"] = "Tool result"
        request_payload = started_event.get("arguments", tool.arguments) if started_event else tool.arguments
        response_payload = response_from_event(completed_event, tool.result) if completed_event else tool.result
        card["exchanges"] = [
            exchange_message(
                sender="MCP client", receiver="MCP server", label="Tool request", role="request",
                payload=clean_payload({"tool": tool.tool_name, "arguments": request_payload}),
                meta=span.request_id,
            ),
            exchange_message(
                sender="MCP server", receiver="MCP client", label="Tool response", role="response",
                payload=response_payload, meta=f"{tool.result_bytes:,} byte",
            ),
        ]
    elif external:
        request_payload = clean_payload({
            "method": started_event.get("method") or external.method,
            "url": started_event.get("url") or external.url_template,
            "params": started_event.get("params") or span.attributes.get("params"),
            "json_body": started_event.get("json_body") if started_event else span.attributes.get("json_body"),
        })
        response_payload = response_from_event(completed_event, span.attributes.get("response") or span.attributes.get("response_text") or {})
        status_code = completed_event.get("status_code") or external.status_code
        card["title"] = f"{external.method} {external.url_template}"
        card["method"] = external.method
        card["endpoint"] = external.url_template
        card["summary"] = f"HTTP {status_code}" if status_code else "Waiting for HTTP response"
        if span.status == "error" and completed_event.get("error"):
            card["summary"] = human_value(completed_event["error"], max_chars=160)
        card["fields"] = [
            {"name": "Service", "value": external.service},
            {"name": "Method", "value": external.method},
            {"name": "Endpoint", "value": external.url_template},
            {"name": "HTTP status", "value": status_code or "—"},
            {"name": "Request size", "value": f"{external.request_bytes:,} bytes"},
            {"name": "Response size", "value": f"{external.response_bytes:,} bytes"},
        ]
        card["exchanges"] = [
            exchange_message(
                sender="MCP server", receiver=f"{external.service.title()} API", label="HTTP request", role="request",
                payload=request_payload, meta=span.request_id,
            ),
            exchange_message(
                sender=f"{external.service.title()} API", receiver="MCP server", label="HTTP response", role="response",
                payload=response_payload, meta=f"HTTP {status_code}" if status_code else "",
            ),
        ]
        card["raw"] = pretty_payload({"request": request_payload, "response": response_payload})
        card["raw_label"] = "HTTP exchange details"
    elif model:
        tokens = [
            f"{model.input_tokens:,} input" if model.input_tokens is not None else None,
            f"{model.output_tokens:,} output" if model.output_tokens is not None else None,
            f"{model.cached_tokens:,} cached" if model.cached_tokens is not None else None,
        ]
        card["summary"] = " · ".join(value for value in tokens if value) or "Token usage not reported"
        card["fields"] = [
            {"name": "Step", "value": str(model.step_no)},
            {"name": "Stop reason", "value": model.stop_reason or "—"},
        ]
        card["raw_label"] = "Model metadata"
    else:
        card["fields"] = payload_fields(span.attributes)
        card["raw_label"] = "Operation metadata"
    return card


def session_created_resources(session) -> dict[str, Any]:
    """
    Extract created or mutated workflows and forms from a session's tool calls.
    """
    from apps.traces.models import ToolCall

    workflows_map: dict[str, dict[str, Any]] = {}
    forms_map: dict[str, dict[str, Any]] = {}

    tool_calls = list(
        getattr(session, "_prefetched_tool_calls", None)
        or ToolCall.objects.filter(span__session=session).select_related("span")
    )

    for call in tool_calls:
        args = parse_json_string(call.arguments) or {}
        res = parse_json_string(call.result) or {}
        tool_name = call.tool_name

        if not isinstance(res, dict):
            continue

        # Extract workflows
        wf_id = str(res.get("workflow_id") or args.get("workflow_id") or "").strip()
        wf_url = res.get("workflow_url") or (f"https://www.jotform.com/workflow/{wf_id}/build" if wf_id else "")
        title = args.get("title") or res.get("title") or res.get("form_title") or ""
        created_steps = res.get("created_steps") or {}
        deleted_steps = res.get("deleted_steps") or []
        created_links_count = res.get("created_links_count") or len(res.get("created_links") or [])
        trigger_form_id = str(res.get("trigger_form_id") or args.get("trigger_form_id") or "").strip()
        trigger_form_url = res.get("trigger_form_url") or (f"https://www.jotform.com/build/{trigger_form_id}" if trigger_form_id else "")

        if wf_id:
            if wf_id not in workflows_map:
                workflows_map[wf_id] = {
                    "workflow_id": wf_id,
                    "title": title or f"Workflow #{wf_id}",
                    "workflow_url": wf_url,
                    "trigger_form_id": trigger_form_id,
                    "trigger_form_url": trigger_form_url,
                    "created_steps": created_steps,
                    "step_count": len(created_steps) if isinstance(created_steps, (dict, list)) else 0,
                    "deleted_steps": deleted_steps,
                    "links_count": created_links_count,
                    "published": bool(res.get("published")),
                    "is_created": tool_name in ("build_workflow_bulk", "create_workflow", "create_workflow_with_ai_form"),
                }
            else:
                existing = workflows_map[wf_id]
                if title and existing["title"].startswith("Workflow #"):
                    existing["title"] = title
                if trigger_form_id and not existing["trigger_form_id"]:
                    existing["trigger_form_id"] = trigger_form_id
                    existing["trigger_form_url"] = trigger_form_url
                if created_steps:
                    if isinstance(existing["created_steps"], dict) and isinstance(created_steps, dict):
                        existing["created_steps"].update(created_steps)
                        existing["step_count"] = len(existing["created_steps"])
                if deleted_steps:
                    existing["deleted_steps"] = list(set(existing["deleted_steps"] + deleted_steps))
                if res.get("published"):
                    existing["published"] = True

        # Extract forms
        form_id = str(res.get("form_id") or res.get("trigger_form_id") or res.get("resource_id") or "").strip()
        if not form_id and tool_name == "create_form_with_ai" and args.get("form_id"):
            form_id = str(args.get("form_id")).strip()

        form_url = res.get("form_url") or res.get("trigger_form_url") or (f"https://www.jotform.com/build/{form_id}" if form_id else "")
        form_title = res.get("title") or res.get("form_title") or args.get("title") or args.get("form_prompt") or ""
        form_summary = res.get("summary") or res.get("form_summary") or ""

        if form_id:
            if form_id not in forms_map:
                forms_map[form_id] = {
                    "form_id": form_id,
                    "title": form_title or f"Form #{form_id}",
                    "form_url": form_url,
                    "summary": form_summary,
                    "is_created": tool_name in ("build_workflow_bulk", "create_form_with_ai", "create_workflow_with_ai_form"),
                }
            else:
                existing_f = forms_map[form_id]
                if form_title and existing_f["title"].startswith("Form #"):
                    existing_f["title"] = form_title
                if form_summary and not existing_f["summary"]:
                    existing_f["summary"] = form_summary

    return {
        "workflows": list(workflows_map.values()),
        "forms": list(forms_map.values()),
        "total_count": len(workflows_map) + len(forms_map),
    }


def format_duration_human(ms: float | None) -> str:
    if ms is None or ms <= 0:
        return "0 ms"
    if ms < 1000:
        return f"{ms:.0f} ms"
    sec = ms / 1000.0
    if sec < 60:
        return f"{sec:.1f}s"
    mins = int(sec // 60)
    rem_sec = int(sec % 60)
    return f"{mins}m {rem_sec}s" if rem_sec else f"{mins}m"


def session_timing_breakdown(session: Any, spans: list[Any] | None = None) -> dict[str, Any]:
    """
    Computes a 3-layer latency breakdown (LLM reasoning vs MCP local engine vs Jotform Cloud API)
    while isolating idle user pauses / long dead gaps (>120s) so active execution time is not distorted.
    """
    if spans is None:
        if hasattr(session, "spans"):
            spans = list(session.spans.all())
        else:
            spans = []

    # 1. Jotform API (HTTP) time: sum of 'external' spans
    ext_spans = [s for s in spans if getattr(s, "kind", None) == "external"]
    jotform_api_ms = sum(float(s.duration_ms or 0) for s in ext_spans)

    # 2. Tool spans total
    tool_spans = [s for s in spans if getattr(s, "kind", None) == "tool"]
    tool_spans_sorted = sorted([s for s in tool_spans if getattr(s, "started_at", None)], key=lambda s: s.started_at)
    tool_total_ms = sum(float(s.duration_ms or 0) for s in tool_spans)

    # MCP Internal logic (Auto-Layout, RAG, preflight validation)
    mcp_internal_ms = max(0.0, tool_total_ms - jotform_api_ms)

    # 3. LLM think time between tool calls & turns
    IDLE_THRESHOLD_MS = 120000.0  # 2 minutes
    llm_active_ms = 0.0
    idle_gap_ms = 0.0
    idle_gaps_count = 0

    for i in range(len(tool_spans_sorted) - 1):
        s1 = tool_spans_sorted[i]
        s2 = tool_spans_sorted[i + 1]
        if getattr(s1, "ended_at", None) and getattr(s2, "started_at", None):
            gap = (s2.started_at - s1.ended_at).total_seconds() * 1000.0
            if gap > 0:
                if gap <= IDLE_THRESHOLD_MS:
                    llm_active_ms += gap
                else:
                    llm_active_ms += IDLE_THRESHOLD_MS
                    idle_gap_ms += (gap - IDLE_THRESHOLD_MS)
                    idle_gaps_count += 1

    # If no multiple tool calls, check turn duration or session duration
    if not tool_spans_sorted and session:
        wall_ms = float(getattr(session, "duration_ms", None) or 0)
        if wall_ms <= IDLE_THRESHOLD_MS:
            llm_active_ms = wall_ms
        else:
            llm_active_ms = IDLE_THRESHOLD_MS
            idle_gap_ms = wall_ms - IDLE_THRESHOLD_MS
            idle_gaps_count = 1

    active_duration_ms = jotform_api_ms + mcp_internal_ms + llm_active_ms
    wall_duration_ms = float(getattr(session, "duration_ms", None) or active_duration_ms) if session else active_duration_ms

    # Check if extra wall time beyond active work is idle
    untracked_wall_diff = wall_duration_ms - (active_duration_ms + idle_gap_ms)
    if untracked_wall_diff > IDLE_THRESHOLD_MS:
        idle_gap_ms += untracked_wall_diff
        idle_gaps_count += 1

    # Compute percentages of active time
    denom = max(active_duration_ms, 1.0)
    pct_llm = round((llm_active_ms / denom) * 100.0, 1)
    pct_api = round((jotform_api_ms / denom) * 100.0, 1)
    pct_mcp = round((mcp_internal_ms / denom) * 100.0, 1)

    return {
        "active_duration_ms": active_duration_ms,
        "active_duration_formatted": format_duration_human(active_duration_ms),
        "wall_duration_ms": wall_duration_ms,
        "wall_duration_formatted": format_duration_human(wall_duration_ms),
        "jotform_api_ms": jotform_api_ms,
        "jotform_api_formatted": format_duration_human(jotform_api_ms),
        "mcp_internal_ms": mcp_internal_ms,
        "mcp_internal_formatted": format_duration_human(mcp_internal_ms),
        "llm_active_ms": llm_active_ms,
        "llm_active_formatted": format_duration_human(llm_active_ms),
        "idle_gap_ms": idle_gap_ms,
        "idle_gap_formatted": format_duration_human(idle_gap_ms),
        "idle_gaps_count": idle_gaps_count,
        "has_idle_gap": idle_gap_ms > 0,
        "pct_llm": pct_llm,
        "pct_api": pct_api,
        "pct_mcp": pct_mcp,
    }


def reconstruct_synthetic_turns(spans: list[Any], raw_events_by_request: dict[str, list[dict]] | None = None) -> list[dict[str, Any]]:
    """
    Groups raw spans into Synthetic Turns (Turlar / Islem Donguleri) when the client did not supply chat turns.
    Computes inter-tool gaps (LLM reasoning) and inter-turn gaps (User wait time / human delay).
    """
    if not spans:
        return []

    raw_events_by_request = raw_events_by_request or {}
    spans_sorted = sorted(spans, key=lambda s: (s.started_at or s.id, s.sequence_no or 0))

    turns_raw: list[dict[str, Any]] = []
    current_spans: list[Any] = []
    last_end = None

    for span in spans_sorted:
        gap_sec = 0.0
        if last_end and span.started_at:
            gap_sec = max(0.0, (span.started_at - last_end).total_seconds())

        is_new_turn = False
        if not current_spans:
            is_new_turn = True
        elif gap_sec > 25.0:
            is_new_turn = True
        elif gap_sec > 10.0 and span.name in ("search_workflow_templates", "build_workflow_bulk", "create_form_with_ai", "restore_workflow_revision"):
            is_new_turn = True

        if is_new_turn and current_spans:
            turns_raw.append({"spans": current_spans, "gap_before_sec": gap_sec})
            current_spans = []

        current_spans.append(span)
        if span.ended_at:
            last_end = max(last_end or span.ended_at, span.ended_at)

    if current_spans:
        turns_raw.append({"spans": current_spans, "gap_before_sec": 0.0})

    result: list[dict[str, Any]] = []
    for idx, t in enumerate(turns_raw, 1):
        t_spans = t["spans"]
        gap_before_sec = t["gap_before_sec"]
        
        start = t_spans[0].started_at
        end = t_spans[-1].ended_at or t_spans[-1].started_at
        dur_ms = (end - start).total_seconds() * 1000.0 if start and end else 0.0

        detected_intent = ""
        for sp in t_spans:
            tool_obj = getattr(sp, "tool_call", None)
            args = tool_obj.arguments if tool_obj and isinstance(tool_obj.arguments, dict) else {}
            candidate = args.get("intent") or args.get("query") or args.get("form_prompt") or args.get("reason") or args.get("title")
            if candidate:
                detected_intent = str(candidate).strip()
                break
        if not detected_intent:
            tool_names = [sp.name for sp in t_spans if sp.kind == "tool"]
            detected_intent = f"İşlem Döngüsü: {', '.join(tool_names[:3])}" if tool_names else "MCP İşlem Bloğu"

        operations = []
        prev_op_end = None
        for sp in t_spans:
            card = operation_card(sp, raw_events_by_request.get(sp.request_id))
            gap_ms = 0.0
            if prev_op_end and sp.started_at:
                gap_ms = max(0.0, (sp.started_at - prev_op_end).total_seconds() * 1000.0)
            
            card["gap_before_ms"] = gap_ms
            card["gap_formatted"] = format_duration_human(gap_ms)
            card["gap_type"] = "user_wait" if gap_ms > 120000.0 else "llm_reasoning" if gap_ms > 400.0 else "none"
            operations.append(card)
            if sp.ended_at:
                prev_op_end = max(prev_op_end or sp.ended_at, sp.ended_at)

        synth_turn = {
            "id": f"synthetic-{idx}",
            "sequence_no": idx,
            "question": detected_intent,
            "answer": f"Bu turda {len(t_spans)} işlem tamamlandı. (Aktif işlem süresi: {format_duration_human(dur_ms)})",
            "started_at": start,
            "ended_at": end,
            "duration_ms": dur_ms,
            "is_synthetic": True,
            "gap_before_formatted": format_duration_human(gap_before_sec * 1000.0) if gap_before_sec > 0 else "",
            "gap_before_sec": gap_before_sec,
        }
        result.append({"turn": synth_turn, "operations": operations})

    return result




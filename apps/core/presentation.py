from __future__ import annotations

import json
from typing import Any


def parse_json_string(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value


def pretty_payload(value: Any, *, max_chars: int = 8000) -> str:
    value = parse_json_string(value)
    if isinstance(value, str):
        rendered = value
    else:
        rendered = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    if len(rendered) <= max_chars:
        return rendered
    hidden = len(rendered) - max_chars
    return f"{rendered[:max_chars]}\n\n... {hidden:,} more characters hidden"


def human_value(value: Any, *, max_chars: int = 500) -> str:
    value = parse_json_string(value)
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list) and all(not isinstance(item, (dict, list)) for item in value):
        rendered = ", ".join(str(item) for item in value) or "—"
    elif isinstance(value, dict) and not value:
        return "No parameters"
    elif isinstance(value, (dict, list)):
        rendered = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    else:
        rendered = str(value)
    return rendered if len(rendered) <= max_chars else f"{rendered[:max_chars]}…"


def compact_nested_value(value: Any) -> str:
    value = parse_json_string(value)
    if isinstance(value, dict):
        keys = [str(key).replace("_", " ") for key in value.keys()]
        preview = ", ".join(keys[:3])
        suffix = f": {preview}" if preview else ""
        return f"Object · {len(value)} fields{suffix}"
    if isinstance(value, list):
        return f"List · {len(value)} items"
    return human_value(value)


def payload_fields(value: Any, *, max_items: int = 18, compact_nested: bool = False) -> list[dict[str, str]]:
    value = parse_json_string(value)
    if not isinstance(value, dict):
        return [{"name": "Value", "value": human_value(value)}]
    render = compact_nested_value if compact_nested else human_value
    fields = [{"name": str(key).replace("_", " "), "value": render(item)} for key, item in list(value.items())[:max_items]]
    if len(value) > max_items:
        fields.append({"name": "More", "value": f"{len(value) - max_items} fields hidden"})
    return fields


def result_summary(value: Any) -> str:
    value = parse_json_string(value)
    if isinstance(value, dict):
        if value.get("error"):
            return human_value(value["error"], max_chars=160)
        for key in ("message", "hint", "title", "status"):
            if value.get(key):
                return human_value(value[key], max_chars=160)
        non_empty = [key for key, item in value.items() if item not in (None, "", [], {})]
        if non_empty:
            return f"Returned {len(non_empty)} fields: {', '.join(non_empty[:4])}"
        return "Empty successful response"
    if isinstance(value, list):
        return f"Returned {len(value)} items"
    text = human_value(value, max_chars=160)
    return text or "Response received"


def event_by_phase(raw_events: list[dict[str, Any]] | None, phase: str) -> dict[str, Any]:
    for event in raw_events or []:
        if str(event.get("event_type") or "").endswith(f".{phase}"):
            return event
    return {}


def clean_payload(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item not in (None, "", [], {})}


def exchange_message(*, sender: str, receiver: str, label: str, payload: Any, role: str, meta: str = "") -> dict[str, Any]:
    payload = parse_json_string(payload)
    return {
        "sender": sender,
        "receiver": receiver,
        "label": label,
        "role": role,
        "meta": meta,
        "fields": payload_fields(payload, max_items=8, compact_nested=True),
        "raw": pretty_payload(payload, max_chars=4000),
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


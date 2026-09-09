from __future__ import annotations

import hashlib
import json
import os
import fcntl
from contextlib import contextmanager
from datetime import timedelta, timezone as datetime_timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from django.db import transaction
from django.db.utils import OperationalError
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.core.models import Workspace
from apps.traces.models import ExternalCall, ModelStep, Session, Span, Task, ToolCall, Turn, UsageRecord

from .models import IngestionSource, QuarantinedEvent, RawEvent

PARSER_VERSION = "1.0"


@contextmanager
def ingestion_lock(path: Path):
    """Prevent overlapping local/beat ingestion for the same physical file."""
    lock_name = hashlib.sha256(str(path).encode()).hexdigest()[:24]
    lock_path = Path("/tmp") / f"jotform-observability-{lock_name}.lock"
    with lock_path.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def as_datetime(value: Any):
    if not value or not isinstance(value, str):
        return None
    parsed = parse_datetime(value)
    if parsed and timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, datetime_timezone.utc)
    return parsed


def json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return {"text": value}
    return value if value is not None else {}


def result_issue_severity(result: Any, *, explicit_is_error: bool = False, phase: str = "completed") -> str:
    if phase == "failed" or explicit_is_error:
        return "error"
    if not isinstance(result, dict):
        return "ok"
    if result.get("error"):
        if (
            result.get("form_id")
            or result.get("workflow_id")
            or result.get("partial_success")
            or result.get("fallback_used")
            or result.get("ai_fallback")
        ):
            return "warning"
        return "error"
    if result.get("warnings") or result.get("health_warnings") or result.get("fallback_used"):
        return "warning"
    return "ok"


def get_workspace() -> Workspace:
    workspace, _ = Workspace.objects.get_or_create(
        slug="jotform-workflow-mcp",
        defaults={"name": "Jotform Workflow MCP", "environment": "development"},
    )
    return workspace


def detect_provider_and_model(payload: dict) -> tuple[str | None, str | None]:
    if payload.get("provider") and str(payload.get("provider")) not in ("mcp", "canonical", "unknown"):
        return str(payload["provider"]), str(payload.get("model") or "")

    headers = payload.get("headers") or {}
    if isinstance(headers, dict):
        ua = str(headers.get("user-agent") or "").lower()
        origin = str(headers.get("origin") or "").lower()
        if "claude" in ua or "anthropic" in ua or "claude.ai" in origin:
            return "anthropic", "Claude Connector"
        if "chatgpt" in ua or "openai" in ua or "chatgpt.com" in origin or "openai.com" in origin:
            return "openai", "ChatGPT Connector"
        if "gemini" in ua or "google" in ua:
            return "gemini", "Gemini Client"
        if "cursor" in ua:
            return "cursor", "Cursor IDE"

    query = payload.get("query") or {}
    if isinstance(query, dict):
        platform = str(query.get("platform") or query.get("client") or "").lower()
        if platform in ("claude", "anthropic"):
            return "anthropic", "Claude SSE"
        if platform in ("gpt", "openai", "chatgpt"):
            return "openai", "ChatGPT SSE"
        if platform == "gemini":
            return "gemini", "Gemini SSE"

    client_name = str(payload.get("client_name") or payload.get("client") or "").lower()
    if "claude" in client_name:
        return "anthropic", "Claude Desktop"
    if "openai" in client_name or "chatgpt" in client_name:
        return "openai", "ChatGPT Connector"
    if "gemini" in client_name:
        return "gemini", "Gemini Client"

    return None, None


def get_session(payload: dict, *, provider: str) -> Session:
    external_id = str(payload.get("session_id") or payload.get("run_id") or "unknown")
    det_prov, det_model = detect_provider_and_model(payload)
    effective_provider = det_prov or provider
    effective_model = det_model or str(payload.get("model") or "")

    session, created = Session.objects.get_or_create(
        workspace=get_workspace(),
        external_session_id=external_id,
        defaults={
            "provider": effective_provider,
            "model": effective_model,
            "agent_name": str(payload.get("agent_name") or effective_provider),
            "correlation_confidence": "high" if payload.get("trace_id") else "low",
        },
    )
    changed = []
    if effective_provider not in {"mcp", "canonical", "unknown"} and (not session.provider or session.provider in {"mcp", "canonical", "unknown"}):
        session.provider = effective_provider
        changed.append("provider")
    if effective_model and (not session.model or session.model in {"tool/API traffic", "unknown"}):
        session.model = effective_model
        changed.append("model")
    if payload.get("trace_id") and session.correlation_confidence != "high":
        session.correlation_confidence = "high"
        changed.append("correlation_confidence")
    if changed:
        session.save(update_fields=changed)
    if payload.get("task_id"):
        task, _ = Task.objects.get_or_create(
            workspace=session.workspace, external_id=str(payload["task_id"]),
            defaults={
                "name": str(payload.get("question") or ""),
                "status": "error" if payload.get("error") else "running",
                "started_at": as_datetime(payload.get("timestamp")),
            },
        )
        task_changed = []
        if payload.get("question") and not task.name:
            task.name = str(payload["question"])
            task_changed.append("name")
        if payload.get("question"):
            task.status = "error" if payload.get("error") else "ok"
            task_changed.append("status")
        if task_changed:
            task.save(update_fields=task_changed)
        if session.task_id != task.id:
            session.task = task
            session.save(update_fields=["task"])
    return session


def merge_session_metadata(session: Session, payload: dict) -> None:
    keys = (
        "tool_profile",
        "experiment_id",
        "experiment_scenario",
        "experiment_prompt_id",
        "experiment_prompt",
    )
    metadata = dict(session.metadata or {})
    changed = False
    for key in keys:
        value = payload.get(key)
        if value is not None and metadata.get(key) != value:
            metadata[key] = value
            changed = True
    if changed:
        session.metadata = metadata
        session.save(update_fields=["metadata"])


def update_session_bounds(session: Session) -> None:
    turns = session.turns.exclude(started_at=None)
    spans = session.spans.exclude(started_at=None)
    starts = [value for value in (turns.order_by("started_at").values_list("started_at", flat=True).first(), spans.order_by("started_at").values_list("started_at", flat=True).first()) if value]
    turn_ends = session.turns.exclude(ended_at=None).order_by("-ended_at").values_list("ended_at", flat=True).first()
    span_ends = session.spans.exclude(ended_at=None).order_by("-ended_at").values_list("ended_at", flat=True).first()
    ends = [value for value in (turn_ends, span_ends) if value]
    session.started_at = min(starts) if starts else session.started_at
    session.ended_at = max(ends) if ends else session.ended_at
    if session.started_at and session.ended_at:
        session.duration_ms = (session.ended_at - session.started_at).total_seconds() * 1000
    has_error = session.turns.filter(status="error").exists() or session.spans.filter(status="error").exists()
    has_running = session.spans.filter(status="running").exists()
    session.status = "error" if has_error else ("running" if has_running else "ok")
    session.save(update_fields=["started_at", "ended_at", "duration_ms", "status"])


def normalize_agent_turn(payload: dict) -> None:
    session = get_session(payload, provider=str(payload.get("provider") or "agent"))
    timestamp = as_datetime(payload.get("timestamp")) or timezone.now()
    duration_ms = float(payload.get("duration_ms") or 0)
    started = timestamp - timedelta(milliseconds=duration_ms)
    sequence = session.turns.count() + 1
    turn = Turn.objects.create(
        session=session,
        external_id=str(payload.get("turn_id") or ""),
        sequence_no=sequence,
        question=str(payload.get("question") or ""),
        answer=str(payload.get("answer") or ""),
        status="error" if payload.get("error") else "ok",
        started_at=started,
        ended_at=timestamp,
        duration_ms=duration_ms,
        input_tokens=payload.get("input_tokens"),
        output_tokens=payload.get("output_tokens"),
        cost_usd=payload.get("cost_usd"),
    )
    trace_id = str(payload.get("trace_id") or session.external_session_id)
    correlated = bool(payload.get("trace_id") and payload.get("turn_id"))
    if correlated:
        session.spans.filter(attributes__turn_id=str(payload["turn_id"]), turn=None).update(turn=turn)
    cursor = started
    for index, call in enumerate([] if correlated else (payload.get("tool_calls") or []), start=1):
        call_duration = float(call.get("duration_ms") or 0)
        ended = cursor + timedelta(milliseconds=call_duration)
        args = call.get("arguments") or {}
        result = json_value(call.get("result"))
        severity = result_issue_severity(result, explicit_is_error=bool(call.get("is_error")))
        is_error = severity == "error"
        span = Span.objects.create(
            session=session, turn=turn, trace_id=trace_id,
            external_span_id=str(call.get("span_id") or ""),
            request_id=str(call.get("request_id") or ""), kind="tool",
            name=str(call.get("name") or "unknown_tool"), sequence_no=index,
            started_at=cursor, ended_at=ended, duration_ms=call_duration,
            status="error" if is_error else "ok",
            attributes={"legacy_embedded": True, "timing_is_estimated": True, "result_severity": severity},
        )
        ToolCall.objects.create(
            span=span, tool_name=span.name, arguments=args, result=result,
            argument_hash=digest(args), result_hash=digest(result), is_error=is_error,
            result_bytes=len(stable_json(result).encode("utf-8")),
        )
        cursor = ended
    if payload.get("cost_usd") is not None:
        UsageRecord.objects.create(
            turn=turn, metric_type="total", quantity=1, unit="turn",
            cost_usd=payload["cost_usd"], pricing_version="legacy-agent-log", estimated=True,
        )
    update_session_bounds(session)


def url_template(url: str) -> str:
    parts = urlsplit(url)
    segments = []
    for item in parts.path.split("/"):
        segments.append("{id}" if item.isdigit() and len(item) >= 5 else item)
    return f"{parts.scheme}://{parts.netloc}{'/'.join(segments)}" if parts.netloc else "/".join(segments)


def nearest_open_tool(session: Session, timestamp):
    return session.spans.filter(kind="tool", status="running", started_at__lte=timestamp).order_by("-started_at").first()


def normalize_mcp_event(payload: dict) -> None:
    session = get_session(payload, provider="mcp")
    merge_session_metadata(session, payload)
    event_type = str(payload.get("event_type") or "")
    timestamp = as_datetime(payload.get("timestamp")) or timezone.now()
    request_id = str(payload.get("request_id") or "")
    phase = event_type.rsplit(".", 1)[-1]
    turn = session.turns.filter(external_id=str(payload.get("turn_id") or "")).first() if payload.get("turn_id") else None
    parent = session.spans.filter(external_span_id=str(payload.get("parent_span_id") or "")).first() if payload.get("parent_span_id") else None
    correlation_attributes = {
        key: payload[key] for key in ("task_id", "turn_id", "model_step_id") if payload.get(key)
    }
    if event_type.startswith("mcp.tool_call.") or event_type.startswith("function.call."):
        is_function = event_type.startswith("function.call.")
        tool_name = str(payload.get("function") if is_function else payload.get("tool") or "unknown_tool")
        if phase == "started":
            Span.objects.get_or_create(
                session=session, request_id=request_id,
                defaults={
                    "turn": turn, "parent": parent,
                    "trace_id": str(payload.get("trace_id") or session.external_session_id),
                    "external_span_id": str(payload.get("span_id") or ""),
                    "kind": "tool", "name": tool_name,
                    "sequence_no": session.spans.filter(kind="tool").count() + 1,
                    "started_at": timestamp, "status": "running",
                    "attributes": {"correlation_confidence": "high" if payload.get("trace_id") else "low", **correlation_attributes},
                },
            )
        else:
            span = session.spans.filter(request_id=request_id, kind="tool").first()
            if not span:
                span = Span.objects.create(session=session, turn=turn, parent=parent, request_id=request_id, external_span_id=str(payload.get("span_id") or ""), trace_id=str(payload.get("trace_id") or session.external_session_id), kind="tool", name=tool_name, started_at=timestamp, attributes={"missing_started_event": True, **correlation_attributes})
            span.ended_at = timestamp
            span.duration_ms = float(payload.get("duration_ms") or ((timestamp - span.started_at).total_seconds() * 1000 if span.started_at else 0))
            result = json_value(payload.get("result") or payload.get("error"))
            severity = str(payload.get("result_severity") or "").strip().lower()
            if severity not in {"ok", "warning", "error"}:
                severity = result_issue_severity(
                    result,
                    explicit_is_error=bool(payload.get("is_error")),
                    phase=phase,
                )
            span.status = "error" if severity == "error" else "ok"
            span.attributes = {**span.attributes, "result_severity": severity}
            span.save()
            ToolCall.objects.update_or_create(
                span=span,
                defaults={
                    "tool_name": span.name, "arguments": span.attributes.get("arguments", {}),
                    "result": result, "argument_hash": digest(span.attributes.get("arguments", {})),
                    "result_hash": digest(result), "is_error": span.status == "error",
                    "result_bytes": len(stable_json(result).encode("utf-8")),
                },
            )
        if phase == "started":
            span = session.spans.filter(request_id=request_id, kind="tool").first()
            if span:
                span.attributes = {**span.attributes, "arguments": payload.get("arguments") or {}}
                span.save(update_fields=["attributes"])
    elif event_type.startswith("jotform.request."):
        if phase == "started":
            parent = parent or nearest_open_tool(session, timestamp)
            span = Span.objects.create(
                session=session, turn=turn, parent=parent, request_id=request_id,
                trace_id=str(payload.get("trace_id") or session.external_session_id),
                external_span_id=str(payload.get("span_id") or ""), kind="external",
                name=f"{payload.get('method', 'HTTP')} {url_template(str(payload.get('url') or ''))}",
                sequence_no=session.spans.filter(kind="external").count() + 1,
                started_at=timestamp, status="running",
                attributes={
                    "method": payload.get("method") or "GET",
                    "url": payload.get("url") or "",
                    "params": payload.get("params") or {},
                    "json_body": payload.get("json_body"),
                    **correlation_attributes,
                },
            )
            ExternalCall.objects.create(
                span=span, service="jotform", method=str(payload.get("method") or "GET"),
                url_template=url_template(str(payload.get("url") or "")),
                request_bytes=len(stable_json({"params": payload.get("params"), "json_body": payload.get("json_body")}).encode("utf-8")),
            )
        else:
            span = session.spans.filter(request_id=request_id, kind="external").first()
            if not span:
                span = Span.objects.create(session=session, turn=turn, parent=parent, request_id=request_id, external_span_id=str(payload.get("span_id") or ""), trace_id=str(payload.get("trace_id") or session.external_session_id), kind="external", name=f"{payload.get('method', 'HTTP')} unknown", started_at=timestamp, attributes={"missing_started_event": True, **correlation_attributes})
                ExternalCall.objects.create(span=span, method=str(payload.get("method") or "GET"), url_template=url_template(str(payload.get("url") or "")))
            span.ended_at = timestamp
            span.duration_ms = float(payload.get("duration_ms") or ((timestamp - span.started_at).total_seconds() * 1000 if span.started_at else 0))
            span.status = "error" if phase == "failed" or int(payload.get("status_code") or 200) >= 400 else "ok"
            response_text = payload.get("response_text")
            span.attributes = {
                **span.attributes,
                "status_code": payload.get("status_code"),
                "response_text": response_text,
                "response": json_value(response_text) if response_text is not None else {},
                "error": payload.get("error") or "",
            }
            span.save()
            external = span.external_call
            external.status_code = payload.get("status_code")
            external.response_bytes = len(str(payload.get("response_text") or "").encode("utf-8"))
            external.save()
    elif event_type.startswith("model.step."):
        external_span_id = str(payload.get("span_id") or "")
        if phase == "started":
            Span.objects.get_or_create(
                session=session, external_span_id=external_span_id,
                defaults={
                    "turn": turn, "parent": parent,
                    "trace_id": str(payload.get("trace_id") or session.external_session_id),
                    "kind": "model", "name": str(payload.get("model") or "model"),
                    "sequence_no": int(payload.get("step_no") or 0), "started_at": timestamp,
                    "status": "running", "attributes": {"provider": payload.get("provider"), **correlation_attributes},
                },
            )
        else:
            span = session.spans.filter(external_span_id=external_span_id, kind="model").first()
            if not span:
                span = Span.objects.create(session=session, turn=turn, parent=parent, external_span_id=external_span_id, trace_id=str(payload.get("trace_id") or session.external_session_id), kind="model", name=str(payload.get("model") or "model"), started_at=timestamp, attributes={"missing_started_event": True, **correlation_attributes})
            span.ended_at = timestamp
            span.duration_ms = float(payload.get("duration_ms") or ((timestamp - span.started_at).total_seconds() * 1000 if span.started_at else 0))
            span.status = "error" if phase == "failed" else "ok"
            span.save()
            ModelStep.objects.update_or_create(
                span=span,
                defaults={
                    "step_no": int(payload.get("step_no") or span.sequence_no),
                    "stop_reason": str(payload.get("stop_reason") or ""),
                    "input_tokens": payload.get("input_tokens"),
                    "cached_tokens": payload.get("cached_tokens"),
                    "reasoning_tokens": payload.get("reasoning_tokens"),
                    "output_tokens": payload.get("output_tokens"),
                },
            )
    update_session_bounds(session)


def normalize_canonical(payload: dict) -> None:
    event_name = str(payload.get("event_name") or "")
    provider = str((payload.get("attributes") or {}).get("provider") or "canonical")
    session = get_session(payload, provider=provider)
    timestamp = as_datetime(payload.get("timestamp")) or timezone.now()
    span_id = str(payload.get("span_id") or payload.get("event_id") or "")
    phase = event_name.rsplit(".", 1)[-1]
    kind = "external" if event_name.startswith("external.") else "model" if event_name.startswith("model.") else "tool" if event_name.startswith("mcp.tool") else "other"
    if phase == "started":
        parent = session.spans.filter(external_span_id=str(payload.get("parent_span_id") or "")).first()
        Span.objects.get_or_create(
            session=session, external_span_id=span_id,
            defaults={
                "parent": parent, "trace_id": str(payload.get("trace_id") or session.external_session_id),
                "request_id": str(payload.get("request_id") or ""), "kind": kind,
                "name": str((payload.get("attributes") or {}).get("name") or event_name.rsplit(".", 1)[0]),
                "sequence_no": int(payload.get("sequence_no") or 0), "started_at": timestamp,
                "status": "running", "attributes": payload.get("attributes") or {},
            },
        )
    else:
        span = session.spans.filter(external_span_id=span_id).first()
        if not span:
            span = Span.objects.create(session=session, external_span_id=span_id, trace_id=str(payload.get("trace_id") or session.external_session_id), kind=kind, name=event_name.rsplit(".", 1)[0], started_at=timestamp, attributes={"missing_started_event": True})
        span.ended_at = timestamp
        span.duration_ms = float(payload.get("duration_ms") or ((timestamp - span.started_at).total_seconds() * 1000 if span.started_at else 0))
        span.status = str(payload.get("status") or ("error" if phase == "failed" else "ok"))
        span.attributes = {**span.attributes, **(payload.get("attributes") or {})}
        span.save()
        if kind == "tool":
            args, result = span.attributes.get("arguments") or {}, span.attributes.get("result") or {}
            ToolCall.objects.update_or_create(span=span, defaults={"tool_name": span.name, "arguments": args, "result": result, "argument_hash": digest(args), "result_hash": digest(result), "is_error": span.status == "error", "result_bytes": len(stable_json(result).encode())})
        elif kind == "model":
            ModelStep.objects.update_or_create(span=span, defaults={"step_no": span.sequence_no, "stop_reason": str(span.attributes.get("stop_reason") or ""), "input_tokens": span.attributes.get("input_tokens"), "cached_tokens": span.attributes.get("cached_tokens"), "reasoning_tokens": span.attributes.get("reasoning_tokens"), "output_tokens": span.attributes.get("output_tokens")})
    update_session_bounds(session)


def detect_kind(path: Path, payload: dict) -> str:
    if payload.get("schema_version") and payload.get("event_name"):
        return "canonical_v1"
    if "question" in payload and "tool_calls" in payload:
        return "agent_turn"
    if payload.get("event_type"):
        return "mcp_audit"
    if payload.get("revision_id") or "revisions" in path.parts:
        return "revision"
    return "unknown"


def normalize(payload: dict, kind: str) -> None:
    if kind == "agent_turn":
        normalize_agent_turn(payload)
    elif kind == "mcp_audit":
        normalize_mcp_event(payload)
    elif kind == "canonical_v1":
        normalize_canonical(payload)


def ingest_file(path_value: str | Path) -> dict[str, int | str]:
    path = Path(path_value).resolve()
    with ingestion_lock(path) as acquired:
        if not acquired:
            return {"path": str(path), "ingested": 0, "quarantined": 0, "status": "locked"}
        return _ingest_locked_file(path)


def _ingest_locked_file(path: Path) -> dict[str, int | str]:
    stat = path.stat()
    inode = str(stat.st_ino)
    source, _ = IngestionSource.objects.get_or_create(
        path=str(path), inode=inode,
        defaults={"kind": "auto", "parser_version": PARSER_VERSION},
    )
    source.last_seen_at = timezone.now()
    if stat.st_size < source.last_offset:
        source.status = "truncated"
        source.error_message = "File shrank while keeping the same inode; automatic reset was skipped to preserve safe idempotency."
        source.save()
        return {"path": str(path), "ingested": 0, "quarantined": 0, "status": "truncated"}
    ingested = quarantined = 0
    with path.open("rb") as stream:
        stream.seek(source.last_offset)
        while True:
            offset = stream.tell()
            line = stream.readline()
            if not line:
                break
            if not line.endswith(b"\n"):
                break
            try:
                payload = json.loads(line.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("JSONL line must be an object")
                kind = detect_kind(path, payload)
                with transaction.atomic():
                    _, created = RawEvent.objects.get_or_create(
                        source=source, source_offset=offset,
                        defaults={
                            "event_type": str(payload.get("event_name") or payload.get("event_type") or kind),
                            "occurred_at": as_datetime(payload.get("timestamp")),
                            "schema_version": int(payload.get("schema_version") or 0),
                            "payload": payload, "payload_hash": digest(payload),
                            "correlation_confidence": "high" if payload.get("trace_id") else "low",
                        },
                    )
                    if created:
                        normalize(payload, kind)
                        ingested += 1
                        source.kind = kind if source.kind == "auto" else source.kind
                        source.last_hash = digest(payload)
            except OperationalError:
                # A transient database lock is infrastructure state, not bad input.
                # Leave the checkpoint before this line so the next run retries it.
                break
            except Exception as exc:
                QuarantinedEvent.objects.create(
                    source=source, source_offset=offset,
                    raw_preview=line.decode("utf-8", errors="replace")[:4000],
                    error_type=type(exc).__name__, error_message=str(exc),
                )
                quarantined += 1
            source.last_offset = stream.tell()
    source.records_ingested += ingested
    source.last_ingested_at = timezone.now()
    source.status = "error" if quarantined else "healthy"
    source.error_message = f"{quarantined} line(s) quarantined" if quarantined else ""
    source.save()
    return {"path": str(path), "ingested": ingested, "quarantined": quarantined, "status": source.status}


def discover_log_files(root: str | Path) -> list[Path]:
    root = Path(root)
    candidates = [
        root / "agent" / "logs" / "turns.jsonl",
        root / "mcp_server" / "logs" / "mcp_audit.jsonl",
        *sorted((root / "mcp_server" / "logs" / "sessions").glob("*.jsonl")),
        *sorted((root / "mcp_server" / "revisions").glob("*.jsonl")),
    ]
    return [path for path in candidates if path.is_file()]


def reclassify_sessions(root: str | Path | None = None) -> dict[str, int]:
    from django.conf import settings
    root_path = Path(root or settings.MCP_LOG_ROOT)
    session_logs_dir = root_path / "mcp_server" / "logs" / "sessions"
    turns_file = root_path / "agent" / "logs" / "turns.jsonl"

    turns_map: dict[str, dict] = {}
    if turns_file.exists():
        with turns_file.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                try:
                    t = json.loads(line)
                    sid = str(t.get("session_id") or "").strip()
                    if sid:
                        turns_map[sid] = t
                except Exception:
                    pass

    summary = {"claude": 0, "gemini": 0, "gpt": 0, "mcp": 0, "updated": 0}

    for sess in Session.objects.all():
        sid = sess.external_session_id
        original_prov = sess.provider
        original_model = sess.model

        new_prov = sess.provider
        new_model = sess.model

        # 1. Check turns.jsonl
        if sid in turns_map:
            t = turns_map[sid]
            new_prov = t.get("provider") or new_prov
            new_model = t.get("model") or new_model

        # 2. Check experiment metadata
        meta = sess.metadata or {}
        exp_id = str(meta.get("experiment_id") or "")
        if "abcd" in exp_id or "dealer" in exp_id or "warranty" in exp_id or "customer-returns" in exp_id:
            new_prov = "anthropic"
            if not new_model or new_model in ("tool/API traffic", "unknown"):
                new_model = "claude-3-5-sonnet-20241022"

        # 3. Check session logs for User-Agent & Origin
        if session_logs_dir.exists():
            matching = list(session_logs_dir.glob(f"*{sid}*.jsonl"))
            for match_path in matching:
                try:
                    with match_path.open("r", encoding="utf-8", errors="ignore") as fp:
                        for line in fp:
                            ev = json.loads(line)
                            hdrs = ev.get("headers") or {}
                            ua = str(hdrs.get("user-agent") or hdrs.get("User-Agent") or "").lower()
                            orig = str(hdrs.get("origin") or hdrs.get("Origin") or "").lower()
                            if "claude" in ua or "anthropic" in ua or "claude.ai" in orig:
                                new_prov = "anthropic"
                                if not new_model or new_model in ("tool/API traffic", "unknown"):
                                    new_model = "Claude Connector"
                            elif "chatgpt" in ua or "openai" in ua or "chatgpt.com" in orig or "openai.com" in orig:
                                new_prov = "openai"
                                if not new_model or new_model in ("tool/API traffic", "unknown"):
                                    new_model = "ChatGPT Connector"
                            elif "gemini" in ua or "google" in ua:
                                new_prov = "gemini"
                                if not new_model or new_model in ("tool/API traffic", "unknown"):
                                    new_model = "Gemini Client"
                except Exception:
                    pass

        # 4. Known historical tunnel/agent sessions
        chatgpt_prefixes = (
            "f985c6c7ed51", "ce7b99e039b2", "1e4f32b0ec55",
            "f87c6bccb5b7", "a127ab00af4d", "ffe97588d865",
            "4ee4aa19ddc8", "f625764a9da8", "4cec10af05d9",
            "ec6eb26c8d18", "534a4d8bbd6b", "7cbe93d2eab9", "c6eb564b519b"
        )
        if any(sid.startswith(p) for p in chatgpt_prefixes):
            new_prov = "openai"
            if not new_model or new_model in ("tool/API traffic", "unknown"):
                new_model = "ChatGPT Developer Connector"

        gemini_prefixes = ("82de46c0ca25", "d25838cfe792", "af0712b3133f")
        if any(sid.startswith(p) for p in gemini_prefixes):
            new_prov = "gemini"
            if not new_model or new_model in ("tool/API traffic", "unknown"):
                new_model = "gemini-3.6-flash"

        # 5. Fallback if empty
        if not new_prov:
            new_prov = "mcp"

        changed_fields = []
        if new_prov != original_prov:
            sess.provider = new_prov
            changed_fields.append("provider")
        if new_model != original_model:
            sess.model = new_model
            changed_fields.append("model")

        if changed_fields:
            sess.save(update_fields=changed_fields)
            summary["updated"] += 1

        p_lower = (sess.provider or "").lower()
        m_lower = (sess.model or "").lower()
        if "anthropic" in p_lower or "claude" in p_lower or "claude" in m_lower:
            summary["claude"] += 1
        elif "gemini" in p_lower or "google" in p_lower or "gemini" in m_lower:
            summary["gemini"] += 1
        elif "openai" in p_lower or "gpt" in p_lower or "chatgpt" in p_lower or "gpt" in m_lower:
            summary["gpt"] += 1
        else:
            summary["mcp"] += 1

    return summary


def ingest_tree(root: str | Path) -> list[dict]:
    results = [ingest_file(path) for path in discover_log_files(root)]
    reclassify_sessions(root)
    return results

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


def get_workspace() -> Workspace:
    workspace, _ = Workspace.objects.get_or_create(
        slug="jotform-workflow-mcp",
        defaults={"name": "Jotform Workflow MCP", "environment": "development"},
    )
    return workspace


def get_session(payload: dict, *, provider: str) -> Session:
    external_id = str(payload.get("session_id") or payload.get("run_id") or "unknown")
    session, created = Session.objects.get_or_create(
        workspace=get_workspace(),
        external_session_id=external_id,
        defaults={
            "provider": provider,
            "model": str(payload.get("model") or ""),
            "agent_name": str(payload.get("agent_name") or provider),
            "correlation_confidence": "high" if payload.get("trace_id") else "low",
        },
    )
    changed = []
    if provider not in {"mcp", "canonical"} and (not session.provider or session.provider in {"mcp", "canonical"}):
        session.provider = provider
        changed.append("provider")
    if payload.get("model") and not session.model:
        session.model = str(payload["model"])
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
        is_error = bool(call.get("is_error")) or bool(isinstance(result, dict) and result.get("error"))
        span = Span.objects.create(
            session=session, turn=turn, trace_id=trace_id,
            external_span_id=str(call.get("span_id") or ""),
            request_id=str(call.get("request_id") or ""), kind="tool",
            name=str(call.get("name") or "unknown_tool"), sequence_no=index,
            started_at=cursor, ended_at=ended, duration_ms=call_duration,
            status="error" if is_error else "ok",
            attributes={"legacy_embedded": True, "timing_is_estimated": True},
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
    event_type = str(payload.get("event_type") or "")
    timestamp = as_datetime(payload.get("timestamp")) or timezone.now()
    request_id = str(payload.get("request_id") or "")
    phase = event_type.rsplit(".", 1)[-1]
    turn = session.turns.filter(external_id=str(payload.get("turn_id") or "")).first() if payload.get("turn_id") else None
    parent = session.spans.filter(external_span_id=str(payload.get("parent_span_id") or "")).first() if payload.get("parent_span_id") else None
    correlation_attributes = {
        key: payload[key] for key in ("task_id", "turn_id", "model_step_id") if payload.get(key)
    }
    if event_type.startswith("mcp.tool_call."):
        if phase == "started":
            Span.objects.get_or_create(
                session=session, request_id=request_id,
                defaults={
                    "turn": turn, "parent": parent,
                    "trace_id": str(payload.get("trace_id") or session.external_session_id),
                    "external_span_id": str(payload.get("span_id") or ""),
                    "kind": "tool", "name": str(payload.get("tool") or "unknown_tool"),
                    "sequence_no": session.spans.filter(kind="tool").count() + 1,
                    "started_at": timestamp, "status": "running",
                    "attributes": {"correlation_confidence": "high" if payload.get("trace_id") else "low", **correlation_attributes},
                },
            )
        else:
            span = session.spans.filter(request_id=request_id, kind="tool").first()
            if not span:
                span = Span.objects.create(session=session, turn=turn, parent=parent, request_id=request_id, external_span_id=str(payload.get("span_id") or ""), trace_id=str(payload.get("trace_id") or session.external_session_id), kind="tool", name=str(payload.get("tool") or "unknown_tool"), started_at=timestamp, attributes={"missing_started_event": True, **correlation_attributes})
            span.ended_at = timestamp
            span.duration_ms = float(payload.get("duration_ms") or ((timestamp - span.started_at).total_seconds() * 1000 if span.started_at else 0))
            span.status = "error" if phase == "failed" or payload.get("is_error") else "ok"
            span.save()
            result = json_value(payload.get("result") or payload.get("error"))
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
        source.error_message = "Dosya aynı inode ile küçüldü; güvenli idempotency için otomatik sıfırlama yapılmadı."
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
                    raise ValueError("JSONL satırı object olmalı")
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
    source.error_message = f"{quarantined} satır quarantine'a alındı" if quarantined else ""
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


def ingest_tree(root: str | Path) -> list[dict]:
    return [ingest_file(path) for path in discover_log_files(root)]

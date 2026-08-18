from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from apps.traces.models import Session, Span, ToolCall

from .models import Finding, RuleSetting

READ_TOOLS = {"list_workflows", "get_workflow", "list_step_types", "get_step_schema", "get_form_fields", "get_revisions", "get_revision"}

RULES = {
    "DUPLICATE_EXACT_CALL": "The same tool was called repeatedly with identical normalized arguments in the same scope.",
    "REDUNDANT_READ": "The same resource was read again without a related write in between.",
    "RETRY_STORM": "The same failed call was repeated at least three times within a short period.",
    "INCOMPLETE_SPAN": "A started event exists, but no completed or failed event was received.",
}


def ensure_rule_settings() -> None:
    for code, description in RULES.items():
        RuleSetting.objects.get_or_create(rule_code=code, defaults={"description": description})


def fingerprint(rule_code: str, scope: str, argument_hash: str = "") -> str:
    return hashlib.sha256(f"{rule_code}:{scope}:{argument_hash}".encode()).hexdigest()


def create_finding(*, rule_code: str, session: Session, span: Span | None, title: str, description: str, recommendation: str, severity: str, confidence: str, wasted_ms: float, evidence: dict, scope: str, argument_hash: str = "") -> bool:
    setting = RuleSetting.objects.filter(rule_code=rule_code).first()
    if setting and not setting.enabled:
        return False
    finding, created = Finding.objects.get_or_create(
        fingerprint=fingerprint(rule_code, scope, argument_hash),
        defaults={
            "session": session, "turn": span.turn if span else None, "span": span,
            "rule_code": rule_code, "title": title, "description": description,
            "recommendation": recommendation, "severity": severity,
            "confidence": confidence, "wasted_ms": wasted_ms,
            "wasted_usd": Decimal("0"), "evidence": evidence,
        },
    )
    if not created:
        changed = []
        for field, value in {"title": title, "description": description, "recommendation": recommendation}.items():
            if getattr(finding, field) != value:
                setattr(finding, field, value)
                changed.append(field)
        if changed:
            finding.save(update_fields=changed)
    return created


def duplicate_rules() -> int:
    created = 0
    grouped: dict[tuple, list[ToolCall]] = defaultdict(list)
    calls = ToolCall.objects.select_related("span__session", "span__turn").order_by("span__started_at", "span__sequence_no")
    for call in calls:
        scope = str(call.span.turn_id or call.span.session_id)
        grouped[(scope, call.tool_name, call.argument_hash)].append(call)
    for (scope, tool_name, argument_hash), items in grouped.items():
        if len(items) < 2:
            continue
        first = items[0]
        wasted_ms = sum(float(item.span.duration_ms or 0) for item in items[1:])
        code = "REDUNDANT_READ" if tool_name in READ_TOOLS else "DUPLICATE_EXACT_CALL"
        created += int(create_finding(
            rule_code=code, session=first.span.session, span=first.span,
            title="Redundant read" if code == "REDUNDANT_READ" else "Duplicate exact call",
            description=f"{tool_name} was called {len(items)} times with identical arguments in this scope.",
            recommendation="Use a turn-scoped read cache and invalidate it after a write." if code == "REDUNDANT_READ" else "Reuse the previous result instead of sending the same tool call again.",
            severity="high" if len(items) >= 3 else "medium", confidence="high",
            wasted_ms=wasted_ms,
            evidence={"tool": tool_name, "count": len(items), "argument_hash": argument_hash, "span_ids": [str(item.span_id) for item in items]},
            scope=scope, argument_hash=argument_hash,
        ))
    return created


def retry_storm_rule() -> int:
    created = 0
    grouped: dict[tuple, list[ToolCall]] = defaultdict(list)
    for call in ToolCall.objects.filter(is_error=True).select_related("span__session", "span__turn").order_by("span__started_at"):
        grouped[(str(call.span.turn_id or call.span.session_id), call.tool_name, call.argument_hash)].append(call)
    for (scope, tool_name, argument_hash), items in grouped.items():
        if len(items) < 3:
            continue
        timestamps = [item.span.started_at for item in items if item.span.started_at]
        if len(timestamps) >= 2 and timestamps[-1] - timestamps[0] > timedelta(minutes=2):
            continue
        first = items[0]
        created += int(create_finding(
            rule_code="RETRY_STORM", session=first.span.session, span=first.span,
            title="Retry storm", description=f"{tool_name} produced {len(items)} failed attempts within a short period.",
            recommendation="Apply exponential backoff, jitter, and a maximum retry limit.",
            severity="high", confidence="high",
            wasted_ms=sum(float(item.span.duration_ms or 0) for item in items[1:]),
            evidence={"tool": tool_name, "count": len(items), "span_ids": [str(item.span_id) for item in items]},
            scope=scope, argument_hash=argument_hash,
        ))
    return created


def incomplete_span_rule() -> int:
    created = 0
    cutoff = timezone.now() - timedelta(minutes=5)
    for span in Span.objects.filter(status="running", started_at__lt=cutoff).select_related("session", "turn"):
        created += int(create_finding(
            rule_code="INCOMPLETE_SPAN", session=span.session, span=span,
            title="Incomplete span", description=f"{span.name} started, but no terminal event was received.",
            recommendation="Add a recovery job that closes the span as error or abandoned after a process crash or timeout.",
            severity="medium", confidence="high", wasted_ms=0,
            evidence={"span_id": str(span.id), "request_id": span.request_id, "started_at": span.started_at.isoformat() if span.started_at else None},
            scope=str(span.id),
        ))
    return created


def run_all_rules() -> int:
    ensure_rule_settings()
    return duplicate_rules() + retry_storm_rule() + incomplete_span_rule()

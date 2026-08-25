from __future__ import annotations

import uuid

from django.db import models


class Task(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey("core.Workspace", on_delete=models.CASCADE, related_name="tasks")
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="children")
    external_id = models.CharField(max_length=160, blank=True, db_index=True)
    name = models.CharField(max_length=500, blank=True)
    kind = models.CharField(max_length=80, default="agent_request")
    status = models.CharField(max_length=32, default="unknown", db_index=True)
    started_at = models.DateTimeField(null=True, blank=True, db_index=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    tags = models.JSONField(default=list, blank=True)


class Session(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey("core.Workspace", on_delete=models.CASCADE, related_name="sessions")
    task = models.ForeignKey(Task, null=True, blank=True, on_delete=models.SET_NULL, related_name="sessions")
    external_session_id = models.CharField(max_length=160, db_index=True)
    provider = models.CharField(max_length=80, blank=True, db_index=True)
    model = models.CharField(max_length=160, blank=True, db_index=True)
    agent_name = models.CharField(max_length=160, blank=True)
    status = models.CharField(max_length=32, default="unknown", db_index=True)
    correlation_confidence = models.CharField(max_length=16, default="low")
    started_at = models.DateTimeField(null=True, blank=True, db_index=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    duration_ms = models.FloatField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["workspace", "external_session_id"], name="unique_external_session")]
        ordering = ["-started_at"]

    @property
    def display_name(self) -> str:
        return self.external_session_id[:12]

    @property
    def platform(self) -> str:
        p = (self.provider or "").lower()
        m = (self.model or "").lower()
        if "test" in p or "test" in m or "ci" in p or "pytest" in m:
            return "test"
        if "anthropic" in p or "claude" in p or "claude" in m:
            return "claude"
        if "gemini" in p or "google" in p or "gemini" in m:
            return "gemini"
        if "openai" in p or "chatgpt" in p or "gpt" in p or "gpt" in m or "o1" in m or "o3" in m:
            return "gpt"
        return "mcp"

    @property
    def platform_display(self) -> str:
        mapping = {
            "claude": "Claude",
            "gemini": "Gemini",
            "gpt": "ChatGPT / GPT",
            "mcp": "MCP Direct",
            "test": "🧪 Tests / CI",
        }
        return mapping.get(self.platform, "MCP")


class Turn(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(Session, on_delete=models.CASCADE, related_name="turns")
    external_id = models.CharField(max_length=160, blank=True)
    sequence_no = models.PositiveIntegerField()
    question = models.TextField(blank=True)
    answer = models.TextField(blank=True)
    status = models.CharField(max_length=32, default="unknown")
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    duration_ms = models.FloatField(null=True, blank=True)
    input_tokens = models.BigIntegerField(null=True, blank=True)
    output_tokens = models.BigIntegerField(null=True, blank=True)
    cost_usd = models.DecimalField(max_digits=14, decimal_places=6, null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["session", "sequence_no"], name="unique_session_turn_sequence")]
        ordering = ["sequence_no"]


class Span(models.Model):
    KIND_CHOICES = [(value, value) for value in ("turn", "model", "tool", "external", "other")]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(Session, on_delete=models.CASCADE, related_name="spans")
    turn = models.ForeignKey(Turn, null=True, blank=True, on_delete=models.CASCADE, related_name="spans")
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="children")
    trace_id = models.CharField(max_length=64, db_index=True)
    external_span_id = models.CharField(max_length=160, blank=True, db_index=True)
    request_id = models.CharField(max_length=160, blank=True, db_index=True)
    kind = models.CharField(max_length=24, choices=KIND_CHOICES, default="other", db_index=True)
    name = models.CharField(max_length=255, db_index=True)
    sequence_no = models.PositiveIntegerField(default=0)
    started_at = models.DateTimeField(null=True, blank=True, db_index=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    duration_ms = models.FloatField(null=True, blank=True)
    status = models.CharField(max_length=32, default="unknown", db_index=True)
    attributes = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["started_at", "sequence_no"]
        indexes = [models.Index(fields=["turn", "started_at"]), models.Index(fields=["session", "kind"])]


class ModelStep(models.Model):
    span = models.OneToOneField(Span, primary_key=True, on_delete=models.CASCADE, related_name="model_step")
    step_no = models.PositiveIntegerField()
    stop_reason = models.CharField(max_length=80, blank=True)
    input_tokens = models.BigIntegerField(null=True, blank=True)
    cached_tokens = models.BigIntegerField(null=True, blank=True)
    reasoning_tokens = models.BigIntegerField(null=True, blank=True)
    output_tokens = models.BigIntegerField(null=True, blank=True)


class ToolCall(models.Model):
    span = models.OneToOneField(Span, primary_key=True, on_delete=models.CASCADE, related_name="tool_call")
    tool_name = models.CharField(max_length=255, db_index=True)
    arguments = models.JSONField(default=dict, blank=True)
    result = models.JSONField(default=dict, blank=True)
    argument_hash = models.CharField(max_length=64, db_index=True)
    result_hash = models.CharField(max_length=64, blank=True, db_index=True)
    is_error = models.BooleanField(default=False, db_index=True)
    result_bytes = models.PositiveBigIntegerField(default=0)


class ExternalCall(models.Model):
    span = models.OneToOneField(Span, primary_key=True, on_delete=models.CASCADE, related_name="external_call")
    service = models.CharField(max_length=100, default="jotform", db_index=True)
    method = models.CharField(max_length=16)
    url_template = models.CharField(max_length=500, db_index=True)
    status_code = models.PositiveIntegerField(null=True, blank=True, db_index=True)
    request_bytes = models.PositiveBigIntegerField(default=0)
    response_bytes = models.PositiveBigIntegerField(default=0)


class UsageRecord(models.Model):
    span = models.ForeignKey(Span, null=True, blank=True, on_delete=models.CASCADE, related_name="usage_records")
    turn = models.ForeignKey(Turn, null=True, blank=True, on_delete=models.CASCADE, related_name="usage_records")
    metric_type = models.CharField(max_length=64)
    quantity = models.DecimalField(max_digits=18, decimal_places=6)
    unit = models.CharField(max_length=32)
    cost_usd = models.DecimalField(max_digits=14, decimal_places=6, null=True, blank=True)
    pricing_version = models.CharField(max_length=80, default="legacy-unknown")
    estimated = models.BooleanField(default=True)
    recorded_at = models.DateTimeField(auto_now_add=True)

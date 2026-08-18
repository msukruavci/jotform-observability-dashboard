import uuid

from django.conf import settings
from django.db import models


class Finding(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey("traces.Session", on_delete=models.CASCADE, related_name="findings")
    turn = models.ForeignKey("traces.Turn", null=True, blank=True, on_delete=models.CASCADE, related_name="findings")
    span = models.ForeignKey("traces.Span", null=True, blank=True, on_delete=models.CASCADE, related_name="findings")
    rule_code = models.CharField(max_length=100, db_index=True)
    severity = models.CharField(max_length=16, default="medium", db_index=True)
    confidence = models.CharField(max_length=16, default="medium", db_index=True)
    status = models.CharField(max_length=24, default="open", db_index=True)
    title = models.CharField(max_length=255)
    description = models.TextField()
    recommendation = models.TextField(blank=True)
    wasted_ms = models.FloatField(default=0)
    wasted_usd = models.DecimalField(max_digits=14, decimal_places=6, default=0)
    evidence = models.JSONField(default=dict)
    fingerprint = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def display_title(self):
        return {
            "DUPLICATE_EXACT_CALL": "Duplicate exact call",
            "REDUNDANT_READ": "Redundant read",
            "RETRY_STORM": "Retry storm",
            "INCOMPLETE_SPAN": "Incomplete span",
        }.get(self.rule_code, self.title)

    @property
    def display_description(self):
        tool = self.evidence.get("tool", "The tool")
        count = self.evidence.get("count", 0)
        if self.rule_code in {"DUPLICATE_EXACT_CALL", "REDUNDANT_READ"}:
            return f"{tool} was called {count} times with identical arguments in this scope."
        if self.rule_code == "RETRY_STORM":
            return f"{tool} produced {count} failed attempts within a short period."
        if self.rule_code == "INCOMPLETE_SPAN":
            return f"{self.span.name if self.span else 'The span'} started, but no terminal event was received."
        return self.description

    @property
    def display_recommendation(self):
        return {
            "DUPLICATE_EXACT_CALL": "Reuse the previous result instead of sending the same tool call again.",
            "REDUNDANT_READ": "Use a turn-scoped read cache and invalidate it after a write.",
            "RETRY_STORM": "Apply exponential backoff, jitter, and a maximum retry limit.",
            "INCOMPLETE_SPAN": "Add a recovery job that closes the span as error or abandoned after a process crash or timeout.",
        }.get(self.rule_code, self.recommendation)


class Annotation(models.Model):
    finding = models.ForeignKey(Finding, on_delete=models.CASCADE, related_name="annotations")
    label = models.CharField(max_length=40)
    comment = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)


class RuleSetting(models.Model):
    rule_code = models.CharField(max_length=100, unique=True)
    enabled = models.BooleanField(default=True)
    severity = models.CharField(max_length=16, default="medium")
    parameters = models.JSONField(default=dict, blank=True)
    description = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

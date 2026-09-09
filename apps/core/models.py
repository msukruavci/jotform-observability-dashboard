import uuid
from django.utils import timezone

from django.db import models


class Workspace(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=160, unique=True)
    slug = models.SlugField(max_length=160, unique=True)
    environment = models.CharField(max_length=40, default="development")
    timezone = models.CharField(max_length=64, default="Europe/Istanbul")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.name


class DashboardCache(models.Model):
    key = models.CharField(max_length=255, unique=True, db_index=True)
    signature = models.CharField(max_length=500)
    value = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["key", "signature"])]

    def __str__(self) -> str:
        return self.key


MODEL_LIMITS = {
    "gemini-3.5-flash-lite": {"rpd": 500, "rpm": 15, "name": "Gemini 3.5 Flash Lite"},
    "gemini-3.1-flash-lite": {"rpd": 500, "rpm": 15, "name": "Gemini 3.1 Flash Lite"},
    "gemini-3.7-flash": {"rpd": 20, "rpm": 5, "name": "Gemini 3.7 Flash"},
    "gemini-3.6-flash": {"rpd": 20, "rpm": 5, "name": "Gemini 3.6 Flash"},
    "gemini-3.5-flash": {"rpd": 20, "rpm": 5, "name": "Gemini 3.5 Flash"},
}


class AITelemetryUsage(models.Model):
    date = models.DateField(default=timezone.now)
    model_name = models.CharField(max_length=64, default="gemini-3.5-flash-lite")
    request_count = models.IntegerField(default=0)
    
    class Meta:
        unique_together = ("date", "model_name")
    
    @classmethod
    def get_today_stats(cls):
        today = timezone.now().date()
        stats = {}
        for m_id, meta in MODEL_LIMITS.items():
            usage, _ = cls.objects.get_or_create(date=today, model_name=m_id)
            used = usage.request_count
            remaining = max(0, meta["rpd"] - used)
            stats[m_id] = {
                "id": m_id,
                "name": meta["name"],
                "rpd": meta["rpd"],
                "rpm": meta["rpm"],
                "used": used,
                "remaining": remaining,
            }
        return stats
        
    @classmethod
    def increment(cls, model_name="gemini-3.5-flash-lite"):
        today = timezone.now().date()
        usage, _ = cls.objects.get_or_create(date=today, model_name=model_name)
        usage.request_count = models.F('request_count') + 1
        usage.save()
        usage.refresh_from_db()
        return usage.request_count

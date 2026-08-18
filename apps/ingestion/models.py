from django.db import models


class IngestionSource(models.Model):
    path = models.TextField()
    kind = models.CharField(max_length=64, default="auto")
    inode = models.CharField(max_length=64, blank=True)
    last_offset = models.PositiveBigIntegerField(default=0)
    last_hash = models.CharField(max_length=64, blank=True)
    parser_version = models.CharField(max_length=32, default="1.0")
    status = models.CharField(max_length=32, default="new", db_index=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    last_ingested_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True)
    records_ingested = models.PositiveBigIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["path", "inode"], name="unique_source_path_inode")]

    def __str__(self) -> str:
        return self.path


class RawEvent(models.Model):
    source = models.ForeignKey(IngestionSource, on_delete=models.CASCADE, related_name="events")
    source_offset = models.PositiveBigIntegerField()
    event_type = models.CharField(max_length=160, db_index=True)
    occurred_at = models.DateTimeField(null=True, blank=True, db_index=True)
    schema_version = models.PositiveIntegerField(default=0)
    payload = models.JSONField()
    payload_hash = models.CharField(max_length=64, db_index=True)
    correlation_confidence = models.CharField(max_length=16, default="low")
    ingested_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["source", "source_offset"], name="unique_source_offset")]
        indexes = [models.Index(fields=["event_type", "occurred_at"])]


class QuarantinedEvent(models.Model):
    source = models.ForeignKey(IngestionSource, on_delete=models.CASCADE, related_name="quarantined_events")
    source_offset = models.PositiveBigIntegerField()
    raw_preview = models.TextField()
    error_type = models.CharField(max_length=120)
    error_message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

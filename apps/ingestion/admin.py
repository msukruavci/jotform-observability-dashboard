from django.contrib import admin

from .models import IngestionSource, QuarantinedEvent, RawEvent

admin.site.register(IngestionSource)
admin.site.register(RawEvent)
admin.site.register(QuarantinedEvent)


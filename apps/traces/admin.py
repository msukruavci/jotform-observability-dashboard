from django.contrib import admin

from .models import ExternalCall, ModelStep, Session, Span, Task, ToolCall, Turn, UsageRecord

for model in (Task, Session, Turn, Span, ModelStep, ToolCall, ExternalCall, UsageRecord):
    admin.site.register(model)


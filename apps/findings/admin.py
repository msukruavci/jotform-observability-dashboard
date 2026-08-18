from django.contrib import admin

from .models import Annotation, Finding, RuleSetting

admin.site.register(Finding)
admin.site.register(Annotation)
admin.site.register(RuleSetting)


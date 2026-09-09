from django.contrib import admin

from .models import DashboardCache, Workspace

admin.site.register(Workspace)
admin.site.register(DashboardCache)

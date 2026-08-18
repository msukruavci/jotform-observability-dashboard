from django.contrib import admin
from django.urls import include, path

from apps.core import views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", views.OverviewView.as_view(), name="overview"),
    path("sessions/", views.SessionListView.as_view(), name="session-list"),
    path("sessions/<uuid:pk>/", views.SessionDetailView.as_view(), name="session-detail"),
    path("tools/", views.ToolIntelligenceView.as_view(), name="tool-intelligence"),
    path("findings/", views.FindingsView.as_view(), name="findings"),
    path("costs/", views.CostView.as_view(), name="costs"),
    path("data-health/", views.DataHealthView.as_view(), name="data-health"),
    path("api/v1/", include("apps.core.api_urls")),
]


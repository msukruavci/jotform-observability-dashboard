from django.contrib import admin
from django.urls import include, path

from apps.core import views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", views.OverviewView.as_view(), name="overview"),
    path("sessions/", views.SessionListView.as_view(), name="session-list"),
    path("sessions/<uuid:pk>/", views.SessionDetailView.as_view(), name="session-detail"),
    path("sessions/<uuid:pk>/export/pdf-view/", views.SessionExportPDFView.as_view(), name="session-export-pdf-view"),
    path("sessions/<uuid:pk>/export/json/", views.SessionExportJSONView.as_view(), name="session-export-json"),
    path("sessions/<uuid:pk>/export/markdown/", views.SessionExportMarkdownView.as_view(), name="session-export-markdown"),
    path("tools/", views.ToolIntelligenceView.as_view(), name="tool-intelligence"),
    path("templates/", views.TemplateIntelligenceView.as_view(), name="template-intelligence"),
    path("templates/<str:template_id>/modal/", views.TemplateDetailModalView.as_view(), name="template-detail-modal"),
    path("findings/", views.FindingsView.as_view(), name="findings"),
    path("costs/", views.CostView.as_view(), name="costs"),
    path("data-health/", views.DataHealthView.as_view(), name="data-health"),
    path("api/v1/", include("apps.core.api_urls")),
]



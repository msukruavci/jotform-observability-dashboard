from django.contrib import admin
from django.urls import include, path

from apps.core import views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", views.OverviewView.as_view(), name="overview"),
    path("sessions/", views.SessionListView.as_view(), name="session-list"),
    path("sessions/<uuid:pk>/", views.SessionDetailView.as_view(), name="session-detail"),
    path("sessions/<uuid:pk>/ai-analysis/", views.SessionAIAnalysisView.as_view(), name="session-ai-analysis"),
    path("sessions/<uuid:pk>/export/pdf-view/", views.SessionExportPDFView.as_view(), name="session-export-pdf-view"),
    path("sessions/<uuid:pk>/export/json/", views.SessionExportJSONView.as_view(), name="session-export-json"),
    path("sessions/<uuid:pk>/export/markdown/", views.SessionExportMarkdownView.as_view(), name="session-export-markdown"),
    path("tools/", views.ToolIntelligenceView.as_view(), name="tool-intelligence"),
    path("tools/<str:tool_name>/modal/", views.ToolDetailModalView.as_view(), name="tool-detail-modal"),
    path("tools/<str:tool_name>/", views.ToolIntelligenceView.as_view(), name="tool-detail"),
    path("experiments/", views.ExperimentABCDView.as_view(), name="experiments"),
    path("experiments/images/<str:filename>", views.ExperimentImageView.as_view(), name="experiment-image"),
    path("templates/", views.TemplateIntelligenceView.as_view(), name="template-intelligence"),
    path("templates/<str:template_id>/modal/", views.TemplateDetailModalView.as_view(), name="template-detail-modal"),
    path("findings/", views.FindingsView.as_view(), name="findings"),
    path("costs/", views.CostView.as_view(), name="costs"),
    path("data-health/", views.DataHealthView.as_view(), name="data-health"),
    path("feature-requests/", views.FeatureRequestsView.as_view(), name="feature-requests"),
    path("function-traces/", views.FunctionTracesView.as_view(), name="function-traces"),
    path("generated-elements/", views.GeneratedElementsView.as_view(), name="generated-elements"),
    path("api/v1/", include("apps.core.api_urls")),
]

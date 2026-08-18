from django.urls import path

from .api_views import OverviewAPIView, SessionTraceAPIView, ToolsAPIView

urlpatterns = [
    path("overview/", OverviewAPIView.as_view(), name="api-overview"),
    path("tools/", ToolsAPIView.as_view(), name="api-tools"),
    path("sessions/<uuid:pk>/trace/", SessionTraceAPIView.as_view(), name="api-session-trace"),
]


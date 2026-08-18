from rest_framework.response import Response
from rest_framework.views import APIView

from apps.traces.models import Session

from .metrics import overview_metrics, session_timeseries, tool_metrics


class OverviewAPIView(APIView):
    def get(self, request):
        return Response({"metrics": overview_metrics(), "timeseries": session_timeseries()})


class ToolsAPIView(APIView):
    def get(self, request):
        return Response({"results": tool_metrics()})


class SessionTraceAPIView(APIView):
    def get(self, request, pk):
        session = Session.objects.get(pk=pk)
        spans = session.spans.select_related("parent", "turn").order_by("started_at", "sequence_no")
        return Response({
            "session": {"id": str(session.id), "external_id": session.external_session_id, "status": session.status, "duration_ms": session.duration_ms},
            "spans": [{
                "id": str(span.id), "parent_id": str(span.parent_id) if span.parent_id else None,
                "turn_id": str(span.turn_id) if span.turn_id else None, "kind": span.kind,
                "name": span.name, "status": span.status,
                "started_at": span.started_at, "ended_at": span.ended_at,
                "duration_ms": span.duration_ms, "attributes": span.attributes,
            } for span in spans],
        })


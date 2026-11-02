"""/health/ — liveness + readiness in one JSON document.

Returns 200 when the database answers, 503 when it does not, so Docker's
HEALTHCHECK, a load balancer or Kubernetes probe can act on the status code alone.
"""

import time
from datetime import timedelta

from django.conf import settings
from django.db import connections
from django.utils import timezone
from drf_spectacular.utils import OpenApiResponse, extend_schema, inline_serializer
from rest_framework import permissions, serializers
from rest_framework.response import Response
from rest_framework.views import APIView

PROCESS_STARTED_AT = timezone.now()
_START_MONOTONIC = time.monotonic()


def check_database(alias: str = "default") -> dict:
    started = time.perf_counter()
    try:
        with connections[alias].cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception as exc:  # any driver/network failure means "down"
        return {"status": "down", "error": exc.__class__.__name__}
    return {
        "status": "up",
        "vendor": connections[alias].vendor,
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
    }


def _human(seconds: float) -> str:
    return str(timedelta(seconds=int(seconds)))


_health_schema = inline_serializer(
    name="HealthResponse",
    fields={
        "status": serializers.ChoiceField(choices=["ok", "unavailable"]),
        "service": serializers.CharField(),
        "version": serializers.CharField(),
        "timestamp": serializers.DateTimeField(),
        "started_at": serializers.DateTimeField(),
        "uptime_seconds": serializers.FloatField(),
        "uptime": serializers.CharField(),
        "checks": serializers.DictField(),
    },
)


class HealthView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    throttle_classes = []

    @extend_schema(
        tags=["health"],
        summary="Service status, uptime and database connectivity",
        responses={200: _health_schema, 503: OpenApiResponse(_health_schema)},
    )
    def get(self, request):
        uptime = time.monotonic() - _START_MONOTONIC
        database = check_database()
        healthy = database["status"] == "up"
        body = {
            "status": "ok" if healthy else "unavailable",
            "service": settings.SERVICE_NAME,
            "version": settings.SERVICE_VERSION,
            "timestamp": timezone.now().isoformat(),
            "started_at": PROCESS_STARTED_AT.isoformat(),
            "uptime_seconds": round(uptime, 2),
            "uptime": _human(uptime),
            "checks": {"database": database},
        }
        response = Response(body, status=200 if healthy else 503)
        response["Cache-Control"] = "no-store"
        return response

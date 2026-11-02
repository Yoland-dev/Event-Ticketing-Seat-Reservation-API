"""
Standardised JSON error schema.

Every error the service returns — DRF exceptions, Django 404/500s, database
integrity problems and domain rule violations — has exactly this shape:

    {
      "error": {
        "status": 404,
        "code": "not_found",
        "message": "No Booking matches the given query.",
        "details": null,
        "path": "/api/bookings/99/",
        "method": "GET",
        "request_id": "4f0c…",
        "timestamp": "2026-11-01T15:00:00.000000+00:00"
      }
    }

`details` carries field-level validation messages ({"field": ["msg", ...]}) for
400s and is null otherwise.
"""

import logging

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError
from django.db.models import ProtectedError
from django.http import JsonResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.serializers import as_serializer_error
from rest_framework.views import exception_handler as drf_exception_handler

from .models import DomainConflict

logger = logging.getLogger("ticketing.errors")


class Conflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "The request conflicts with the current state of the resource."
    default_code = "conflict"


def build_error(status_code, code, message, details=None, request=None):
    return {
        "error": {
            "status": status_code,
            "code": code,
            "message": message,
            "details": details,
            "path": getattr(request, "path", None),
            "method": getattr(request, "method", None),
            "request_id": getattr(request, "request_id", None),
            "timestamp": timezone.now().isoformat(),
        }
    }


def _translate(exc):
    """Map non-DRF exceptions onto DRF exceptions so one code path formats them."""
    if isinstance(exc, DjangoValidationError):
        return ValidationError(detail=as_serializer_error(exc))
    if isinstance(exc, DomainConflict):
        return Conflict(str(exc), code=exc.code)
    if isinstance(exc, ProtectedError):
        return Conflict(
            "This record is referenced by other records and cannot be deleted.",
            code="protected_resource",
        )
    if isinstance(exc, IntegrityError):
        return Conflict("The request conflicts with existing data.", code="integrity_error")
    return exc


def api_exception_handler(exc, context):
    exc = _translate(exc)
    request = context.get("request")
    response = drf_exception_handler(exc, context)

    if response is None:
        logger.exception("Unhandled error", exc_info=exc)
        return JsonResponse(
            build_error(500, "server_error", "An unexpected error occurred.", request=request),
            status=500,
        )

    data = response.data
    if isinstance(exc, ValidationError):
        code, message, details = "validation_error", "Request validation failed.", data
    elif isinstance(data, dict) and "detail" in data:
        detail = data["detail"]
        code = str(getattr(detail, "code", None) or data.get("code") or exc.default_code)
        message = str(detail)
        extras = {k: v for k, v in data.items() if k not in ("detail", "code")}
        details = extras or None
    else:  # pragma: no cover - defensive; DRF always returns one of the above
        code, message, details = exc.default_code, str(exc), data

    response.data = build_error(response.status_code, code, message, details, request)
    return response


# ---------------------------------------------- Django-level (non-DRF) handlers
def _json(request, status_code, code, message):
    return JsonResponse(
        build_error(status_code, code, message, request=request), status=status_code
    )


def json_400(request, exception=None):
    return _json(request, 400, "bad_request", "Bad request.")


def json_403(request, exception=None):
    return _json(request, 403, "permission_denied", "You do not have permission to do this.")


def json_404(request, exception=None):
    return _json(request, 404, "not_found", "The requested resource was not found.")


def json_500(request):
    return _json(request, 500, "server_error", "An unexpected error occurred.")

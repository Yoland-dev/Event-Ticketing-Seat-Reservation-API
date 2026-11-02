"""Health check, error schema plumbing, request IDs, OpenAPI docs, management commands."""

import json
from io import StringIO
from unittest import mock

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import RequestFactory
from rest_framework.test import APIClient

from ticketing import exceptions
from ticketing.models import Booking, Event, Seat, Venue
from ticketing.tests.utils import assert_error

pytestmark = pytest.mark.django_db


class TestHealth:
    def test_healthy(self, anon_client):
        response = anon_client.get("/health/")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["service"] == "halcyon-ticketing-api"
        assert body["checks"]["database"]["status"] == "up"
        assert body["checks"]["database"]["latency_ms"] >= 0
        assert body["uptime_seconds"] >= 0 and ":" in body["uptime"]
        assert response["Cache-Control"] == "no-store"

    def test_database_down_returns_503(self, anon_client):
        broken = mock.MagicMock()
        broken.cursor.side_effect = ConnectionError("db unreachable")
        with mock.patch("ticketing.health.connections", {"default": broken}):
            response = anon_client.get("/health/")
        assert response.status_code == 503
        assert response.json()["status"] == "unavailable"
        assert response.json()["checks"]["database"] == {
            "status": "down",
            "error": "ConnectionError",
        }


class TestErrorSchema:
    def test_unknown_url_is_json_404(self, anon_client):
        assert_error(anon_client.get("/api/definitely-not-here/"), 404, "not_found")

    def test_method_not_allowed(self, admin_client):
        assert_error(admin_client.put("/api/bookings/", {}), 405, "method_not_allowed")

    def test_malformed_json(self, admin_client):
        response = admin_client.post(
            "/api/venues/", data="{not json", content_type="application/json"
        )
        assert_error(response, 400, "parse_error")

    def test_unhandled_exception_becomes_json_500(self, admin_client):
        with mock.patch("ticketing.views.VenueViewSet.list", side_effect=RuntimeError("boom")):
            response = admin_client.get("/api/venues/")
        error = assert_error(response, 500, "server_error")
        assert "boom" not in error["message"]  # internals never leak

    def test_integrity_error_maps_to_conflict(self):
        from django.db import IntegrityError

        request = RequestFactory().get("/x/")
        response = exceptions.api_exception_handler(IntegrityError("dup"), {"request": request})
        assert response.status_code == 409
        assert response.data["error"]["code"] == "integrity_error"

    def test_django_validation_error_maps_to_400(self):
        from django.core.exceptions import ValidationError

        response = exceptions.api_exception_handler(
            ValidationError({"sku": ["bad"]}), {"request": None}
        )
        assert response.status_code == 400
        assert response.data["error"]["details"] == {"sku": ["bad"]}

    @pytest.mark.parametrize(
        "handler,status_code",
        [
            (exceptions.json_400, 400),
            (exceptions.json_403, 403),
            (exceptions.json_404, 404),
        ],
    )
    def test_django_level_handlers(self, handler, status_code):
        request = RequestFactory().get("/somewhere/")
        response = handler(request, Exception())
        assert response.status_code == status_code
        assert json.loads(response.content)["error"]["path"] == "/somewhere/"

    def test_django_500_handler(self):
        response = exceptions.json_500(RequestFactory().get("/"))
        assert response.status_code == 500
        assert json.loads(response.content)["error"]["code"] == "server_error"


class TestRequestId:
    def test_generated_when_missing(self, anon_client):
        assert len(anon_client.get("/health/")["X-Request-ID"]) == 32

    def test_safe_upstream_id_is_propagated(self):
        client = APIClient(HTTP_X_REQUEST_ID="lb-trace-123")
        assert client.get("/health/")["X-Request-ID"] == "lb-trace-123"

    def test_unsafe_upstream_id_is_replaced(self):
        client = APIClient(HTTP_X_REQUEST_ID="<script>alert(1)</script>")
        assert client.get("/health/")["X-Request-ID"] != "<script>alert(1)</script>"


class TestDocs:
    def test_openapi_schema(self, anon_client):
        response = anon_client.get("/api/schema/", HTTP_ACCEPT="application/vnd.oai.openapi+json")
        assert response.status_code == 200
        paths = response.json()["paths"]
        for path in [
            "/health/",
            "/api/auth/token/",
            "/api/venues/{id}/generate-seats/",
            "/api/bookings/{id}/confirm/",
            "/api/events/{id}/seat-map/",
        ]:
            assert path in paths

    def test_swagger_and_redoc_pages(self, anon_client):
        assert anon_client.get("/api/docs/").status_code == 200
        assert anon_client.get("/api/redoc/").status_code == 200

    def test_root_redirects_to_docs(self, anon_client):
        response = anon_client.get("/")
        assert response.status_code == 302 and response["Location"] == "/api/docs/"


class TestManagementCommands:
    def test_seed_demo_is_idempotent(self):
        call_command("seed_demo", stdout=StringIO())
        call_command("seed_demo", stdout=StringIO())
        assert Venue.objects.count() == 2
        assert Seat.objects.count() == 118
        assert Event.objects.count() == 4
        assert Event.objects.filter(status=Event.Status.ON_SALE).count() == 3

    def test_seed_demo_without_users(self):
        from django.contrib.auth import get_user_model

        call_command("seed_demo", "--no-users", stdout=StringIO())
        assert not get_user_model().objects.exists()

    def test_ensure_superuser(self, monkeypatch):
        from django.contrib.auth import get_user_model

        monkeypatch.setenv("DJANGO_SUPERUSER_USERNAME", "root")
        monkeypatch.setenv("DJANGO_SUPERUSER_PASSWORD", "Root-Passw0rd!")
        out = StringIO()
        call_command("ensure_superuser", stdout=out)
        call_command("ensure_superuser", stdout=out)
        assert "already exists" in out.getvalue()
        assert get_user_model().objects.get(username="root").is_superuser

    def test_ensure_superuser_requires_env(self, monkeypatch):
        monkeypatch.delenv("DJANGO_SUPERUSER_USERNAME", raising=False)
        monkeypatch.delenv("DJANGO_SUPERUSER_PASSWORD", raising=False)
        with pytest.raises(CommandError):
            call_command("ensure_superuser")

    def test_release_expired_holds_command(self, standard_user, book):
        booking = book(standard_user)
        Booking.objects.filter(pk=booking.pk).update(hold_expires_at=booking.created_at)
        out = StringIO()
        call_command("release_expired_holds", stdout=out)
        assert "Expired 1" in out.getvalue()
        booking.refresh_from_db()
        assert booking.status == Booking.Status.EXPIRED

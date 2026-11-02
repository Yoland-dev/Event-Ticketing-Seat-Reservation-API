"""CRUD + role permissions for venues, seats and events."""

from datetime import timedelta

import pytest
from django.utils import timezone

from ticketing.models import Booking, Event, Seat, Venue
from ticketing.tests.conftest import make_event
from ticketing.tests.utils import assert_error

pytestmark = pytest.mark.django_db


def iso(dt):
    return dt.isoformat()


# ------------------------------------------------------------------------- venues
class TestVenues:
    url = "/api/venues/"

    def test_admin_full_crud(self, admin_client):
        created = admin_client.post(
            self.url,
            {"name": "Durban Dome", "city": "Durban", "address": "1 Beach Rd"},
            format="json",
        )
        assert created.status_code == 201, created.content
        detail = f"{self.url}{created.json()['id']}/"
        assert admin_client.get(detail).json()["seat_count"] == 0
        assert (
            admin_client.put(
                detail, {"name": "Durban Dome", "city": "Umhlanga"}, format="json"
            ).json()["city"]
            == "Umhlanga"
        )
        assert (
            admin_client.patch(detail, {"is_active": False}, format="json").json()["is_active"]
            is False
        )
        assert admin_client.delete(detail).status_code == 204
        assert_error(admin_client.get(detail), 404, "not_found")

    def test_standard_user_read_only(self, user_client, venue, seats):
        listing = user_client.get(self.url).json()
        assert listing["count"] == 1 and listing["results"][0]["seat_count"] == 4
        assert_error(
            user_client.post(self.url, {"name": "x", "city": "y"}, format="json"),
            403,
            "permission_denied",
        )
        assert_error(user_client.delete(f"{self.url}{venue.pk}/"), 403)

    def test_duplicate_name_is_field_error(self, admin_client, venue):
        response = admin_client.post(self.url, {"name": venue.name, "city": "x"}, format="json")
        assert "name" in assert_error(response, 400, "validation_error")["details"]

    def test_filter_and_search(self, user_client, venue, other_venue):
        assert user_client.get(self.url, {"city": "Cape Town"}).json()["count"] == 1
        assert user_client.get(self.url, {"search": "highveld"}).json()["count"] == 1

    def test_generate_seats(self, admin_client, venue):
        response = admin_client.post(
            f"{self.url}{venue.pk}/generate-seats/",
            {"section": "Balcony", "rows": ["A", "B"], "seats_per_row": 5, "seat_type": "premium"},
            format="json",
        )
        assert response.status_code == 201, response.content
        assert len(response.json()) == 10
        assert Seat.objects.filter(venue=venue, seat_type="premium").count() == 10

    def test_generate_seats_validation_and_overlap(self, admin_client, venue, seats):
        bad = admin_client.post(
            f"{self.url}{venue.pk}/generate-seats/",
            {"section": "Floor", "rows": ["a", "a"], "seats_per_row": 0},
            format="json",
        )
        assert {"rows", "seats_per_row"} <= assert_error(bad, 400)["details"].keys()
        dup_rows = admin_client.post(
            f"{self.url}{venue.pk}/generate-seats/",
            {"section": "Floor", "rows": ["B", "B"], "seats_per_row": 2},
            format="json",
        )
        assert "rows" in assert_error(dup_rows, 400)["details"]
        overlap = admin_client.post(
            f"{self.url}{venue.pk}/generate-seats/",
            {"section": "Floor", "rows": ["A"], "seats_per_row": 2},
            format="json",
        )
        assert_error(overlap, 409, "integrity_error")
        assert Seat.objects.filter(venue=venue).count() == 4  # all-or-nothing

    def test_generate_seats_admin_only(self, user_client, venue):
        response = user_client.post(
            f"{self.url}{venue.pk}/generate-seats/",
            {"section": "X", "rows": ["A"], "seats_per_row": 1},
            format="json",
        )
        assert_error(response, 403)

    def test_venue_with_events_cannot_be_deleted(self, admin_client, venue, event):
        assert_error(admin_client.delete(f"{self.url}{venue.pk}/"), 409, "protected_resource")
        assert Venue.objects.filter(pk=venue.pk).exists()


# -------------------------------------------------------------------------- seats
class TestSeats:
    url = "/api/seats/"

    def test_admin_crud(self, admin_client, venue):
        created = admin_client.post(
            self.url,
            {"venue": venue.pk, "section": "Box", "row": "A", "number": 1, "seat_type": "vip"},
            format="json",
        )
        assert created.status_code == 201, created.content
        assert created.json()["label"] == "Box A1"
        detail = f"{self.url}{created.json()['id']}/"
        assert admin_client.patch(detail, {"number": 2}, format="json").json()["label"] == "Box A2"
        assert admin_client.delete(detail).status_code == 204

    def test_duplicate_position_and_bad_values(self, admin_client, venue, seats):
        dup = admin_client.post(
            self.url,
            {"venue": venue.pk, "section": "Floor", "row": "A", "number": 1},
            format="json",
        )
        assert_error(dup, 400, "validation_error")
        bad = admin_client.post(
            self.url,
            {"venue": venue.pk, "section": "!", "row": "a1", "number": 0, "seat_type": "throne"},
            format="json",
        )
        assert {"section", "row", "number", "seat_type"} <= assert_error(bad, 400)["details"].keys()

    def test_filter_by_venue_and_type(self, user_client, venue, seats, other_venue):
        Seat.objects.create(venue=other_venue, section="S", row="A", number=1)
        assert user_client.get(self.url, {"venue": venue.pk}).json()["count"] == 4
        assert user_client.get(self.url, {"seat_type": "vip"}).json()["count"] == 1

    def test_standard_user_read_only(self, user_client, seats):
        assert_error(user_client.delete(f"{self.url}{seats[0].pk}/"), 403)

    def test_booked_seat_cannot_be_deleted(self, admin_client, standard_user, book, seats):
        book(standard_user, seats[:1])
        assert_error(admin_client.delete(f"{self.url}{seats[0].pk}/"), 409, "protected_resource")


# ------------------------------------------------------------------------- events
class TestEvents:
    url = "/api/events/"

    def payload(self, venue, days=5, hours=2, **extra):
        starts = timezone.now() + timedelta(days=days)
        return {
            "title": "Summer Gala",
            "venue": venue.pk,
            "starts_at": iso(starts),
            "ends_at": iso(starts + timedelta(hours=hours)),
            "base_price": "250.00",
            **extra,
        }

    def test_admin_create_publish_update_delete(self, admin_client, admin_user, venue, seats):
        created = admin_client.post(self.url, self.payload(venue, status="on_sale"), format="json")
        assert created.status_code == 201, created.content
        body = created.json()
        assert body["status"] == "draft"  # status is read-only; publish is an action
        assert body["organiser"] == "admin"
        assert (body["seats_total"], body["seats_sold"], body["seats_available"]) == (4, 0, 4)

        detail = f"{self.url}{body['id']}/"
        published = admin_client.post(f"{detail}publish/")
        assert published.status_code == 200 and published.json()["status"] == "on_sale"
        assert_error(admin_client.post(f"{detail}publish/"), 409, "invalid_state_transition")

        assert (
            admin_client.patch(detail, {"title": "Gala"}, format="json").json()["title"] == "Gala"
        )
        assert admin_client.delete(detail).status_code == 204

    def test_validation(self, admin_client, venue):
        past = self.payload(venue, days=-1)
        assert (
            "starts_at"
            in assert_error(admin_client.post(self.url, past, format="json"), 400)["details"]
        )
        backwards = self.payload(venue, hours=-1)
        assert (
            "ends_at"
            in assert_error(admin_client.post(self.url, backwards, format="json"), 400)["details"]
        )
        bad = {**self.payload(venue), "base_price": "-1", "max_seats_per_booking": 0}
        details = assert_error(admin_client.post(self.url, bad, format="json"), 400)["details"]
        assert {"base_price", "max_seats_per_booking"} <= details.keys()

    def test_overlapping_event_at_same_venue_rejected(
        self, admin_client, venue, event, other_venue
    ):
        clash = {
            **self.payload(venue),
            "starts_at": iso(event.starts_at + timedelta(hours=1)),
            "ends_at": iso(event.ends_at + timedelta(hours=1)),
        }
        error = assert_error(admin_client.post(self.url, clash, format="json"), 400)
        assert "Overlaps 'Jazz Night'" in error["details"]["starts_at"][0]
        elsewhere = {**clash, "venue": other_venue.pk}
        assert admin_client.post(self.url, elsewhere, format="json").status_code == 201

    def test_inactive_venue_rejected(self, admin_client, venue):
        Venue.objects.filter(pk=venue.pk).update(is_active=False)
        response = admin_client.post(self.url, self.payload(venue), format="json")
        assert "venue" in assert_error(response, 400)["details"]

    def test_venue_locked_once_on_sale(self, admin_client, event, other_venue):
        response = admin_client.patch(
            f"{self.url}{event.pk}/", {"venue": other_venue.pk}, format="json"
        )
        assert "venue" in assert_error(response, 400)["details"]

    def test_standard_user_never_sees_drafts(self, user_client, event, draft_event):
        listing = user_client.get(self.url).json()
        assert [e["title"] for e in listing["results"]] == ["Jazz Night"]
        assert_error(user_client.get(f"{self.url}{draft_event.pk}/"), 404)

    def test_admin_sees_drafts_and_filters(self, admin_client, event, draft_event):
        assert admin_client.get(self.url).json()["count"] == 2
        assert admin_client.get(self.url, {"status": "draft"}).json()["count"] == 1
        assert admin_client.get(self.url, {"search": "jazz"}).json()["count"] == 1
        assert admin_client.get(self.url, {"venue__city": "Johannesburg"}).json()["count"] == 2

    def test_standard_user_cannot_write_or_publish(self, user_client, venue, event, draft_event):
        assert_error(user_client.post(self.url, self.payload(venue), format="json"), 403)
        assert_error(user_client.post(f"{self.url}{event.pk}/cancel/"), 403)
        assert_error(user_client.post(f"{self.url}{event.pk}/publish/"), 403)

    def test_availability_counts_and_ordering(self, user_client, standard_user, book, event, venue):
        book(standard_user)  # 2 of 4 seats
        later = make_event(venue, days=60, title="Later Show")
        rows = user_client.get(self.url, {"ordering": "seats_available"}).json()["results"]
        assert [(r["title"], r["seats_sold"], r["seats_available"]) for r in rows] == [
            ("Jazz Night", 2, 2),
            ("Later Show", 0, 4),
        ]
        assert later.pk == rows[1]["id"]

    def test_cancel_event_cascades_to_bookings(self, admin_client, standard_user, book, event):
        booking = book(standard_user, confirm=True)
        response = admin_client.post(f"{self.url}{event.pk}/cancel/")
        assert response.json()["status"] == "cancelled" and response.json()["seats_sold"] == 0
        booking.refresh_from_db()
        assert booking.status == Booking.Status.CANCELLED
        assert_error(
            admin_client.patch(f"{self.url}{event.pk}/", {"title": "x"}, format="json"),
            409,
            "invalid_state_transition",
        )

    def test_event_with_bookings_cannot_be_deleted(self, admin_client, standard_user, book, event):
        book(standard_user)
        assert_error(admin_client.delete(f"{self.url}{event.pk}/"), 409, "invalid_state_transition")
        assert Event.objects.filter(pk=event.pk).exists()


class TestSeatMap:
    def test_prices_and_availability(self, user_client, other_user, book, event, seats):
        book(other_user, seats[:1])
        rows = user_client.get(f"/api/events/{event.pk}/seat-map/").json()
        assert len(rows) == 4
        by_label = {r["label"]: r for r in rows}
        assert by_label["Floor A1"]["available"] is False
        assert by_label["Floor A2"]["available"] is True
        assert by_label["Floor A4"]["price"] == "500.00"

    def test_available_filter(self, user_client, other_user, book, event, seats):
        book(other_user, seats[:3])
        rows = user_client.get(f"/api/events/{event.pk}/seat-map/", {"available": "true"}).json()
        assert [r["label"] for r in rows] == ["Floor A4"]

    def test_expired_holds_show_as_available(self, user_client, other_user, book, event, seats):
        stale = book(other_user, seats[:1])
        Booking.objects.filter(pk=stale.pk).update(hold_expires_at=timezone.now())
        rows = user_client.get(f"/api/events/{event.pk}/seat-map/").json()
        assert all(r["available"] for r in rows)

    def test_cancelled_event_has_nothing_available(self, user_client, event):
        event.cancel()
        rows = user_client.get(f"/api/events/{event.pk}/seat-map/").json()
        assert not any(r["available"] for r in rows)

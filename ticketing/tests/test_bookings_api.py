from datetime import timedelta

import pytest
from django.utils import timezone

from ticketing.models import Booking, Event, Seat, Ticket
from ticketing.tests.conftest import make_event
from ticketing.tests.utils import assert_error

pytestmark = pytest.mark.django_db

URL = "/api/bookings/"


def payload(event, seats, **extra):
    return {"event": event.pk, "seats": [s.pk for s in seats], **extra}


class TestCreate:
    def test_hold_seats(self, user_client, event, seats):
        response = user_client.post(URL, payload(event, [seats[0], seats[3]]), format="json")
        assert response.status_code == 201, response.content
        body = response.json()
        assert body["status"] == "pending"
        assert body["reference"].startswith("BK-")
        assert body["user"] == "alice"
        assert body["contact_name"] == "Alice Dlamini"  # defaults from the account
        assert body["contact_email"] == "alice@test.example"
        assert body["total_price"] == "700.00"
        assert [t["seat_label"] for t in body["tickets"]] == ["Floor A1", "Floor A4"]
        assert body["hold_expires_at"] is not None

    def test_explicit_contact_details(self, user_client, event, seats):
        response = user_client.post(
            URL,
            payload(event, seats[:1], contact_name="Sipho M", contact_email="SIPHO@Example.com"),
            format="json",
        )
        assert response.json()["contact_email"] == "sipho@example.com"

    def test_contact_email_required_when_account_has_none(self, event, seats, db):
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient

        user = get_user_model().objects.create_user("noemail", password="x")
        client = APIClient()
        client.force_authenticate(user)
        response = client.post(URL, payload(event, seats[:1]), format="json")
        assert "contact_email" in assert_error(response, 400)["details"]

    def test_double_booking_is_conflict(self, user_client, other_client, event, seats):
        assert user_client.post(URL, payload(event, seats[:2]), format="json").status_code == 201
        response = other_client.post(URL, payload(event, seats[1:3]), format="json")
        error = assert_error(response, 409, "seat_unavailable")
        assert "Floor A2" in error["message"]
        assert Booking.objects.count() == 1  # the failed attempt left nothing behind

    def test_validation_rules(self, user_client, event, seats, other_venue):
        foreign = Seat.objects.create(venue=other_venue, section="S", row="A", number=1)
        cases = [
            (payload(event, []), "seats"),
            (payload(event, [seats[0], seats[0]]), "seats"),
            (payload(event, [foreign]), "seats"),
            ({"event": 9999, "seats": [seats[0].pk]}, "event"),
            ({"event": event.pk, "seats": [9999]}, "seats"),
        ]
        for body, field in cases:
            response = user_client.post(URL, body, format="json")
            assert field in assert_error(response, 400, "validation_error")["details"], body

    def test_seat_limit_per_booking(self, user_client, event, seats, settings):
        Event.objects.filter(pk=event.pk).update(max_seats_per_booking=2)
        response = user_client.post(URL, payload(event, seats[:3]), format="json")
        assert "At most 2" in assert_error(response, 400)["details"]["seats"][0]
        settings.MAX_SEATS_PER_BOOKING = 1
        response = user_client.post(URL, payload(event, seats[:2]), format="json")
        assert "At most 1" in assert_error(response, 400)["details"]["seats"][0]

    def test_cannot_book_draft_or_cancelled_event(self, user_client, draft_event, event, seats):
        assert_error(
            user_client.post(URL, payload(draft_event, seats[:1]), format="json"),
            409,
            "invalid_state_transition",
        )
        event.cancel()
        assert_error(user_client.post(URL, payload(event, seats[:1]), format="json"), 409)

    def test_anonymous_cannot_book(self, anon_client, event, seats):
        assert_error(anon_client.post(URL, payload(event, seats[:1]), format="json"), 401)


class TestVisibility:
    def test_users_see_only_their_own(self, user_client, standard_user, other_user, book, seats):
        mine = book(standard_user, seats[:1])
        theirs = book(other_user, seats[1:2])
        listing = user_client.get(URL).json()
        assert [b["reference"] for b in listing["results"]] == [mine.reference]
        assert_error(user_client.get(f"{URL}{theirs.pk}/"), 404, "not_found")

    def test_admin_sees_all_and_filters(self, admin_client, standard_user, other_user, book, seats):
        book(standard_user, seats[:1], confirm=True)
        book(other_user, seats[1:2])
        assert admin_client.get(URL).json()["count"] == 2
        assert admin_client.get(URL, {"status": "confirmed"}).json()["count"] == 1
        assert admin_client.get(URL, {"search": "bob"}).json()["count"] == 1


class TestUpdateAndDelete:
    def test_update_contact_details_only(self, user_client, standard_user, book, event, seats):
        booking = book(standard_user)
        response = user_client.patch(
            f"{URL}{booking.pk}/",
            {"contact_name": "A. Dlamini", "status": "confirmed", "total_price": "0.00"},
            format="json",
        )
        assert response.status_code == 200, response.content
        body = response.json()
        assert body["contact_name"] == "A. Dlamini"
        assert body["status"] == "pending" and body["total_price"] == "400.00"

    def test_cannot_edit_cancelled_booking(self, user_client, standard_user, book):
        booking = book(standard_user)
        booking.cancel()
        response = user_client.patch(f"{URL}{booking.pk}/", {"contact_name": "x"}, format="json")
        assert_error(response, 409, "invalid_state_transition")

    def test_other_user_cannot_edit(self, other_client, standard_user, book):
        booking = book(standard_user)
        assert_error(
            other_client.patch(f"{URL}{booking.pk}/", {"contact_name": "x"}, format="json"), 404
        )

    def test_delete_pending_hold_releases_seats(self, user_client, standard_user, book, seats):
        booking = book(standard_user)
        assert user_client.delete(f"{URL}{booking.pk}/").status_code == 204
        assert not Ticket.objects.exists()

    def test_cannot_delete_confirmed(self, user_client, standard_user, book):
        booking = book(standard_user, confirm=True)
        assert_error(user_client.delete(f"{URL}{booking.pk}/"), 409, "invalid_state_transition")


class TestActions:
    def test_full_flow_hold_confirm_cancel_rebook(self, user_client, other_client, event, seats):
        created = user_client.post(URL, payload(event, seats[:2]), format="json").json()
        confirmed = user_client.post(f"{URL}{created['id']}/confirm/")
        assert confirmed.status_code == 200
        assert confirmed.json()["status"] == "confirmed"
        assert confirmed.json()["hold_expires_at"] is None

        seat_map = user_client.get(f"/api/events/{event.pk}/seat-map/").json()
        assert sum(not r["available"] for r in seat_map) == 2

        cancelled = user_client.post(f"{URL}{created['id']}/cancel/")
        assert cancelled.json()["status"] == "cancelled"
        assert all(t["status"] == "released" for t in cancelled.json()["tickets"])

        rebook = other_client.post(URL, payload(event, seats[:2]), format="json")
        assert rebook.status_code == 201

    def test_confirm_after_hold_expired(self, user_client, standard_user, book):
        booking = book(standard_user)
        Booking.objects.filter(pk=booking.pk).update(
            hold_expires_at=timezone.now() - timedelta(seconds=1)
        )
        assert_error(user_client.post(f"{URL}{booking.pk}/confirm/"), 409, "hold_expired")
        assert user_client.get(f"{URL}{booking.pk}/").json()["status"] == "expired"

    def test_confirm_twice_is_conflict(self, user_client, standard_user, book):
        booking = book(standard_user, confirm=True)
        assert_error(
            user_client.post(f"{URL}{booking.pk}/confirm/"), 409, "invalid_state_transition"
        )

    def test_cancel_after_event_started(self, user_client, standard_user, book, event):
        booking = book(standard_user, confirm=True)
        Event.objects.filter(pk=event.pk).update(starts_at=timezone.now() - timedelta(hours=1))
        assert_error(user_client.post(f"{URL}{booking.pk}/cancel/"), 409)

    def test_admin_can_act_on_any_booking(self, admin_client, standard_user, book):
        booking = book(standard_user)
        assert admin_client.post(f"{URL}{booking.pk}/cancel/").json()["status"] == "cancelled"

    def test_cannot_act_on_someone_elses_booking(self, other_client, standard_user, book):
        booking = book(standard_user)
        assert_error(other_client.post(f"{URL}{booking.pk}/confirm/"), 404)

    def test_booking_same_seat_for_different_events(
        self, user_client, other_client, event, venue, seats
    ):
        second_event = make_event(venue, days=20, title="Encore")
        assert user_client.post(URL, payload(event, seats[:1]), format="json").status_code == 201
        assert (
            other_client.post(URL, payload(second_event, seats[:1]), format="json").status_code
            == 201
        )

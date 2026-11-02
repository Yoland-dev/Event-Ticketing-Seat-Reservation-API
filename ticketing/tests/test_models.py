from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from ticketing.models import (
    Booking,
    Event,
    HoldExpiredError,
    Seat,
    SeatUnavailableError,
    StateTransitionError,
    Ticket,
)
from ticketing.tests.conftest import make_event

pytestmark = pytest.mark.django_db


class TestVenueAndSeat:
    def test_str_and_label(self, venue, seats):
        assert str(venue) == "Highveld Arena (Johannesburg)"
        assert str(seats[0]) == seats[0].label == "Floor A1"

    @pytest.mark.parametrize("field,value", [("row", "a"), ("row", "ABCD"), ("number", 0)])
    def test_seat_validation(self, seats, field, value):
        seat = seats[0]
        setattr(seat, field, value)
        with pytest.raises(ValidationError) as exc:
            seat.full_clean()
        assert field in exc.value.message_dict

    def test_seat_position_unique(self, venue, seats):
        with pytest.raises(IntegrityError), transaction.atomic():
            Seat.objects.create(venue=venue, section="Floor", row="A", number=1)

    def test_same_position_allowed_in_another_venue(self, other_venue, seats):
        Seat.objects.create(venue=other_venue, section="Floor", row="A", number=1)

    def test_price_multipliers(self, event, seats):
        assert seats[0].price_for(event) == Decimal("200.00")
        assert seats[3].price_for(event) == Decimal("500.00")  # VIP × 2.5


class TestEvent:
    def test_str(self, event):
        assert str(event) == "Jazz Night @ Highveld Arena"

    def test_end_must_follow_start_in_database(self, event):
        with pytest.raises(IntegrityError), transaction.atomic():
            Event.objects.filter(pk=event.pk).update(ends_at=event.starts_at)

    def test_publish(self, draft_event):
        draft_event.publish()
        assert draft_event.status == Event.Status.ON_SALE
        with pytest.raises(StateTransitionError, match="Only draft"):
            draft_event.publish()

    def test_publish_requires_seats(self, other_venue):
        event = make_event(other_venue, status=Event.Status.DRAFT)
        with pytest.raises(StateTransitionError, match="no seats"):
            event.publish()

    def test_publish_rejects_past_event(self, draft_event):
        Event.objects.filter(pk=draft_event.pk).update(
            starts_at=timezone.now() - timedelta(hours=1)
        )
        draft_event.refresh_from_db()
        with pytest.raises(StateTransitionError, match="started"):
            draft_event.publish()

    def test_cancel_releases_every_live_booking(
        self, event, book, standard_user, other_user, seats
    ):
        pending = book(standard_user, seats[:1])
        confirmed = book(other_user, seats[1:2], confirm=True)
        event.cancel()
        assert event.status == Event.Status.CANCELLED
        for booking in (pending, confirmed):
            booking.refresh_from_db()
            assert booking.status == Booking.Status.CANCELLED
        assert not Ticket.objects.filter(status=Ticket.Status.ACTIVE).exists()
        with pytest.raises(StateTransitionError):
            event.cancel()


class TestBookingLifecycle:
    def test_reserve_creates_priced_tickets_and_hold(self, event, seats, standard_user):
        booking = Booking.reserve(
            user=standard_user,
            event=event,
            seats=[seats[0], seats[3]],
            contact_name="Alice",
            contact_email="alice@test.example",
        )
        assert booking.status == Booking.Status.PENDING
        assert booking.reference.startswith("BK-") and str(booking) == booking.reference
        assert booking.total_price == Decimal("700.00")
        assert set(booking.seats.all()) == {seats[0], seats[3]}  # M2M through Ticket
        assert booking.hold_expires_at > timezone.now()
        assert str(booking.tickets.first()).startswith(booking.reference)

    def test_seat_cannot_be_reserved_twice(self, book, standard_user, other_user, seats):
        book(standard_user, seats[:2])
        with pytest.raises(SeatUnavailableError, match="Floor A2"):
            book(other_user, seats[1:3])

    def test_database_blocks_double_booking_even_without_app_check(
        self, book, standard_user, other_user, event, seats
    ):
        """The partial unique index is the real guarantee, not the Python check."""
        book(standard_user, seats[:1])
        intruder = Booking.objects.create(
            user=other_user, event=event, contact_name="b", contact_email="b@test.example"
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            Ticket.objects.create(booking=intruder, event=event, seat=seats[0], price=1)

    def test_race_lost_at_insert_becomes_seat_unavailable(
        self, book, standard_user, other_user, seats, monkeypatch
    ):
        """Simulate a competing request that slipped in after our availability check."""
        book(standard_user, seats[:1])
        original_filter = Ticket.objects.filter

        def blind_filter(*args, **kwargs):
            if "seat__in" in kwargs:
                return Ticket.objects.none()
            return original_filter(*args, **kwargs)

        monkeypatch.setattr(Ticket.objects, "filter", blind_filter)
        with pytest.raises(SeatUnavailableError, match="just taken"):
            book(other_user, seats[:1])

    def test_released_seats_can_be_rebooked(self, book, standard_user, other_user, seats):
        first = book(standard_user, seats[:1])
        first.cancel()
        second = book(other_user, seats[:1])
        assert second.status == Booking.Status.PENDING
        assert Ticket.objects.filter(seat=seats[0]).count() == 2  # history kept

    def test_reserve_requires_on_sale_future_event(self, draft_event, event, standard_user, seats):
        with pytest.raises(StateTransitionError, match="not on sale"):
            Booking.reserve(
                user=standard_user,
                event=draft_event,
                seats=seats[:1],
                contact_name="a",
                contact_email="a@test.example",
            )
        Event.objects.filter(pk=event.pk).update(starts_at=timezone.now() - timedelta(minutes=1))
        with pytest.raises(StateTransitionError, match="already started"):
            Booking.reserve(
                user=standard_user,
                event=event,
                seats=seats[:1],
                contact_name="a",
                contact_email="a@test.example",
            )

    def test_confirm(self, book, standard_user):
        booking = book(standard_user)
        booking.confirm()
        assert booking.status == Booking.Status.CONFIRMED
        assert booking.confirmed_at and booking.hold_expires_at is None
        with pytest.raises(StateTransitionError):
            booking.confirm()

    def test_confirm_after_hold_expired(self, book, standard_user):
        booking = book(standard_user)
        Booking.objects.filter(pk=booking.pk).update(
            hold_expires_at=timezone.now() - timedelta(seconds=1)
        )
        booking.refresh_from_db()
        assert booking.hold_has_expired
        with pytest.raises(HoldExpiredError):
            booking.confirm()
        booking.refresh_from_db()
        # The expiry is committed even though confirm() raised.
        assert booking.status == Booking.Status.EXPIRED
        assert not booking.tickets.filter(status=Ticket.Status.ACTIVE).exists()

    def test_expired_holds_are_released_when_someone_else_books(
        self, book, standard_user, other_user, seats
    ):
        stale = book(standard_user, seats[:1])
        Booking.objects.filter(pk=stale.pk).update(hold_expires_at=timezone.now())
        fresh = book(other_user, seats[:1])
        stale.refresh_from_db()
        assert stale.status == Booking.Status.EXPIRED
        assert fresh.status == Booking.Status.PENDING

    def test_cancel_rules(self, book, standard_user, event):
        booking = book(standard_user, confirm=True)
        booking.cancel()
        assert booking.status == Booking.Status.CANCELLED and booking.cancelled_at
        with pytest.raises(StateTransitionError, match="already"):
            booking.cancel()

    def test_cannot_cancel_after_event_starts(self, book, standard_user, event):
        booking = book(standard_user, confirm=True)
        Event.objects.filter(pk=event.pk).update(starts_at=timezone.now() - timedelta(minutes=5))
        with pytest.raises(StateTransitionError, match="after the event starts"):
            booking.cancel()


def test_declared_indexes_exist_in_database(db):
    """Guard the index design documented in the README against accidental removal."""
    found = set()
    with connection.cursor() as cursor:
        for table in ("ticketing_event", "ticketing_booking", "ticketing_ticket", "ticketing_seat"):
            found |= set(connection.introspection.get_constraints(cursor, table))
    assert {
        "event_status_start_idx",
        "event_venue_start_idx",
        "booking_user_created_idx",
        "booking_event_status_idx",
        "booking_pending_expiry_idx",
        "one_active_ticket_per_seat_per_event",
        "unique_seat_per_booking",
        "unique_seat_position",
    } <= found

from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APIClient

from ticketing.models import Booking, Event, Seat, Venue

User = get_user_model()
PASSWORD = "Str0ng-Test-Passw0rd!"


@pytest.fixture(autouse=True)
def _fast_hashing_and_clean_throttles(settings):
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
    cache.clear()  # throttle counters live in the cache


@pytest.fixture
def admin_user(db):
    return User.objects.create_user("admin", "admin@test.example", PASSWORD, is_staff=True)


@pytest.fixture
def standard_user(db):
    return User.objects.create_user(
        "alice", "alice@test.example", PASSWORD, first_name="Alice", last_name="Dlamini"
    )


@pytest.fixture
def other_user(db):
    return User.objects.create_user("bob", "bob@test.example", PASSWORD)


def _client(user=None):
    client = APIClient()
    if user is not None:
        client.force_authenticate(user)
    return client


@pytest.fixture
def anon_client():
    return _client()


@pytest.fixture
def admin_client(admin_user):
    return _client(admin_user)


@pytest.fixture
def user_client(standard_user):
    return _client(standard_user)


@pytest.fixture
def other_client(other_user):
    return _client(other_user)


@pytest.fixture
def venue(db):
    return Venue.objects.create(name="Highveld Arena", city="Johannesburg")


@pytest.fixture
def other_venue(db):
    return Venue.objects.create(name="Harbour Theatre", city="Cape Town")


@pytest.fixture
def seats(venue):
    """Row A: A1-A3 standard, A4 vip."""
    created = [
        Seat.objects.create(venue=venue, section="Floor", row="A", number=n) for n in (1, 2, 3)
    ]
    created.append(
        Seat.objects.create(
            venue=venue, section="Floor", row="A", number=4, seat_type=Seat.SeatType.VIP
        )
    )
    return created


def make_event(venue, *, days=10, status=Event.Status.ON_SALE, title="Jazz Night", price="200.00"):
    starts = timezone.now() + timedelta(days=days)
    return Event.objects.create(
        title=title,
        venue=venue,
        starts_at=starts,
        ends_at=starts + timedelta(hours=3),
        base_price=Decimal(price),
        status=status,
    )


@pytest.fixture
def event(venue, seats):
    return make_event(venue)


@pytest.fixture
def draft_event(venue, seats):
    return make_event(venue, days=40, status=Event.Status.DRAFT, title="Draft Show")


@pytest.fixture
def book(event, seats):
    """Reserve seats directly through the domain layer."""

    def _book(user, seat_list=None, confirm=False, on_event=None):
        booking = Booking.reserve(
            user=user,
            event=on_event or event,
            seats=seat_list or seats[:2],
            contact_name=user.username,
            contact_email=user.email or "x@test.example",
        )
        if confirm:
            booking.confirm()
        return booking

    return _book

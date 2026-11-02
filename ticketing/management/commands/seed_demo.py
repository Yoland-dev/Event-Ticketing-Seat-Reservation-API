from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from ticketing.models import Event, Seat, Venue

# (name, city, address, [(section, rows, seats_per_row, seat_type), ...])
VENUES = [
    (
        "Highveld Arena",
        "Johannesburg",
        "12 Stadium Road, Johannesburg",
        [
            ("Floor", "ABCD", 12, "standard"),
            ("Floor", "E", 12, "accessible"),
            ("Balcony", "AB", 10, "premium"),
            ("Box", "A", 6, "vip"),
        ],
    ),
    (
        "Harbour Theatre",
        "Cape Town",
        "3 Quay Street, Cape Town",
        [("Stalls", "ABC", 8, "standard"), ("Circle", "A", 8, "premium")],
    ),
]

# (title, venue index, days from now, hours long, base price, status)
EVENTS = [
    ("Jazz on the Highveld", 0, 14, 3, "350.00", Event.Status.ON_SALE),
    ("Tech Leaders Summit 2026", 0, 30, 8, "1200.00", Event.Status.ON_SALE),
    ("A Midsummer Night's Dream", 1, 21, 3, "280.00", Event.Status.ON_SALE),
    ("Comedy Night (draft)", 1, 45, 2, "180.00", Event.Status.DRAFT),
]


class Command(BaseCommand):
    help = "Load idempotent demo data (venues, seats, events and demo users)."

    def add_arguments(self, parser):
        parser.add_argument("--no-users", action="store_true", help="Skip creating the demo users.")

    @transaction.atomic
    def handle(self, *args, **options):
        User = get_user_model()
        admin = None
        if not options["no_users"]:
            admin = self._user(User, "admin", "admin@halcyon.example", "HalcyonAdmin!2026", True)
            self._user(User, "fan", "fan@halcyon.example", "HalcyonFan!2026", False)

        venues = []
        for name, city, address, blocks in VENUES:
            venue, created = Venue.objects.get_or_create(
                name=name, defaults={"city": city, "address": address}
            )
            if created:
                Seat.objects.bulk_create(
                    Seat(venue=venue, section=section, row=row, number=n, seat_type=seat_type)
                    for section, rows, per_row, seat_type in blocks
                    for row in rows
                    for n in range(1, per_row + 1)
                )
            venues.append(venue)

        # Round to a fixed time of day so re-running the command stays idempotent.
        base = timezone.now().replace(hour=18, minute=0, second=0, microsecond=0)
        for title, venue_idx, days, hours, price, status in EVENTS:
            starts = base + timedelta(days=days)
            Event.objects.get_or_create(
                title=title,
                defaults={
                    "venue": venues[venue_idx],
                    "starts_at": starts,
                    "ends_at": starts + timedelta(hours=hours),
                    "base_price": Decimal(price),
                    "status": status,
                    "organiser": admin,
                },
            )
        self.stdout.write(self.style.SUCCESS("Demo data ready."))

    def _user(self, User, username, email, password, staff):
        user = User.objects.filter(username=username).first()
        if user:
            return user
        if staff:
            return User.objects.create_superuser(username=username, email=email, password=password)
        return User.objects.create_user(username=username, email=email, password=password)

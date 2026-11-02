from django.core.management.base import BaseCommand

from ticketing.models import Booking


class Command(BaseCommand):
    help = (
        "Expire pending bookings whose seat hold has run out, releasing their seats. "
        "Holds are also released lazily whenever someone books or views a seat map; "
        "run this on a schedule (e.g. every minute) to keep availability counts exact."
    )

    def handle(self, *args, **options):
        released = Booking.expire_stale()
        self.stdout.write(f"Expired {released} stale booking(s).")

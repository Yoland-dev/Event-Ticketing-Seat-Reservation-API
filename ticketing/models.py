"""
Domain model for the Event Ticketing & Seat Reservation API.

    Venue ──1:N──> Seat
      │              ▲
     1:N             │ M2M (through Ticket)
      ▼              │
    Event ──1:N──> Booking ──1:N──> Ticket ──FK──> Seat
      │              │                │
      FK             FK               FK ──> Event (denormalised for the
      ▼              ▼                          double-booking constraint)
    User          User
  (organiser)   (customer)

The guarantee that matters most — a seat is never sold twice for the same event —
is enforced by the database, not by application code: a *partial* unique index on
Ticket(event, seat) WHERE status = 'active'. Application checks give friendly
errors; the index makes races impossible.
"""

import secrets
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import IntegrityError, models, transaction
from django.db.models import Q
from django.utils import timezone

row_validator = RegexValidator(r"^[A-Z]{1,3}$", "Row must be 1-3 uppercase letters, e.g. A or AA.")
section_validator = RegexValidator(
    r"^[A-Za-z0-9][A-Za-z0-9 -]{0,29}$", "Section may contain letters, digits, spaces and hyphens."
)


# ------------------------------------------------------------------ domain errors
class DomainConflict(Exception):
    """A business rule refused the request. Mapped to HTTP 409 by the error handler."""

    code = "conflict"


class StateTransitionError(DomainConflict):
    code = "invalid_state_transition"


class SeatUnavailableError(DomainConflict):
    code = "seat_unavailable"


class HoldExpiredError(DomainConflict):
    code = "hold_expired"


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


# ------------------------------------------------------------------------ venues
class Venue(TimeStampedModel):
    name = models.CharField(max_length=150, unique=True)
    city = models.CharField(max_length=80)
    address = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        indexes = [models.Index(fields=["city"], name="venue_city_idx")]

    def __str__(self):
        return f"{self.name} ({self.city})"


class Seat(models.Model):
    class SeatType(models.TextChoices):
        STANDARD = "standard", "Standard"
        PREMIUM = "premium", "Premium"
        VIP = "vip", "VIP"
        ACCESSIBLE = "accessible", "Accessible"

    # Price multiplier applied to the event's base price.
    PRICE_MULTIPLIERS = {
        SeatType.STANDARD: Decimal("1.00"),
        SeatType.PREMIUM: Decimal("1.50"),
        SeatType.VIP: Decimal("2.50"),
        SeatType.ACCESSIBLE: Decimal("1.00"),
    }

    # db_index=False: the unique (venue, section, row, number) index leads with venue.
    venue = models.ForeignKey(Venue, on_delete=models.CASCADE, related_name="seats", db_index=False)
    section = models.CharField(max_length=30, validators=[section_validator])
    row = models.CharField(max_length=3, validators=[row_validator])
    number = models.PositiveSmallIntegerField(validators=[MinValueValidator(1)])
    seat_type = models.CharField(max_length=10, choices=SeatType.choices, default=SeatType.STANDARD)

    class Meta:
        ordering = ["venue_id", "section", "row", "number"]
        constraints = [
            models.UniqueConstraint(
                fields=["venue", "section", "row", "number"], name="unique_seat_position"
            ),
            models.CheckConstraint(condition=Q(number__gte=1), name="seat_number_positive"),
        ]

    def __str__(self):
        return f"{self.section} {self.row}{self.number}"

    @property
    def label(self) -> str:
        return str(self)

    def price_for(self, event: "Event") -> Decimal:
        return (event.base_price * self.PRICE_MULTIPLIERS[self.seat_type]).quantize(Decimal("0.01"))


# ------------------------------------------------------------------------ events
class Event(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        ON_SALE = "on_sale", "On sale"
        CANCELLED = "cancelled", "Cancelled"

    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    # db_index=False: the (venue, starts_at) index below leads with venue.
    venue = models.ForeignKey(
        Venue, on_delete=models.PROTECT, related_name="events", db_index=False
    )
    organiser = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="organised_events",
    )
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    base_price = models.DecimalField(
        max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal("0.00"))]
    )
    max_seats_per_booking = models.PositiveSmallIntegerField(
        default=10, validators=[MinValueValidator(1), MaxValueValidator(50)]
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)

    class Meta:
        ordering = ["starts_at"]
        indexes = [
            # Public listing: "events on sale, soonest first".
            models.Index(fields=["status", "starts_at"], name="event_status_start_idx"),
            # Venue calendar and the no-overlap check when scheduling an event.
            models.Index(fields=["venue", "starts_at"], name="event_venue_start_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(ends_at__gt=models.F("starts_at")), name="event_ends_after_start"
            ),
            models.CheckConstraint(condition=Q(base_price__gte=0), name="event_price_non_negative"),
        ]

    def __str__(self):
        return f"{self.title} @ {self.venue.name}"

    @property
    def has_started(self) -> bool:
        return self.starts_at <= timezone.now()

    def publish(self):
        if self.status != self.Status.DRAFT:
            raise StateTransitionError(
                f"Only draft events can be published (event is '{self.status}')."
            )
        if self.has_started:
            raise StateTransitionError("Cannot put an event on sale after it has started.")
        if not self.venue.seats.exists():
            raise StateTransitionError("The venue has no seats configured.")
        self.status = self.Status.ON_SALE
        self.save(update_fields=["status", "updated_at"])

    @transaction.atomic
    def cancel(self):
        """Cancel the event and every live booking for it, releasing all seats."""
        if self.status == self.Status.CANCELLED:
            raise StateTransitionError("Event is already cancelled.")
        live = self.bookings.filter(status__in=[Booking.Status.PENDING, Booking.Status.CONFIRMED])
        for booking in live:
            booking._close(Booking.Status.CANCELLED)
        self.status = self.Status.CANCELLED
        self.save(update_fields=["status", "updated_at"])

    def release_expired_holds(self) -> int:
        """Expire this event's pending bookings whose hold has run out."""
        return Booking.expire_stale(event=self)


# ---------------------------------------------------------------------- bookings
def generate_booking_reference() -> str:
    return f"BK-{secrets.token_hex(4).upper()}"


class BookingQuerySet(models.QuerySet):
    def stale(self, now=None):
        return self.filter(
            status=Booking.Status.PENDING, hold_expires_at__lte=now or timezone.now()
        )


class Booking(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending payment"
        CONFIRMED = "confirmed", "Confirmed"
        CANCELLED = "cancelled", "Cancelled"
        EXPIRED = "expired", "Expired"

    reference = models.CharField(
        max_length=12, unique=True, editable=False, default=generate_booking_reference
    )
    # db_index=False on both: composite indexes below lead with each of them.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="bookings", db_index=False
    )
    event = models.ForeignKey(
        Event, on_delete=models.PROTECT, related_name="bookings", db_index=False
    )
    seats = models.ManyToManyField(Seat, through="Ticket", related_name="bookings")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    contact_name = models.CharField(max_length=120)
    contact_email = models.EmailField()
    total_price = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    hold_expires_at = models.DateTimeField(null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    objects = BookingQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            # "My bookings, newest first" — every standard user's list query.
            models.Index(fields=["user", "-created_at"], name="booking_user_created_idx"),
            # Admin: bookings for an event, by status.
            models.Index(fields=["event", "status"], name="booking_event_status_idx"),
            # Hold sweeper: only pending rows are ever scanned, so index only those.
            models.Index(
                fields=["hold_expires_at"],
                condition=Q(status="pending"),
                name="booking_pending_expiry_idx",
            ),
        ]

    def __str__(self):
        return self.reference

    # ------------------------------------------------------------- lifecycle
    @property
    def hold_has_expired(self) -> bool:
        return (
            self.status == self.Status.PENDING
            and self.hold_expires_at is not None
            and self.hold_expires_at <= timezone.now()
        )

    def _close(self, status):
        now = timezone.now()
        self.status = status
        if status == self.Status.CANCELLED:
            self.cancelled_at = now
        self.save(update_fields=["status", "cancelled_at", "updated_at"])
        self.tickets.filter(status=Ticket.Status.ACTIVE).update(status=Ticket.Status.RELEASED)

    @classmethod
    @transaction.atomic
    def expire_stale(cls, event=None) -> int:
        stale = cls.objects.stale().select_for_update(skip_locked=True)
        if event is not None:
            stale = stale.filter(event=event)
        count = 0
        for booking in stale:
            booking._close(cls.Status.EXPIRED)
            count += 1
        return count

    @classmethod
    @transaction.atomic
    def reserve(cls, *, user, event, seats, contact_name, contact_email):
        """Hold `seats` for `event` and return a pending booking.

        Raises SeatUnavailableError if any seat is already taken. The partial unique
        index on Ticket is the final arbiter: if two requests race for the same seat,
        exactly one INSERT succeeds and the other surfaces as SeatUnavailableError.
        """
        event = Event.objects.select_for_update().get(pk=event.pk)
        if event.status != Event.Status.ON_SALE:
            raise StateTransitionError("Tickets for this event are not on sale.")
        if event.has_started:
            raise StateTransitionError("This event has already started.")

        cls.expire_stale(event=event)

        taken = set(
            Ticket.objects.filter(
                event=event, seat__in=seats, status=Ticket.Status.ACTIVE
            ).values_list("seat_id", flat=True)
        )
        if taken:
            labels = ", ".join(sorted(str(s) for s in seats if s.pk in taken))
            raise SeatUnavailableError(f"Already booked or held: {labels}.")

        hold = timedelta(minutes=settings.BOOKING_HOLD_MINUTES)
        booking = cls.objects.create(
            user=user,
            event=event,
            contact_name=contact_name,
            contact_email=contact_email,
            hold_expires_at=timezone.now() + hold,
        )
        tickets = [
            Ticket(booking=booking, event=event, seat=seat, price=seat.price_for(event))
            for seat in seats
        ]
        try:
            with transaction.atomic():
                Ticket.objects.bulk_create(tickets)
        except IntegrityError as exc:
            raise SeatUnavailableError(
                "One or more seats were just taken. Please try again."
            ) from exc
        booking.total_price = sum((t.price for t in tickets), Decimal("0.00"))
        booking.save(update_fields=["total_price", "updated_at"])
        return booking

    def confirm(self):
        """Pending → confirmed (payment succeeded). Fails if the hold has lapsed.

        An expired hold is committed as EXPIRED *before* the error is raised, so the
        seats are released even though the request fails.
        """
        with transaction.atomic():
            locked = Booking.objects.select_for_update().get(pk=self.pk)
            if locked.status != self.Status.PENDING:
                raise StateTransitionError(
                    f"Only pending bookings can be confirmed (booking is '{locked.status}')."
                )
            if not locked.hold_has_expired:
                locked.status = self.Status.CONFIRMED
                locked.confirmed_at = timezone.now()
                locked.hold_expires_at = None
                locked.save(
                    update_fields=["status", "confirmed_at", "hold_expires_at", "updated_at"]
                )
                self.refresh_from_db()
                return
            locked._close(self.Status.EXPIRED)
        self.refresh_from_db()
        raise HoldExpiredError("The seat hold expired before the booking was confirmed.")

    @transaction.atomic
    def cancel(self):
        """Pending or confirmed → cancelled, releasing seats. Not after the event starts."""
        locked = Booking.objects.select_for_update().select_related("event").get(pk=self.pk)
        if locked.status not in (self.Status.PENDING, self.Status.CONFIRMED):
            raise StateTransitionError(f"Booking is already '{locked.status}'.")
        if locked.event.has_started:
            raise StateTransitionError("Bookings cannot be cancelled after the event starts.")
        locked._close(self.Status.CANCELLED)
        self.refresh_from_db()


class Ticket(models.Model):
    """One seat on one booking — the explicit `through` model of Booking.seats."""

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        RELEASED = "released", "Released"

    # db_index=False: the (booking, seat) unique constraint below leads with booking.
    booking = models.ForeignKey(
        Booking, on_delete=models.CASCADE, related_name="tickets", db_index=False
    )
    # Denormalised from booking.event so the double-booking rule can be a single index.
    # db_index=False: the partial unique index below leads with event.
    event = models.ForeignKey(
        Event, on_delete=models.PROTECT, related_name="tickets", db_index=False
    )
    seat = models.ForeignKey(Seat, on_delete=models.PROTECT, related_name="tickets")
    price = models.DecimalField(
        max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal("0.00"))]
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)

    class Meta:
        ordering = ["seat__section", "seat__row", "seat__number"]
        constraints = [
            models.UniqueConstraint(
                fields=["event", "seat"],
                condition=Q(status="active"),
                name="one_active_ticket_per_seat_per_event",
            ),
            models.UniqueConstraint(fields=["booking", "seat"], name="unique_seat_per_booking"),
        ]

    def __str__(self):
        return f"{self.booking.reference}: {self.seat}"

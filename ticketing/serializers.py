from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.conf import settings
from django.utils import timezone
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from .models import Booking, Event, Seat, StateTransitionError, Ticket, Venue

User = get_user_model()


# ------------------------------------------------------------------------ users
class UserSerializer(serializers.ModelSerializer):
    role = serializers.SerializerMethodField()
    password = serializers.CharField(
        write_only=True, required=False, style={"input_type": "password"}
    )

    class Meta:
        model = User
        fields = [
            "id",
            "username",
            "email",
            "first_name",
            "last_name",
            "is_staff",
            "is_active",
            "role",
            "password",
            "date_joined",
        ]
        read_only_fields = ["id", "date_joined"]

    @extend_schema_field(serializers.ChoiceField(choices=["admin", "standard"]))
    def get_role(self, obj):
        return "admin" if obj.is_staff else "standard"

    def validate_password(self, value):
        validate_password(value)
        return value

    def create(self, validated_data):
        password = validated_data.pop("password", None)
        if not password:
            raise serializers.ValidationError({"password": ["This field is required."]})
        user = User(**validated_data)
        user.set_password(password)
        user.save()
        return user

    def update(self, instance, validated_data):
        password = validated_data.pop("password", None)
        instance = super().update(instance, validated_data)
        if password:
            instance.set_password(password)
            instance.save(update_fields=["password"])
        return instance


class RegisterSerializer(serializers.ModelSerializer):
    """Self-service sign-up. Always creates a *standard* (non-admin) user."""

    password = serializers.CharField(write_only=True, style={"input_type": "password"})

    class Meta:
        model = User
        fields = ["id", "username", "email", "first_name", "last_name", "password"]
        read_only_fields = ["id"]
        extra_kwargs = {"email": {"required": True}}

    def validate(self, attrs):
        candidate = User(username=attrs.get("username"), email=attrs.get("email"))
        try:
            validate_password(attrs["password"], user=candidate)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"password": list(exc.messages)})
        return attrs

    def create(self, validated_data):
        return User.objects.create_user(**validated_data)


# ------------------------------------------------------------------- venues/seats
class VenueSerializer(serializers.ModelSerializer):
    seat_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Venue
        fields = [
            "id",
            "name",
            "city",
            "address",
            "seat_count",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class SeatSerializer(serializers.ModelSerializer):
    label = serializers.CharField(read_only=True)

    class Meta:
        model = Seat
        fields = ["id", "venue", "section", "row", "number", "seat_type", "label"]
        read_only_fields = ["id"]


class SeatGenerateSerializer(serializers.Serializer):
    """Bulk-create a block of seats: every row in `rows` gets `seats_per_row` seats."""

    section = serializers.CharField(max_length=30)
    rows = serializers.ListField(
        child=serializers.RegexField(r"^[A-Z]{1,3}$"), min_length=1, max_length=52
    )
    seats_per_row = serializers.IntegerField(min_value=1, max_value=200)
    start_number = serializers.IntegerField(min_value=1, default=1)
    seat_type = serializers.ChoiceField(choices=Seat.SeatType.choices, default="standard")

    def validate_rows(self, value):
        if len(value) != len(set(value)):
            raise serializers.ValidationError("Rows must be unique.")
        return value


# ------------------------------------------------------------------------ events
class EventSerializer(serializers.ModelSerializer):
    venue_name = serializers.CharField(source="venue.name", read_only=True)
    organiser = serializers.CharField(source="organiser.username", read_only=True, default=None)
    seats_total = serializers.IntegerField(read_only=True)
    seats_sold = serializers.IntegerField(read_only=True)
    seats_available = serializers.IntegerField(read_only=True)

    class Meta:
        model = Event
        fields = [
            "id",
            "title",
            "description",
            "venue",
            "venue_name",
            "starts_at",
            "ends_at",
            "base_price",
            "max_seats_per_booking",
            "status",
            "organiser",
            "seats_total",
            "seats_sold",
            "seats_available",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "status", "created_at", "updated_at"]

    def validate_venue(self, value):
        if not value.is_active:
            raise serializers.ValidationError("Venue is inactive.")
        if self.instance and self.instance.venue_id != value.pk:
            if self.instance.status != Event.Status.DRAFT:
                raise serializers.ValidationError(
                    "The venue can only change while the event is a draft."
                )
        return value

    def validate(self, attrs):
        if self.instance and self.instance.status == Event.Status.CANCELLED:
            raise StateTransitionError("Cancelled events cannot be edited.")

        starts = attrs.get("starts_at", getattr(self.instance, "starts_at", None))
        ends = attrs.get("ends_at", getattr(self.instance, "ends_at", None))
        venue = attrs.get("venue", getattr(self.instance, "venue", None))

        if starts and ends and ends <= starts:
            raise serializers.ValidationError({"ends_at": ["Must be after starts_at."]})
        if "starts_at" in attrs and starts <= timezone.now():
            raise serializers.ValidationError({"starts_at": ["Must be in the future."]})

        if venue and starts and ends:
            clash = (
                Event.objects.filter(venue=venue, starts_at__lt=ends, ends_at__gt=starts)
                .exclude(status=Event.Status.CANCELLED)
                .exclude(pk=getattr(self.instance, "pk", None))
                .first()
            )
            if clash:
                raise serializers.ValidationError(
                    {"starts_at": [f"Overlaps '{clash.title}' at this venue."]}
                )
        return attrs


class SeatMapEntrySerializer(serializers.Serializer):
    id = serializers.IntegerField()
    label = serializers.CharField()
    section = serializers.CharField()
    row = serializers.CharField()
    number = serializers.IntegerField()
    seat_type = serializers.CharField()
    price = serializers.DecimalField(max_digits=10, decimal_places=2)
    available = serializers.BooleanField()


# ---------------------------------------------------------------------- bookings
class TicketSerializer(serializers.ModelSerializer):
    seat_label = serializers.CharField(source="seat.label", read_only=True)
    seat_type = serializers.CharField(source="seat.seat_type", read_only=True)

    class Meta:
        model = Ticket
        fields = ["id", "seat", "seat_label", "seat_type", "price", "status"]
        read_only_fields = fields


class BookingSerializer(serializers.ModelSerializer):
    """Read representation; on update only the contact details can change."""

    event_title = serializers.CharField(source="event.title", read_only=True)
    event_starts_at = serializers.DateTimeField(source="event.starts_at", read_only=True)
    user = serializers.CharField(source="user.username", read_only=True)
    tickets = TicketSerializer(many=True, read_only=True)

    class Meta:
        model = Booking
        fields = [
            "id",
            "reference",
            "event",
            "event_title",
            "event_starts_at",
            "user",
            "status",
            "contact_name",
            "contact_email",
            "tickets",
            "total_price",
            "hold_expires_at",
            "confirmed_at",
            "cancelled_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [f for f in fields if f not in ("contact_name", "contact_email")]

    def validate(self, attrs):
        if self.instance and self.instance.status in (
            Booking.Status.CANCELLED,
            Booking.Status.EXPIRED,
        ):
            raise StateTransitionError(f"A {self.instance.status} booking cannot be edited.")
        return attrs


class BookingCreateSerializer(serializers.Serializer):
    event = serializers.PrimaryKeyRelatedField(queryset=Event.objects.select_related("venue"))
    seats = serializers.PrimaryKeyRelatedField(
        queryset=Seat.objects.all(), many=True, allow_empty=False
    )
    contact_name = serializers.CharField(max_length=120, required=False)
    contact_email = serializers.EmailField(required=False)

    def validate(self, attrs):
        request = self.context.get("request")
        if not attrs.get("contact_email") and not getattr(request.user, "email", ""):
            raise serializers.ValidationError(
                {"contact_email": ["Required because your account has no email address."]}
            )
        event, seats = attrs["event"], attrs["seats"]
        if len({s.pk for s in seats}) != len(seats):
            raise serializers.ValidationError({"seats": ["Each seat may appear only once."]})
        limit = min(event.max_seats_per_booking, settings.MAX_SEATS_PER_BOOKING)
        if len(seats) > limit:
            raise serializers.ValidationError(
                {"seats": [f"At most {limit} seats per booking for this event."]}
            )
        foreign = sorted(str(s.pk) for s in seats if s.venue_id != event.venue_id)
        if foreign:
            raise serializers.ValidationError(
                {"seats": [f"Seats not in this event's venue: {', '.join(foreign)}."]}
            )
        return attrs

    def create(self, validated_data):
        user = self.context["request"].user
        return Booking.reserve(
            user=user,
            event=validated_data["event"],
            seats=validated_data["seats"],
            contact_name=validated_data.get("contact_name")
            or user.get_full_name()
            or user.username,
            contact_email=validated_data.get("contact_email") or user.email,
        )

    def validate_contact_email(self, value):
        return value.lower()

    def to_representation(self, instance):
        return BookingSerializer(instance, context=self.context).data

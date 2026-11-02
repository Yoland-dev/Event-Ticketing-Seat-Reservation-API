from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Count, Exists, F, IntegerField, OuterRef, Prefetch, Subquery, Value
from django.db.models.functions import Coalesce
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import generics, mixins, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from .models import Booking, Event, Seat, StateTransitionError, Ticket, Venue
from .permissions import IsAdmin, IsAdminOrReadOnly, IsOwnerOrAdmin, is_admin
from .serializers import (
    BookingCreateSerializer,
    BookingSerializer,
    EventSerializer,
    RegisterSerializer,
    SeatGenerateSerializer,
    SeatMapEntrySerializer,
    SeatSerializer,
    UserSerializer,
    VenueSerializer,
)

User = get_user_model()
_conflict = OpenApiResponse(
    description="Business rule refused the request (409, standard error schema)"
)


def count_of(queryset, group_field):
    """Correlated COUNT(*) as a subquery, so other JOINs can never inflate it."""
    return Coalesce(
        Subquery(
            queryset.order_by().values(group_field).annotate(c=Count("pk")).values("c"),
            output_field=IntegerField(),
        ),
        Value(0),
    )


# ------------------------------------------------------------------------- auth
@extend_schema(tags=["auth"], summary="Register a standard (non-admin) user")
class RegisterView(generics.CreateAPIView):
    serializer_class = RegisterSerializer
    permission_classes = [permissions.AllowAny]
    authentication_classes = []


@extend_schema(tags=["auth"], summary="Current user's profile and role")
class MeView(generics.RetrieveAPIView):
    serializer_class = UserSerializer

    def get_object(self):
        return self.request.user


@extend_schema(tags=["users"])
class UserViewSet(viewsets.ModelViewSet):
    queryset = User.objects.order_by("username")
    serializer_class = UserSerializer
    permission_classes = [IsAdmin]
    filterset_fields = ["is_staff", "is_active"]
    search_fields = ["username", "email", "first_name", "last_name"]
    ordering_fields = ["username", "date_joined"]


# ---------------------------------------------------------------- venues / seats
@extend_schema(tags=["venues"])
class VenueViewSet(viewsets.ModelViewSet):
    serializer_class = VenueSerializer
    permission_classes = [IsAdminOrReadOnly]
    filterset_fields = ["city", "is_active"]
    search_fields = ["name", "city"]
    ordering_fields = ["name", "city", "seat_count"]

    def get_queryset(self):
        return Venue.objects.annotate(
            seat_count=count_of(Seat.objects.filter(venue=OuterRef("pk")), "venue")
        )

    @extend_schema(
        summary="Bulk-create a block of seats (admin)",
        request=SeatGenerateSerializer,
        responses={201: SeatSerializer(many=True)},
    )
    @action(detail=True, methods=["post"], url_path="generate-seats", permission_classes=[IsAdmin])
    def generate_seats(self, request, pk=None):
        venue = self.get_object()
        params = SeatGenerateSerializer(data=request.data)
        params.is_valid(raise_exception=True)
        data = params.validated_data
        seats = [
            Seat(
                venue=venue,
                section=data["section"],
                row=row,
                number=number,
                seat_type=data["seat_type"],
            )
            for row in data["rows"]
            for number in range(data["start_number"], data["start_number"] + data["seats_per_row"])
        ]
        with transaction.atomic():  # IntegrityError (overlap with existing seats) → 409
            created = Seat.objects.bulk_create(seats)
        return Response(SeatSerializer(created, many=True).data, status=status.HTTP_201_CREATED)


@extend_schema(tags=["seats"])
class SeatViewSet(viewsets.ModelViewSet):
    queryset = Seat.objects.all()
    serializer_class = SeatSerializer
    permission_classes = [IsAdminOrReadOnly]
    filterset_fields = ["venue", "section", "row", "seat_type"]
    ordering_fields = ["section", "row", "number"]


# ------------------------------------------------------------------------ events
@extend_schema(tags=["events"])
class EventViewSet(viewsets.ModelViewSet):
    """Standard users only ever see events that are on sale or cancelled — never drafts."""

    serializer_class = EventSerializer
    permission_classes = [IsAdminOrReadOnly]
    filterset_fields = ["status", "venue", "venue__city"]
    search_fields = ["title", "description", "venue__name"]
    ordering_fields = ["starts_at", "base_price", "title", "seats_available"]

    def get_queryset(self):
        queryset = (
            Event.objects.select_related("venue", "organiser")
            .annotate(
                seats_total=count_of(Seat.objects.filter(venue=OuterRef("venue")), "venue"),
                seats_sold=count_of(
                    Ticket.objects.filter(event=OuterRef("pk"), status=Ticket.Status.ACTIVE),
                    "event",
                ),
            )
            .annotate(seats_available=F("seats_total") - F("seats_sold"))
        )
        if getattr(self, "swagger_fake_view", False):
            return queryset.none()
        if not is_admin(self.request.user):
            queryset = queryset.exclude(status=Event.Status.DRAFT)
        return queryset

    def _reload(self, serializer):
        # Re-read through get_queryset() so the response carries the seat counts.
        serializer.instance = self.get_queryset().get(pk=serializer.instance.pk)

    def perform_create(self, serializer):
        serializer.save(organiser=self.request.user)
        self._reload(serializer)

    def perform_update(self, serializer):
        serializer.save()
        self._reload(serializer)

    def perform_destroy(self, instance):
        if instance.bookings.exists():
            raise StateTransitionError("This event has bookings; cancel it instead of deleting it.")
        instance.delete()

    def _respond(self, event):
        return Response(self.get_serializer(self.get_queryset().get(pk=event.pk)).data)

    @extend_schema(request=None, responses={200: EventSerializer, 409: _conflict})
    @action(detail=True, methods=["post"], permission_classes=[IsAdmin])
    def publish(self, request, pk=None):
        """Draft → on sale. Admin only."""
        event = self.get_object()
        event.publish()
        return self._respond(event)

    @extend_schema(request=None, responses={200: EventSerializer, 409: _conflict})
    @action(detail=True, methods=["post"], permission_classes=[IsAdmin])
    def cancel(self, request, pk=None):
        """Cancel the event and every pending/confirmed booking for it. Admin only."""
        event = self.get_object()
        event.cancel()
        return self._respond(event)

    @extend_schema(
        summary="Every seat at the venue with its price and availability for this event",
        responses={200: SeatMapEntrySerializer(many=True)},
    )
    @action(detail=True, methods=["get"], url_path="seat-map", pagination_class=None)
    def seat_map(self, request, pk=None):
        event = self.get_object()
        event.release_expired_holds()
        taken = Ticket.objects.filter(event=event, seat=OuterRef("pk"), status=Ticket.Status.ACTIVE)
        seats = Seat.objects.filter(venue_id=event.venue_id).annotate(taken=Exists(taken))
        if request.query_params.get("available") == "true":
            seats = seats.filter(taken=False)
        rows = [
            {
                "id": seat.pk,
                "label": seat.label,
                "section": seat.section,
                "row": seat.row,
                "number": seat.number,
                "seat_type": seat.seat_type,
                "price": seat.price_for(event),
                "available": not seat.taken and event.status == Event.Status.ON_SALE,
            }
            for seat in seats
        ]
        return Response(SeatMapEntrySerializer(rows, many=True).data)


# ---------------------------------------------------------------------- bookings
@extend_schema(tags=["bookings"])
class BookingViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    """Standard users see only their own bookings; admins see all of them."""

    permission_classes = [permissions.IsAuthenticated, IsOwnerOrAdmin]
    filterset_fields = ["status", "event"]
    search_fields = ["reference", "contact_name", "contact_email", "event__title"]
    ordering_fields = ["created_at", "total_price"]

    def get_serializer_class(self):
        return BookingCreateSerializer if self.action == "create" else BookingSerializer

    def get_queryset(self):
        queryset = Booking.objects.select_related("event", "user").prefetch_related(
            Prefetch("tickets", queryset=Ticket.objects.select_related("seat"))
        )
        if getattr(self, "swagger_fake_view", False):
            return queryset.none()
        if not is_admin(self.request.user):
            queryset = queryset.filter(user=self.request.user)
        return queryset

    @extend_schema(
        summary="Hold seats for an event (creates a pending booking)",
        request=BookingCreateSerializer,
        responses={201: BookingSerializer, 409: _conflict},
    )
    def create(self, request, *args, **kwargs):
        return super().create(request, *args, **kwargs)

    def perform_destroy(self, instance):
        """Deleting is only for abandoning a pending hold; paid bookings are cancelled."""
        if instance.status != Booking.Status.PENDING:
            raise StateTransitionError(
                "Only pending bookings can be deleted; use cancel for confirmed ones."
            )
        instance.delete()

    def _transition(self, method_name):
        booking = self.get_object()
        getattr(booking, method_name)()
        return Response(BookingSerializer(self.get_queryset().get(pk=booking.pk)).data)

    @extend_schema(request=None, responses={200: BookingSerializer, 409: _conflict})
    @action(detail=True, methods=["post"])
    def confirm(self, request, pk=None):
        """Pending → confirmed (payment captured). Fails with 409 `hold_expired` if too late."""
        return self._transition("confirm")

    @extend_schema(request=None, responses={200: BookingSerializer, 409: _conflict})
    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        """Pending/confirmed → cancelled, releasing the seats. Not after the event starts."""
        return self._transition("cancel")

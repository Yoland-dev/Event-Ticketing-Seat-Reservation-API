from django.contrib import admin

from .models import Booking, Event, Seat, Ticket, Venue


class SeatInline(admin.TabularInline):
    model = Seat
    extra = 0


@admin.register(Venue)
class VenueAdmin(admin.ModelAdmin):
    list_display = ["name", "city", "is_active"]
    list_filter = ["city", "is_active"]
    search_fields = ["name"]
    inlines = [SeatInline]


@admin.register(Seat)
class SeatAdmin(admin.ModelAdmin):
    list_display = ["venue", "section", "row", "number", "seat_type"]
    list_filter = ["venue", "seat_type"]


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ["title", "venue", "starts_at", "base_price", "status"]
    list_filter = ["status", "venue"]
    search_fields = ["title"]
    date_hierarchy = "starts_at"


class TicketInline(admin.TabularInline):
    model = Ticket
    extra = 0
    readonly_fields = ["seat", "event", "price", "status"]


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = ["reference", "event", "user", "status", "total_price", "created_at"]
    list_filter = ["status", "event"]
    search_fields = ["reference", "contact_email"]
    readonly_fields = [
        "reference",
        "total_price",
        "hold_expires_at",
        "confirmed_at",
        "cancelled_at",
    ]
    inlines = [TicketInline]

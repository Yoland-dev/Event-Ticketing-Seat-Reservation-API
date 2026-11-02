from django.urls import include, path
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView, TokenVerifyView

from . import views

router = DefaultRouter()
router.register("venues", views.VenueViewSet, basename="venue")
router.register("seats", views.SeatViewSet, basename="seat")
router.register("events", views.EventViewSet, basename="event")
router.register("bookings", views.BookingViewSet, basename="booking")
router.register("users", views.UserViewSet, basename="user")

auth_tag = extend_schema(tags=["auth"])

urlpatterns = [
    path(
        "auth/token/",
        extend_schema_view(post=auth_tag)(TokenObtainPairView).as_view(),
        name="token_obtain_pair",
    ),
    path(
        "auth/token/refresh/",
        extend_schema_view(post=auth_tag)(TokenRefreshView).as_view(),
        name="token_refresh",
    ),
    path(
        "auth/token/verify/",
        extend_schema_view(post=auth_tag)(TokenVerifyView).as_view(),
        name="token_verify",
    ),
    path("auth/register/", views.RegisterView.as_view(), name="register"),
    path("auth/me/", views.MeView.as_view(), name="me"),
    path("", include(router.urls)),
]

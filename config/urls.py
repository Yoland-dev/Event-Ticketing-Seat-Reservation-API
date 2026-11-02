from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView
from drf_spectacular.views import SpectacularAPIView, SpectacularRedocView, SpectacularSwaggerView

from ticketing.health import HealthView

urlpatterns = [
    path("", RedirectView.as_view(url="/api/docs/", permanent=False)),
    path("admin/", admin.site.urls),
    path("health/", HealthView.as_view(), name="health"),
    path("api/", include("ticketing.urls")),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("api/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
]

# Django-level errors (unknown URLs, crashes outside DRF views) use the same JSON schema.
handler400 = "ticketing.exceptions.json_400"
handler403 = "ticketing.exceptions.json_403"
handler404 = "ticketing.exceptions.json_404"
handler500 = "ticketing.exceptions.json_500"

"""
Django settings for the Halcyon Event Ticketing & Seat Reservation API.

All environment-specific values are read from environment variables so the same
image runs unchanged locally, in CI and in production (12-factor style).
"""

import os
from datetime import timedelta
from pathlib import Path

import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


# --------------------------------------------------------------------------- core
SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "dev-only-insecure-secret-key-change-me-in-every-real-environment",
)
DEBUG = env_bool("DJANGO_DEBUG", False)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,0.0.0.0")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")

SERVICE_NAME = os.environ.get("SERVICE_NAME", "halcyon-ticketing-api")
SERVICE_VERSION = os.environ.get("APP_VERSION", "1.0.0")

# How long seats stay reserved while a booking is pending (awaiting payment).
BOOKING_HOLD_MINUTES = int(os.environ.get("BOOKING_HOLD_MINUTES", "10"))
# Upper limit on seats in one booking, unless an event sets a lower one.
MAX_SEATS_PER_BOOKING = int(os.environ.get("MAX_SEATS_PER_BOOKING", "10"))

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework_simplejwt",
    "django_filters",
    "drf_spectacular",
    "ticketing",
]

MIDDLEWARE = [
    "ticketing.middleware.RequestIDMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# ----------------------------------------------------------------------- database
# PostgreSQL in docker-compose and CI (DATABASE_URL); SQLite only as a zero-setup
# fallback for quick local hacking outside Docker.
DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=int(os.environ.get("DB_CONN_MAX_AGE", "60")),
        conn_health_checks=True,
    )
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# ------------------------------------------------------------------- static files
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}

# ------------------------------------------------------------------------ security
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
SESSION_COOKIE_SECURE = env_bool("DJANGO_SECURE_COOKIES", False)
CSRF_COOKIE_SECURE = env_bool("DJANGO_SECURE_COOKIES", False)
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
# Off by default so `docker-compose up` works over plain HTTP; turn on behind TLS.
SECURE_SSL_REDIRECT = env_bool("DJANGO_SSL_REDIRECT", False)
SECURE_HSTS_SECONDS = int(os.environ.get("DJANGO_HSTS_SECONDS", "0"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = SECURE_HSTS_SECONDS > 0

# ---------------------------------------------------------------- REST framework
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 20,
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "EXCEPTION_HANDLER": "ticketing.exceptions.api_exception_handler",
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": os.environ.get("THROTTLE_ANON", "60/min"),
        "user": os.environ.get("THROTTLE_USER", "600/min"),
    },
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=int(os.environ.get("JWT_ACCESS_MINUTES", "30"))),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=int(os.environ.get("JWT_REFRESH_DAYS", "1"))),
    "AUTH_HEADER_TYPES": ("Bearer",),
    "UPDATE_LAST_LOGIN": True,
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Halcyon Event Ticketing & Seat Reservation API",
    "DESCRIPTION": (
        "Venues, seats, events and seat bookings for Halcyon Systems clients. "
        "Seats are held for a few minutes while a booking is pending and can never "
        "be sold twice. Authenticate with `POST /api/auth/token/`, then "
        "click **Authorize** and paste the access token."
    ),
    "VERSION": SERVICE_VERSION,
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
    "SWAGGER_UI_SETTINGS": {"persistAuthorization": True, "displayRequestDuration": True},
    "TAGS": [
        {"name": "auth", "description": "Tokens, registration and current user"},
        {"name": "venues"},
        {"name": "seats"},
        {"name": "events"},
        {"name": "bookings"},
        {"name": "users", "description": "Admin-only user management"},
        {"name": "health"},
    ],
}

# ------------------------------------------------------------------------- logging
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"},
    },
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "standard"}},
    "root": {"handlers": ["console"], "level": os.environ.get("LOG_LEVEL", "INFO")},
}

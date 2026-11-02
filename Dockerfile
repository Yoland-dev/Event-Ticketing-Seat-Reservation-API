# ---------------------------------------------------------------------------
# Multi-stage build
#   1. builder – resolves and compiles every dependency into wheels
#   2. runtime – installs only those wheels into a clean slim image, runs as a
#                non-root user, has no pip cache, compilers or build tooling
# ---------------------------------------------------------------------------
ARG PYTHON_VERSION=3.12

############################ stage 1: builder ##############################
FROM python:${PYTHON_VERSION}-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
# Copy only the requirements first so this layer is cached until they change.
COPY requirements.txt .
RUN pip wheel --wheel-dir /wheels -r requirements.txt

############################ stage 2: runtime ##############################
FROM python:${PYTHON_VERSION}-slim AS runtime

ARG APP_VERSION=dev
LABEL org.opencontainers.image.title="halcyon-ticketing-api" \
      org.opencontainers.image.description="Event Ticketing & Seat Reservation API" \
      org.opencontainers.image.version="${APP_VERSION}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    APP_VERSION=${APP_VERSION} \
    DJANGO_SETTINGS_MODULE=config.settings

RUN groupadd --system app \
 && useradd --system --gid app --home-dir /app --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app

RUN --mount=type=bind,from=builder,source=/wheels,target=/wheels \
    pip install --no-index --find-links=/wheels /wheels/*.whl

# Code is owned by root and read-only to the app user.
COPY . .

# Static files (admin, DRF browsable API) are baked into the image and served by
# WhiteNoise. The secret key here is a throwaway used only for this build step.
RUN DJANGO_SECRET_KEY=collectstatic-build-only python manage.py collectstatic --noinput \
 && chmod +x docker/entrypoint.sh

USER app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/', timeout=4).status == 200 else 1)"

ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["gunicorn", "config.wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "3", \
     "--timeout", "30", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]

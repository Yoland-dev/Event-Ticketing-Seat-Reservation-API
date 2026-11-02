#!/bin/sh
# Container entrypoint: apply migrations, optionally bootstrap an admin and demo
# data, then hand PID 1 to the real process (gunicorn by default).
set -eu

echo "[entrypoint] applying database migrations"
python manage.py migrate --noinput

if [ -n "${DJANGO_SUPERUSER_USERNAME:-}" ] && [ -n "${DJANGO_SUPERUSER_PASSWORD:-}" ]; then
  python manage.py ensure_superuser
fi

if [ "${SEED_DEMO_DATA:-false}" = "true" ]; then
  echo "[entrypoint] loading demo data"
  python manage.py seed_demo
fi

exec "$@"

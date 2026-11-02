# Halcyon Event Ticketing & Seat Reservation API

**Ticket:** HS-2026-0872 · **Stack:** Django 5.2 LTS · Django REST Framework · SimpleJWT · PostgreSQL 16 · Docker · GitHub Actions

A production-style backend microservice for selling reserved-seat tickets. Admins set up venues, seat maps and events; customers browse events, view a live seat map, hold seats while they pay and confirm or cancel their bookings.

The core guarantee — **a seat can never be sold twice for the same event** — is enforced by a PostgreSQL partial unique index, so it holds even when many requests race for the same seat (verified: 10 simultaneous requests for one seat → exactly one booking, nine `409 seat_unavailable`).

| | |
|---|---|
| Swagger UI | `http://localhost:8000/api/docs/` |
| ReDoc | `http://localhost:8000/api/redoc/` |
| OpenAPI schema | `http://localhost:8000/api/schema/` |
| Health check | `http://localhost:8000/health/` |
| Django admin | `http://localhost:8000/admin/` |

---

## 1. Run it locally with Docker Compose

Prerequisites: Docker Desktop (or Docker Engine with the Compose plugin).

```bash
git clone <your-repo-url> halcyon-ticketing-api
cd halcyon-ticketing-api
cp .env.example .env          # optional – every value has a working default
docker compose up --build
```

On first start the API container waits for PostgreSQL to report healthy, applies migrations, creates the admin account and loads demo data (2 venues, 118 seats, 3 on-sale events and 1 draft). When the logs show gunicorn listening, open <http://localhost:8000/api/docs/>.

**Demo accounts (local only — change them for anything real):**

| Username | Password | Role |
|---|---|---|
| `admin` | `HalcyonAdmin!2026` | admin (`is_staff`) |
| `fan` | `HalcyonFan!2026` | standard user |

Useful commands:

```bash
docker compose logs -f api
docker compose exec api python manage.py release_expired_holds   # run the hold sweeper by hand
docker compose exec api python manage.py createsuperuser
docker compose down          # stop (data kept in the pgdata volume)
docker compose down -v       # stop and wipe the database
```

### Running without Docker

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
export DATABASE_URL=postgres://halcyon:halcyon@localhost:5432/halcyon   # omit to use SQLite
python manage.py migrate && python manage.py seed_demo
python manage.py runserver
```

### Tests, lint and coverage

```bash
black --check . && flake8 .
pytest --cov                     # fails below 80 % (configured in pyproject.toml)
```

Current result: **111 tests, 99.7 % line + branch coverage**, run against PostgreSQL.

---

## 2. Design

### Data model

```
Venue ──1:N──► Seat
  │              ▲
 1:N             │  M2M  Booking.seats  (through Ticket)
  ▼              │
Event ──1:N──► Booking ──1:N──► Ticket ──FK──► Seat
  │              │                │
  FK             FK               FK ──► Event (copied from the booking so the
  ▼              ▼                           no-double-booking rule is one index)
User           User
(organiser)    (customer)
```

| Model | Purpose | Key validation |
|---|---|---|
| `Venue` | A place with a seat map | unique name |
| `Seat` | One physical seat: section, row, number, type | unique (venue, section, row, number); row `A`–`ZZZ`; number ≥ 1 (validator **and** DB `CHECK`) |
| `Event` | A show at a venue | ends after it starts (DB `CHECK`), starts in the future, base price ≥ 0, **no overlapping events at the same venue**, venue must be active |
| `Booking` | A customer's order for an event | seats belong to the event's venue, no duplicates, max seats per booking, event on sale and not started |
| `Ticket` | One seat on one booking, with the price paid | **one active ticket per (event, seat)** — partial unique index |

`Booking.seats` is a many-to-many to `Seat` through the explicit `Ticket` model, which carries the price paid and whether the seat is still held (`active`) or was given back (`released`). Released tickets are kept, so history survives cancellations.

**Pricing:** seat price = event base price × seat-type multiplier (standard 1.0, accessible 1.0, premium 1.5, VIP 2.5). The price is stored on the ticket at booking time, so later price changes never alter existing bookings.

### Booking lifecycle

```
            POST /bookings/                    POST /{id}/confirm/
 (seats) ─────────────────────► PENDING ───────────────────────────► CONFIRMED
                                   │  seats held for                     │
                                   │  BOOKING_HOLD_MINUTES (10)          │
          hold runs out ◄──────────┤                                     │
                │                  │ POST /{id}/cancel/  or  DELETE      │ POST /{id}/cancel/
                ▼                  ▼                                     ▼
             EXPIRED           CANCELLED ◄───────────────────────────────┘
          (seats released)   (seats released; not allowed once the event has started)
```

- **Holds:** a pending booking reserves its seats for 10 minutes (configurable). Confirming after that returns `409 hold_expired` and the booking becomes `expired`.
- **Releasing expired holds** happens three ways: lazily before every new booking for that event, lazily whenever a seat map is viewed, and in bulk via `python manage.py release_expired_holds` (meant to run every minute from cron or a scheduler). A partial index makes that sweep cheap.
- **Cancelling an event** (admin) cancels every pending and confirmed booking for it and releases all seats in one transaction.
- `confirm` represents "payment captured". A real payment provider would call it from its webhook.

### Roles and permissions

| Action | Standard user | Admin |
|---|---|---|
| Browse venues and seats | ✅ | ✅ |
| Browse events | on-sale and cancelled only (drafts → 404) | all, including drafts |
| Create / update / delete venues, seats, events | ❌ 403 | ✅ |
| Publish or cancel an event | ❌ 403 | ✅ |
| Book seats | ✅ | ✅ |
| See bookings | own only (others → 404) | all |
| Edit contact details / confirm / cancel / delete a hold | own only | any |
| Manage users (`/api/users/`) | ❌ 403 | ✅ |

Roles come from Django's `is_staff` flag. Self-registration always creates a standard user and ignores any `is_staff` in the payload. Other people's bookings return 404 rather than 403, so booking IDs can't be probed.

### Endpoints

All under `/api/`, all JWT-protected except `auth/token/*`, `auth/register/`, the docs and `/health/`. List endpoints are paginated (`?page=`) and support `?search=` and `?ordering=`.

| Resource | Endpoints | Filters |
|---|---|---|
| Auth | `POST auth/token/`, `auth/token/refresh/`, `auth/token/verify/`, `auth/register/` · `GET auth/me/` | |
| Venues | `GET POST venues/` · `GET PUT PATCH DELETE venues/{id}/` · `POST venues/{id}/generate-seats/` | `city`, `is_active` |
| Seats | `GET POST seats/` · `GET PUT PATCH DELETE seats/{id}/` | `venue`, `section`, `row`, `seat_type` |
| Events | `GET POST events/` · `GET PUT PATCH DELETE events/{id}/` · `POST {id}/publish/` `{id}/cancel/` · `GET {id}/seat-map/` | `status`, `venue`, `venue__city` |
| Bookings | `GET POST bookings/` · `GET PUT PATCH DELETE bookings/{id}/` · `POST {id}/confirm/` `{id}/cancel/` | `status`, `event` |
| Users (admin) | full CRUD | `is_staff`, `is_active` |

Events include computed `seats_total`, `seats_sold` and `seats_available`, which you can sort by (`?ordering=seats_available`). The seat map returns every seat with its price and whether it can be booked (`?available=true` to show only free seats).

### Standard error schema

Every error — from DRF, Django's URL resolver, the database or a business rule — has the same shape:

```json
{
  "error": {
    "status": 409,
    "code": "seat_unavailable",
    "message": "Already booked or held: Floor A2.",
    "details": null,
    "path": "/api/bookings/",
    "method": "POST",
    "request_id": "7fcabf95d72941ebb6d2a5b28bab49b5",
    "timestamp": "2026-11-01T15:25:49.617199+00:00"
  }
}
```

| Situation | Status | `code` |
|---|---|---|
| Field validation failed | 400 | `validation_error` (`details` holds per-field messages) |
| Malformed JSON | 400 | `parse_error` |
| No / bad token, wrong password | 401 | `not_authenticated`, `token_not_valid`, `no_active_account` |
| Role not allowed | 403 | `permission_denied` |
| Not found (including unknown URLs) | 404 | `not_found` |
| Wrong HTTP method | 405 | `method_not_allowed` |
| Seat already booked or held | 409 | `seat_unavailable` |
| Confirming after the hold ran out | 409 | `hold_expired` |
| Illegal status change (publish twice, cancel after start, edit a cancelled booking…) | 409 | `invalid_state_transition` |
| Deleting a venue/seat still referenced by events or tickets | 409 | `protected_resource` |
| Database uniqueness conflict (e.g. generating seats that already exist) | 409 | `integrity_error` |
| Rate limit | 429 | `throttled` |
| Anything unexpected | 500 | `server_error` (no internals leaked; traceback logged) |

`request_id` matches the `X-Request-ID` response header and the server log, so a client's bug report can be traced to the exact request. A safe upstream `X-Request-ID` (from a load balancer, say) is reused.

### Health check

`GET /health/` — no auth, no throttle, `Cache-Control: no-store`:

```json
{
  "status": "ok",
  "service": "halcyon-ticketing-api",
  "version": "1.0.0",
  "timestamp": "2026-11-01T15:25:34.956535+00:00",
  "started_at": "2026-11-01T15:25:34.933525+00:00",
  "uptime_seconds": 0.01,
  "uptime": "0:00:00",
  "checks": { "database": { "status": "up", "vendor": "postgresql", "latency_ms": 14.96 } }
}
```

If the database is unreachable it returns **503** with `"status": "unavailable"` and `"database": {"status": "down", "error": "OperationalError"}`, so Docker's `HEALTHCHECK`, a load balancer or a Kubernetes probe can act on the status code alone. Uptime is measured per worker process.

---

## 3. Sample cURL commands

These use the demo data that `docker compose up` loads (event 1 is *Jazz on the Highveld*; seats 1–3 are Floor A1–A3; seat 81 is VIP Box A1).

```bash
BASE=http://localhost:8000

# Health (no auth)
curl -s $BASE/health/

# Log in
ADMIN=$(curl -s -X POST $BASE/api/auth/token/ -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"HalcyonAdmin!2026"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access'])")

FAN=$(curl -s -X POST $BASE/api/auth/token/ -H "Content-Type: application/json" \
  -d '{"username":"fan","password":"HalcyonFan!2026"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access'])")

# Register a new customer
curl -s -X POST $BASE/api/auth/register/ -H "Content-Type: application/json" \
  -d '{"username":"thandi","email":"thandi@example.com","password":"Sup3r-Secure-Pass!"}'

# Who am I?
curl -s $BASE/api/auth/me/ -H "Authorization: Bearer $FAN"
```

**Browse and book (customer)**

```bash
# Events on sale, soonest first
curl -s "$BASE/api/events/?ordering=starts_at" -H "Authorization: Bearer $FAN"

# Free seats with prices for event 1
curl -s "$BASE/api/events/1/seat-map/?available=true" -H "Authorization: Bearer $FAN"

# Hold three seats (pending for 10 minutes)
curl -s -X POST $BASE/api/bookings/ -H "Authorization: Bearer $FAN" -H "Content-Type: application/json" \
  -d '{"event":1,"seats":[1,2,81]}'

# Pay → confirm
curl -s -X POST $BASE/api/bookings/1/confirm/ -H "Authorization: Bearer $FAN"

# Update the contact details on the booking
curl -s -X PATCH $BASE/api/bookings/1/ -H "Authorization: Bearer $FAN" -H "Content-Type: application/json" \
  -d '{"contact_name":"Fan Club Rep","contact_email":"rep@example.com"}'

# My bookings
curl -s "$BASE/api/bookings/?status=confirmed" -H "Authorization: Bearer $FAN"

# Someone else trying a taken seat → 409 seat_unavailable
curl -s -X POST $BASE/api/bookings/ -H "Authorization: Bearer $ADMIN" -H "Content-Type: application/json" \
  -d '{"event":1,"seats":[2,3]}'

# Cancel (releases the seats)
curl -s -X POST $BASE/api/bookings/1/cancel/ -H "Authorization: Bearer $FAN"
```

**Set up a venue and event (admin)**

```bash
# New venue
curl -s -X POST $BASE/api/venues/ -H "Authorization: Bearer $ADMIN" -H "Content-Type: application/json" \
  -d '{"name":"Durban Dome","city":"Durban","address":"1 Beach Road"}'

# Generate a 5 × 20 block of standard seats in one call
curl -s -X POST $BASE/api/venues/3/generate-seats/ -H "Authorization: Bearer $ADMIN" -H "Content-Type: application/json" \
  -d '{"section":"Floor","rows":["A","B","C","D","E"],"seats_per_row":20,"seat_type":"standard"}'

# Create an event (starts as a draft)
curl -s -X POST $BASE/api/events/ -H "Authorization: Bearer $ADMIN" -H "Content-Type: application/json" \
  -d '{"title":"Coastal Beats Festival","venue":3,"starts_at":"2026-12-20T17:00:00Z","ends_at":"2026-12-20T23:00:00Z","base_price":"450.00","max_seats_per_booking":6}'

# Put it on sale
curl -s -X POST $BASE/api/events/5/publish/ -H "Authorization: Bearer $ADMIN"

# Change the price
curl -s -X PATCH $BASE/api/events/5/ -H "Authorization: Bearer $ADMIN" -H "Content-Type: application/json" \
  -d '{"base_price":"495.00"}'

# Cancel the event (cancels every booking for it)
curl -s -X POST $BASE/api/events/5/cancel/ -H "Authorization: Bearer $ADMIN"

# A standard user trying the same → 403 in the standard error schema
curl -s -X POST $BASE/api/events/5/cancel/ -H "Authorization: Bearer $FAN"
```

---

## 4. Database index choices and reasoning

Indexes were chosen from the queries the API actually runs. Each one speeds up reads but costs write time and storage, so where a composite index already covers a foreign key, the single-column index Django adds automatically is switched off (`db_index=False`).

Two PostgreSQL facts drive most of the decisions:

1. A composite B-tree index on `(a, b)` also serves queries that filter on `a` alone, so a separate index on `a` is redundant.
2. A **partial index** (`... WHERE condition`) only contains the rows matching the condition. It is smaller and faster, and a partial **unique** index enforces uniqueness only among those rows.

| Index | Columns | Query it serves | Reasoning |
|---|---|---|---|
| `one_active_ticket_per_seat_per_event` | `Ticket(event, seat)` **UNIQUE WHERE status = 'active'** | Double-booking prevention; "is this seat taken?" in booking and the seat map | The most important index in the service. Uniqueness only applies to *active* tickets, so a seat released by a cancelled or expired booking can be sold again while the old ticket stays as history. Two requests racing for one seat cannot both insert: the database rejects the second, which the API returns as `409 seat_unavailable`. Application checks give a friendly message; this index makes the guarantee. `Ticket.event` is copied from the booking precisely so this rule fits in a single index. It also replaces the automatic index on `event`. |
| `unique_seat_per_booking` | `Ticket(booking, seat)` UNIQUE | Loading a booking's tickets; stops the same seat appearing twice in one booking | Integrity and lookup in one. Leads with `booking`, so the automatic FK index on `booking` is turned off. |
| *(automatic FK)* | `Ticket(seat)` | `PROTECT` check when deleting a seat | Kept, because nothing else leads with `seat_id`. |
| `unique_seat_position` | `Seat(venue, section, row, number)` UNIQUE | Seat map (`WHERE venue_id = ?` ordered by section/row/number), "seats at this venue" counts | Stops duplicate seats and returns them already in seat-map order, so no sort is needed. Leads with `venue`, so the automatic FK index is off. |
| `event_status_start_idx` | `Event(status, starts_at)` | The main public listing: events on sale, soonest first | Status is an equality filter and `starts_at` matches the sort order, so rows come back pre-sorted. |
| `event_venue_start_idx` | `Event(venue, starts_at)` | Venue calendars; the **overlap check** when scheduling (`venue = ? AND starts_at < ? AND ends_at > ?`) | Every event create/update runs the overlap check. Replaces the automatic FK index on `venue`. |
| `booking_user_created_idx` | `Booking(user, created_at DESC)` | "My bookings, newest first" | Applied on every request from a standard user, so it is the hottest list query. Replaces the automatic FK index on `user`. |
| `booking_event_status_idx` | `Booking(event, status)` | Admin "bookings for event X by status"; cancelling an event (finds its live bookings) | Replaces the automatic FK index on `event`. |
| `booking_pending_expiry_idx` | `Booking(hold_expires_at)` **WHERE status = 'pending'** | Hold sweeper: `status = 'pending' AND hold_expires_at <= now()` | Confirmed, cancelled and expired bookings — almost every row over time — never need sweeping, so they are left out. The index stays tiny no matter how many historical bookings exist. |
| `venue_city_idx` | `Venue(city)` | `?city=` and `?venue__city=` filters | Regional browsing. |
| Unique columns | `Booking.reference`, `Venue.name` | Lookups and duplicate prevention | PostgreSQL creates these indexes for the unique constraints; no extra index needed. |
| *(automatic FK)* | `Event(organiser)` | `SET NULL` when a user is deleted | Kept; cheap and avoids a full scan on user deletion. |

**Deliberately not indexed**

- `Event.status` or `Booking.status` on their own — a handful of distinct values gives poor selectivity; they only help inside composites or as partial-index conditions.
- `Seat.seat_type` — a small table per venue that is always reached through the venue-leading unique index first.
- Free-text `?search=` — DRF's `SearchFilter` uses `ILIKE '%term%'`, which B-tree indexes can't accelerate. At real scale I'd add a `pg_trgm` GIN index on `Event.title`.

`test_declared_indexes_exist_in_database` introspects PostgreSQL and fails if any of these named indexes disappears, and a model test proves the partial unique index rejects a double booking even when the application check is bypassed.

---

## 5. Containerisation

**Dockerfile (multi-stage)**

1. **builder** — `python:3.12-slim`; compiles every dependency into wheels. Only `requirements.txt` is copied first, so this layer stays cached until dependencies change.
2. **runtime** — a fresh `python:3.12-slim` that installs those wheels through a BuildKit bind mount (the wheels never become an image layer), then copies the code.

Hardening and optimisation:
- runs as an unprivileged `app` user; application code is root-owned and read-only to it
- no pip cache, compilers, tests or dev requirements in the final image (`.dockerignore`)
- static files collected at build time and served by WhiteNoise, so no separate web server is needed
- `HEALTHCHECK` calls `/health/` with the Python standard library (no curl in the image)
- gunicorn with 3 workers, logging to stdout/stderr
- `APP_VERSION` build arg is stamped into the image label and reported by `/health/`

**docker-compose.yml** links `api` to `postgres:16-alpine`. The API starts only after `pg_isready` reports healthy (`depends_on: condition: service_healthy`), and data persists in the `pgdata` volume.

---

## 6. CI pipeline (`.github/workflows/ci.yml`)

Runs on every push, every pull request and on demand.

```
lint ──► test ──► build ──► (push to Docker Hub, main only, optional)
```

| Job | What it proves |
|---|---|
| **lint** | `black --check` (formatting) and `flake8` (style and static errors) |
| **test** | Starts a **temporary PostgreSQL 16 service container** that exists only for the job. Runs `manage.py check`, fails on missing migrations, applies migrations, validates the OpenAPI schema with `--fail-on-warn`, then runs pytest with `--cov-fail-under=80`. Uploads coverage XML, JUnit XML and the schema as artifacts. pytest-django also creates and drops its own `test_` database inside the container. Running on real PostgreSQL matters here: the partial unique index and row locks are what the booking tests exercise. |
| **build** | Builds the image with Buildx (cached in GitHub Actions), starts the full stack with `docker compose`, waits for `/health/` to report `"status":"ok"`, then logs in and calls an authenticated endpoint and the schema. Dumps container logs on failure and always tears the stack down. |
| **push** *(optional)* | On pushes to `main`, if the repository secrets `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` exist, pushes `latest` and the commit-SHA tag to Docker Hub. Without them the step is skipped and the pipeline still passes. |

---

## 7. Configuration reference

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | SQLite file | e.g. `postgres://user:pass@host:5432/db` |
| `DJANGO_SECRET_KEY` | dev placeholder | **set a long random value in every real environment** |
| `DJANGO_DEBUG` | `false` | |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1,0.0.0.0` | comma-separated |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | – | for the admin behind a domain |
| `DJANGO_SSL_REDIRECT`, `DJANGO_HSTS_SECONDS`, `DJANGO_SECURE_COOKIES` | off / `0` / off | turn on behind TLS |
| `BOOKING_HOLD_MINUTES` | `10` | how long pending bookings hold seats |
| `MAX_SEATS_PER_BOOKING` | `10` | global cap (events can set a lower one) |
| `JWT_ACCESS_MINUTES` / `JWT_REFRESH_DAYS` | `30` / `1` | token lifetimes |
| `THROTTLE_ANON` / `THROTTLE_USER` | `60/min` / `600/min` | rate limits |
| `DJANGO_SUPERUSER_USERNAME` / `_PASSWORD` / `_EMAIL` | – | admin created on container start if missing |
| `SEED_DEMO_DATA` | `false` (`true` in compose) | load demo data on start |
| `APP_VERSION` | `1.0.0` | reported by `/health/` and the OpenAPI doc |
| `LOG_LEVEL` | `INFO` | |

---

## 8. Submission checklist

- [x] Django app with 3+ related models — 5 models; FK and M2M (`Booking.seats` through `Ticket`); indexes incl. partial indexes; validators and DB constraints
- [x] CRUD endpoints with authentication & permissions — DRF viewsets, JWT, admin vs standard roles, object-level ownership
- [x] Custom error handling (JSON schema) — one schema for DRF, Django, DB and domain errors
- [x] Multi-stage Dockerfile
- [x] docker-compose with PostgreSQL
- [x] Unit tests ≥ 80 % coverage — 111 tests, 99.7 %, enforced in CI
- [x] GitHub Actions CI pipeline (lint, test, build) — plus compose smoke test and optional Docker Hub push
- [x] `/health/` endpoint — DB connectivity, uptime, status, version
- [x] Swagger/OpenAPI docs — drf-spectacular at `/api/docs/`, `/api/redoc/`, `/api/schema/`
- [x] README with setup, cURL samples, index reasoning

**Links for the submission form**

- GitHub repository: `https://github.com/<you>/halcyon-ticketing-api`
- Docker Hub image: `https://hub.docker.com/r/<you>/ticketing-api` (after adding the two secrets and pushing to `main`)
- CI pipeline: `.github/workflows/ci.yml`
- Swagger/OpenAPI: `http://localhost:8000/api/docs/` when running locally (or your deployed URL)

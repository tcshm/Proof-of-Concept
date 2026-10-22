# RIC Backend (POC)

FastAPI + PostgreSQL (asyncpg) backend for the RIC POC.

## Prerequisites

- Windows / Azure Virtual Desktop with PowerShell
- PostgreSQL 13+ running locally
- uv: `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`
  (on machines that block install scripts: `pip install uv`)

## First-time setup

```powershell
uv python install 3.12                    # if Python 3.12 is not installed
uv sync                                   # create .venv from uv.lock
Copy-Item .env.example .env               # then fill in passwords
uv run python -m scripts.db_bootstrap     # role ric_app + ric_poc + ric_poc_test
uv run python -m scripts.check_env        # all checks must pass
uv run alembic upgrade head               # create the 22 tables (revisions 0001-0002)
uv run python -m scripts.db_status        # revision and table count must be OK
uv run python -m scripts.seed all         # load the POC seed data (network, ITSM, notifications)
uv run pytest                             # tests (use ric_poc_test, never ric_poc)
```

## Running the API

```powershell
uv run uvicorn app.main:app --reload      # http://localhost:8000
```

| URL | Purpose |
|---|---|
| `http://localhost:8000/health` | Liveness: the process is running (never touches the database) |
| `http://localhost:8000/health/ready` | Readiness: 200 when the database answers, 503 when it does not |
| `http://localhost:8000/docs` | Interactive API documentation (when `DOCS_ENABLED=true`) |

### Mobile routes (all need `X-Site-Id`)

| Route | Returns |
|---|---|
| `GET /api/v1/store` | The store: name, timezone, local time, open now |
| `GET /api/v1/network` | Routers and switches (`wiredTiles`), WAN links, wired connections, access points with open incidents |
| `GET /api/v1/devices` | One tile per device type: colour, total, online |
| `GET /api/v1/devices/{deviceType}` | The devices of one type (e.g. `pos`, `rf_gun`), problems first |
| `GET /api/v1/incidents` | The store's active incidents (states 1-3), highest severity first, then newest |
| `GET /api/v1/incidents/{number}` | One incident (any state) with its store-visible comments |
| `GET /api/v1/appointments` | Upcoming visits (scheduled or in progress, not yet over), soonest first |
| `GET /api/v1/notifications` | The store's live notifications, newest first (`?limit=`, default 50, max 200); marks them delivered |
| `GET /api/v1/notifications/unread-count` | `{"unreadCount": n}` for the app's badge |
| `POST /api/v1/notifications/{id}/read` | Mark one notification read (reading again keeps the first read time) |
| `POST /api/v1/notifications/read-all` | Mark every live notification read |

### Admin portal routes (no `X-Site-Id`; actor from `X-User`)

| Route | Purpose |
|---|---|
| `POST /api/v1/portal/notifications` | Publish an announcement: `category`, `title`, `description`, `linkUrl?`, `targetSiteCodes`, `publishAt?` (start), `expiresAt?` (end) |
| `POST /api/v1/portal/notifications/{id}/withdraw` | Withdraw a published announcement (system notices cannot be withdrawn) |
| `GET /api/v1/portal/notifications/{id}/delivery` | Targets, delivered, read, and per store when it was delivered and read |

- Business routes live under `/api/v1`.
- The API starts even if the database is down; `/health/ready` reports 503 until it is back.
- Every element and device carries `status` (colour: `green`, `yellow`, `red`) and
  `connectivity` (`online`, `degraded`, `unknown`, `offline`).
- Incident `id` is the incident number (`INC00001234`). `ciType`, `appointmentType` and the
  notification `category` replace the mock's icons; the frontend maps them to icons.
- Incident display rules (`app/api/mappers/incidents.py`): severity from priority (1-2 high,
  3 medium, 4-5 low); badge from the hold reason when on hold (`Pending Site Visit`, ...), otherwise
  `Open`; summary is the latest comment written by IT staff, otherwise the description;
  `resolutionETA` is the ETA in store time (`Today 2PM IST`), otherwise the ETA note.

### Request headers

| Header | Required | Purpose |
|---|---|---|
| `X-Site-Id` | Mobile routes | The store, e.g. `01234`. Missing or malformed: 422. Unknown store: 404. |
| `X-User` | No | The person acting (audit trail, replies, reads). Default: `store-<site>` on mobile, `portal-user` on the portal. |
| `X-Client-App` | No | `mobile_app` or `admin_portal`, recorded in the request log (otherwise `other`). |
| `X-Request-ID` | No | A UUID chosen by the client; otherwise the API generates one. Always returned in the response. |

`X-User` and the store header are placeholders until Entra ID: then both come from the signed-in
user's token, and only `app/api/context.py` changes.

### Responses

- JSON field names are camelCase (the names the mobile app already uses).
- Dates are ISO 8601 in UTC with a `Z` suffix, e.g. `2026-10-13T07:12:00Z`.
- Display text such as `Today at 1:00 PM` or `Today 2PM IST` is formatted in the store's timezone.
- Every error has the same shape:
  `{"error": {"code": "...", "message": "...", "requestId": "...", "details": []}}`.

## Code layers

| Layer | Folder | Job |
|---|---|---|
| Route | `app/api/routes/` | HTTP only: read the request, call a query or service, return the schema |
| Dependencies | `app/api/deps.py`, `context.py` | Per request: session, clock, store (`X-Site-Id`), actor |
| Query | `app/queries/` | Read the data one screen needs, filtered by store; never writes |
| Mapper | `app/api/mappers/` | Database values to display values (pure functions) |
| Schema | `app/api/schemas/` | The JSON contract (camelCase, UTC dates) |
| Service | `app/services/` | One complete business operation: rules, locks, rows, audit, notifications, one commit |

The worker, the seed and later the MCP servers call the same queries and services as the routes.

## ITSM: incidents, journal and appointments

- `app/services/incidents.py` is the only code that writes incidents and journal entries:
  `create_incident`, `add_journal_entry`, `update_incident`, `resolve_incident`, `close_incident`,
  `cancel_incident`. `app/services/appointments.py` is the only code that writes appointments.
- Every write locks the incident row, checks the rules, writes the change with its journal entries
  and audit rows, and commits once (or only flushes with `commit=False`, used by seeds and tests).
- State changes: New, In Progress and On Hold move between each other and to Resolved or Canceled;
  Resolved can only be Closed; Closed and Canceled are final (no reopening in the POC).
- The journal is also the worker's queue. Pending: the system "Incident created." entry and every
  work note or comment by IT staff (resolving writes the close notes as a pending work note).
  Skipped: store users' replies and entries by the agent or the system.
- Resolving closes the store alert; open actions stay open. Closing or cancelling also closes
  the actions and cancels the incident's scheduled appointments (each with a store notification).
- Every appointment is tagged to an active incident of the same store (`incident_id` is NOT NULL
  since revision `0002`); appointments come from the admin portal.
- New incident numbers come from the `incident_number_seq` sequence (from `INC00002000`).

## Notifications

- `app/services/notifications.py` is the only code that writes `notifications` and
  `notification_targets`: one row per message, one target row per store (which also holds that
  store's `delivered_at`, `read_at` and `read_by`).
- Portal announcements go to an explicit list of site codes (the portal resolves "all stores" or a
  region). An unknown site code rejects the whole request. A start time in the past means now; in
  the future it schedules the announcement. Only published announcements can be withdrawn.
- System notifications use fixed templates (`app/services/notification_templates.py`), never the
  LLM, and always have exactly one target. Appointment notices ("New Appointment: ... at 01:00 PM",
  rescheduled, cancelled) are written by the appointment service in the same transaction; status
  updates (in progress, completed, no-show) send nothing. Incident update and action notices are
  created by the worker (Phase 9). Stored text uses absolute store-local times
  (`Tue 13 Oct at 1:00 PM IST`), never "Today".
- A notification is live for a store when it has a target row, is published, `publish_at <= now`
  and it has not expired. Scheduling, expiry and withdrawal need no background job.
- Expiry: appointment notices 24 hours after the visit ends; incident and action notices after
  7 days; announcements when the portal's end time says so.
- Delivery is pull-based: the app fetches the list and polls the unread count. Push notifications
  can be added later as an extra step after the rows are written.

## Network event processing

Raw Aruba events (`raw_network_events`) are turned into `network_events` (typed history) and
`network_element_current_status` (one row per element: the status the `/network` screen shows).

```powershell
uv run python -m scripts.process_events                 # process pending events
uv run python -m scripts.process_events --rebuild       # clear and replay everything
uv run python -m scripts.process_events --rebuild --as-of 2026-10-13T09:00:00Z
```

- Events are processed in arrival order (`ingestion_time`). Only events that arrived by `--as-of`
  (default: now) are processed.
- An event changes an element's status only if it is newer (by event time) than the newest
  status already applied; late, older events are stored with `status_applied = false`.
- Duplicates (same Aruba event id), unknown sites and unknown elements are recorded on the raw row
  (`duplicate`, `unmatched`); malformed payloads are marked `error` and do not stop the run.
- Every run is recorded in `event_processing_runs`. Two runs never overlap (advisory lock).
- The Aruba payload format lives only in `app/processing/aruba.py`.

## Day-to-day

| Command | Purpose |
|---|---|
| `uv run uvicorn app.main:app --reload` | Run the API locally |
| `uv run ruff check .` | Lint |
| `uv run ruff format .` | Format |
| `uv run pytest` | Run tests |
| `uv add <pkg>` | Add a dependency (updates uv.lock; commit both files) |
| `uv run alembic upgrade head` | Apply new migrations after pulling changes |
| `uv run python -m scripts.db_status` | Show revision and table count |
| `uv run python -m scripts.db_reset --yes` | Drop and recreate all tables (local/test only, data is lost) |
| `uv run python -m scripts.seed all` | Load or refresh all seed data |
| `uv run python -m scripts.seed all --reset --yes` | Rebuild the schema, then load all seed data |
| `uv run python -m scripts.seed itsm` | Reset incidents, journal entries, appointments and appointment notices |
| `uv run python -m scripts.seed notifications` | Reset the seeded portal announcements |
| `uv run python -m scripts.process_events --rebuild` | Rebuild the network status from the raw events |
| `uv run python -m scripts.request_log_tail` | Show the last API requests (`-n 50`, `--site 01234`) |

## Seed data

- Data files are in `seed/data/` (YAML); the code is in `app/seed/`.
- `uv run python -m scripts.seed <step>` with step `master`, `inventory`, `events`, `network`,
  `itsm`, `notifications` or `all`.
  - `master`: 10 stores, assignment groups, network element and device types, prompt versions.
  - `inventory`: 13 network elements and 17 devices per store, from `inventory_template.yaml`
    plus `store_overrides.yaml`.
  - `events`: replaces the 24-hour window of raw Aruba events (all `pending`).
  - `network`: runs the event processor (`--rebuild`, as of the anchor).
  - `itsm`: replaces incidents (`incidents.yaml`), their journal entries (all `pending`, for the
    worker), appointments (`appointments.yaml`) and one "New Appointment" notice per appointment
    (published at its `booked_at`), and clears anything derived from them (alerts, actions,
    AI runs, incident and appointment notifications).
  - `notifications`: replaces the portal announcements (`notifications.yaml`), published through
    the notification service: all stores, a region or single stores; already read, scheduled,
    expired and withdrawn examples.
- Times are offsets from an anchor (the run time, or `--anchor 2026-10-13T09:00:00Z`),
  so the demo data always looks current. Appointment times and firm ETAs are store-local clock
  times (e.g. 12:00 IST): today, or tomorrow if that time has already passed when the seed runs.
- IDs are deterministic (`uuid5`), so every machine gets the same ids for the same data
  (seeded portal announcements get new ids on every run).
- Store `01234` (Chennai Anna Nagar) carries the scenarios of the mobile app mock data: incidents
  INC00001234-INC00001237, appointments apt-001 to apt-003, and notifications n-001, n-002, n-004,
  n-005 (2 PM), n-008 to n-011. Seed it in the Chennai morning to see the visits as
  "Today at 12:00 PM / 1:00 PM / 2:00 PM", exactly as in the mock.
- Safe to run more than once. To change data, edit the YAML files and run the seed again.

## Database schema

- The schema is defined in plain SQL in `migrations/sql/` and applied by Alembic revisions in
  `migrations/versions/`. `0001` creates the full POC schema (22 tables); `0002` adds the incident
  number sequence and makes `appointments.incident_id` required.
- Models in `app/models/` describe columns and keys only. CHECK constraints, partial indexes and
  triggers live in the database. `tests/test_models_match_db.py` fails if models and database differ.
- Allowed values for constrained text columns are in `app/models/values.py`.
  `tests/test_values_match_db.py` fails if they differ from the database.
- New schema changes: add a new revision with
  `uv run alembic revision -m "<what changed>" --rev-id 0003` and write the `op.*` calls by hand.
  Do not rely on `--autogenerate`: it does not understand the SQL-defined checks, indexes and triggers.

## Code layout

| Path | Contents |
|---|---|
| `app/main.py` | `create_app()` factory and lifespan (engine created at startup, disposed at shutdown) |
| `app/core/` | Settings, logging, request id middleware, request log, errors, clock, time formatting, actor, status colours |
| `app/api/` | Dependencies (`deps.py`, `context.py`), the `/api/v1` router, mappers, schemas and route modules |
| `app/queries/` | Read-side SQL for the API, one module per screen, always filtered by store |
| `app/services/` | Business services, the only code that writes business tables: incidents, appointments, notifications, audit |
| `app/processing/` | Network event processor: Aruba extractors, matching, status rules, runs |
| `app/db/` | Engine, sessions, Alembic helper |
| `app/models/` | SQLAlchemy models and allowed value lists |
| `app/seed/` | Seed data loaders and generators |

## Conventions

- asyncpg is the only PostgreSQL driver. The app connects through SQLAlchemy using `postgresql+asyncpg://`.
- Configuration is read only through `app.core.config.Settings`. No direct `os.environ` access.
- Nothing calls `datetime.now()` directly: routes use `ClockDep`, services receive the time or a clock.
- Mobile routes take the store from `StoreDep` and filter every query by `store.id`.
- Routes never write SQL: read queries live in `app/queries/`, writes in `app/services/`.
  A route uses a handful of queries in total, never one query per row.
- Routes never create sessions; they use the `SessionDep` dependency. Services that write call
  `commit()` themselves; read-only routes never commit.
- Services receive an `ActorContext`, write an audit row with `record_audit()` in the same
  transaction as the change, and raise `app.core.errors` exceptions (never HTTP errors).
- Response models inherit `ApiModel` (camelCase JSON) and use `UtcDateTime` for dates.
- Every script lives in `scripts/`, runs as `uv run python -m scripts.<name>` from the repo root,
  and is safe to run more than once.
- `.env` is never committed. `.env.example` is the template.

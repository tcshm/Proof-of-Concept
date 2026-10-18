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
uv run alembic upgrade head               # create the 22 tables
uv run python -m scripts.db_status        # revision and table count must be OK
uv run python -m scripts.seed all         # load the POC seed data
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

- Business routes live under `/api/v1` (added from Phase 5).
- Mobile requests identify the store with the `X-Site-Id` header (for example `X-Site-Id: 01234`).
- Every response carries an `X-Request-ID` header. A client may send its own (a UUID);
  otherwise the API generates one. Quote it when reporting a problem.
- Every error has the same shape:
  `{"error": {"code": "...", "message": "...", "requestId": "...", "details": []}}`.
- The API starts even if the database is down; `/health/ready` reports 503 until it is back.

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

## Seed data

- Data files are in `seed/data/` (YAML); the code is in `app/seed/`.
- `uv run python -m scripts.seed <step>` with step `master`, `inventory`, `events` or `all`.
  - `master`: 10 stores, assignment groups, network element and device types, prompt versions.
  - `inventory`: 13 network elements and 17 devices per store, from `inventory_template.yaml`
    plus `store_overrides.yaml`.
  - `events`: replaces the 24-hour window of raw Aruba events (all `pending`).
- Times are offsets from an anchor (the run time, or `--anchor 2026-10-13T09:00:00Z`),
  so the demo data always looks current.
- IDs are deterministic (`uuid5`), so every machine gets the same ids for the same data.
- Store `01234` (Chennai Anna Nagar) carries the scenarios of the mobile app mock data.
- Safe to run more than once. To change data, edit the YAML files and run the seed again.

## Database schema

- The schema is defined in plain SQL in `migrations/sql/` and applied by Alembic revisions in
  `migrations/versions/`. `0001` creates the full POC schema (22 tables).
- Models in `app/models/` describe columns and keys only. CHECK constraints, partial indexes and
  triggers live in the database. `tests/test_models_match_db.py` fails if models and database differ.
- Allowed values for constrained text columns are in `app/models/values.py`.
  `tests/test_values_match_db.py` fails if they differ from the database.
- New schema changes: add a new revision with
  `uv run alembic revision -m "<what changed>" --rev-id 0002` and write the `op.*` calls by hand.
  Do not rely on `--autogenerate`: it does not understand the SQL-defined checks, indexes and triggers.

## Code layout

| Path | Contents |
|---|---|
| `app/main.py` | `create_app()` factory and lifespan (engine created at startup, disposed at shutdown) |
| `app/core/` | Settings, logging, request id middleware, error types and handlers |
| `app/api/` | Dependencies (`deps.py`), the `/api/v1` router (`router.py`) and route modules (`routes/`) |
| `app/db/` | Engine, sessions, Alembic helper |
| `app/models/` | SQLAlchemy models and allowed value lists |
| `app/seed/` | Seed data loaders and generators |

## Conventions

- asyncpg is the only PostgreSQL driver. The app connects through SQLAlchemy using `postgresql+asyncpg://`.
- Configuration is read only through `app.core.config.Settings`. No direct `os.environ` access.
- Routes never create sessions; they use the `SessionDep` dependency. Services that write call
  `commit()` themselves; read-only routes never commit.
- Services raise `app.core.errors` exceptions (`NotFoundError`, `ConflictError`, ...), never HTTP errors.
- Every script lives in `scripts/`, runs as `uv run python -m scripts.<name>` from the repo root,
  and is safe to run more than once.
- `.env` is never committed. `.env.example` is the template.

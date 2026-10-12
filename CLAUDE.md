# job-scout

Personal pipeline: fetch remote job postings → normalize → dedupe → pre-filter → score with
Claude → daily digest. `PLAN.md` is the build brief (phases, data model, scoring design).
`config/` is the source of truth for *what* to look for; don't change its intent without asking.

## Commands

```sh
uv sync                                         # install deps into .venv
uv run --env-file .env alembic upgrade head     # create/upgrade the DB
uv run --env-file .env job-scout sources check  # validate config, list enabled sources
uv run pytest                                   # offline tests (live ones excluded)
uv run pytest -m live                           # opt-in: real network / real Claude backend
uv run ruff check . && uv run ruff format .
uv run --env-file .env alembic revision --autogenerate -m "what changed"
```

`.env` (gitignored, copy from `.env.example`) points paths at `./data/` for local work;
`settings.yaml` holds the server paths under `/var/lib/job-scout`.

## Layout

- `src/job_scout/cli.py`: typer app. Every command loads and validates all of `config/` first.
  Unbuilt commands exit 1 with the phase that implements them.
- `src/job_scout/config/`: Pydantic models for the four config files. `extra="forbid"`
  everywhere, so typos fail at startup with exit code 2. `profile.md` is validated for its
  `## Output format` JSON block and hashed (SHA-256 of the raw bytes = `profile_hash`).
- `src/job_scout/db/`: SQLAlchemy 2.0 typed ORM models (`models.py`), engine with SQLite
  pragmas (`__init__.py`), `UTCDateTime` type (`types.py`).
- `src/job_scout/migrations/`: Alembic (`script_location = job_scout:migrations`).
- `tests/`: pytest; `conftest.py` clears `JOB_SCOUT_*` env vars and gives a writable copy of
  `config/` (`config_dir` fixture).

## Conventions

- **Code fetches, Claude judges.** Network calls only in fetchers. Everything else must be
  testable offline; tests use recorded fixtures, never live network (except `-m live`).
- **Pydantic for input, SQLAlchemy for storage.** Keep them separate (no SQLModel).
- **Settings precedence:** CLI flag > env var > `settings.yaml` > built-in default.
  Env vars: `JOB_SCOUT_` prefix, `__` between levels (`JOB_SCOUT_SCORING__MODES__DAILY__CLI__MAX_CALLS`).
  Short aliases `JOB_SCOUT_BACKEND` and `JOB_SCOUT_MODEL` win over the long forms. Flags reach
  settings as a nested override dict (`load_config(dir, overrides)`); per-run batching flags go
  through `Scoring.limits(mode, backend, **flags)`.
- **Database:** portable SQL only (Postgres must need just a new `JOB_SCOUT_DATABASE_URL`).
  SQLite runs in WAL mode with `foreign_keys=ON`. Timestamps are tz-aware UTC; naive datetimes
  are rejected on write. Enums are stored as VARCHAR (no native enum, no CHECK), so adding a
  value needs no migration. Every model change needs an Alembic revision;
  `tests/test_db.py::test_migration_matches_models` fails otherwise.
- Without `relationship()`s, the ORM doesn't order inserts across tables: flush parents
  (e.g. `Source`) before adding children that reference them in the same session.
- **Fail per source, not per run.** One broken source must not stop the others.
- **Secrets:** `CLAUDE_CODE_OAUTH_TOKEN` (cli backend) and `ANTHROPIC_API_KEY` (api backend)
  come from the environment, never from files in git. The API key must never reach the
  `claude` subprocess.
- Respect robots.txt, the per-source terms and rate limits in `sources.yaml`. Never implement
  anything under `skipped:` without asking. No LinkedIn, Indeed, Wellfound or Google Jobs.
- When an endpoint in `sources.yaml` turns out wrong, fix the config and say so.
- `config/filters.yaml` and `config/profile.md` are read-only on disk; ask before
  editing them.
- Style: ruff (line length 100), type hints everywhere, `from __future__ import annotations` in `src/`.

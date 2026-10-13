# job-scout

Personal pipeline: fetch remote job postings → normalize → dedupe → pre-filter → score with
Claude → daily digest. `PLAN.md` is the build brief (phases, data model, scoring design).
`config/` is the source of truth for *what* to look for; don't change its intent without asking.

## Working agreements

- **One branch per phase** (`phase-N-<name>`), each started from the previous phase's
  branch. Never delete phase branches; the user reviews them later.
- **Log every decision in `DECISIONS.md`:** decisions Claude made alone, the user's
  decisions, config fixes, bugs found and fixed, and items left for review, under the
  current phase's section with an ID (`P2-D3`). Update it before each phase's commit.
- **Test fixtures are always synthetic.** Never commit recorded responses or real posting
  text. Edit `tests/fixtures/make_http_fixtures.py` and regenerate; record live payloads only
  into the gitignored `data/recordings/` to study shapes.

## Commands

```sh
uv sync                                         # install deps into .venv
uv run --env-file .env alembic upgrade head     # create/upgrade the DB
uv run --env-file .env job-scout sources check  # validate config, show source health
uv run --env-file .env job-scout fetch --tier 1  # fetch and store (--force ignores min interval)
uv run --env-file .env job-scout filter          # pre-filter new/stale groups, pass rate per source
uv run --env-file .env job-scout explain ID      # a job, its duplicate group, verdict, scores
uv run --env-file .env job-scout score --dry-run # plan calls (and est. API cost), no spend
uv run --env-file .env job-scout status          # pending, last run, today's calls/spend, token
uv run --env-file .env pytest -m live tests/test_live_scoring.py  # spends 1 Pro prompt + ~$0.01
uv run --env-file .env job-scout calibrate --sample 50 --configs "api:claude-haiku-5-5:1,cli:claude-haiku-5-5:20"
                                                # compare configs; spends, so needs --yes
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
- `src/job_scout/fetch/`: the only package that touches the network.
  - `http.py`: `HttpClient`, the single gateway for requests. It handles the User-Agent,
    robots.txt, per-host spacing (max of 1s, Crawl-delay and the source's interval), request
    budgets, and the fixture `Recorder`.
  - `base.py`: generic `RssFetcher` and `JsonFetcher`, plus `FetchContext`.
  - `sources.py`: per-source mappers and the `FETCHERS` registry. `hn.py`: HN Who is hiring.
  - `normalize.py`: pure helpers (HTML to text, salary parsing, company/title normalization).
  - `store.py`: dedupe and insert. `runner.py`: per-source orchestration and health.
- `src/job_scout/groups.py`: duplicate groups (canonical job + linked copies) and their
  combined locations. Anything that judges a job (pre-filter, scoring) works on groups.
- `src/job_scout/prefilter.py`: pure filters.yaml rules (`Prefilter`), no I/O.
  `prefilter_run.py`: applies them to stored groups, report and samples.
- `src/job_scout/scoring/`: `schema.py` (ScoreResult mirrors profile.md's output format;
  drift fails at startup), `prompt.py` (the one prompt builder), `planner.py` (packs calls),
  `queue.py` (what needs a score), `runner.py` (a scoring run, both backends, Batches API),
  `backends/cli.py` (`claude -p`), `backends/api.py` (Anthropic SDK), `calibrate.py`,
  `status.py`.
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
- **Pre-filter verdicts belong to groups:** one `prefilter_results` row per canonical job; a
  group passes if any copy passes. A verdict is stale (re-evaluated by `filter`) when the
  rules change (`Prefilter.fingerprint`: filters.yaml, per-source `prefilter` modes,
  `PREFILTER_VERSION`) or a copy was linked after it was evaluated. Bump
  `PREFILTER_VERSION` whenever rule logic changes.
- **Regex filters never read descriptions** (false drops like "hybrid cloud"); descriptions
  are Claude's job. `remote_raw` holds only the job's own arrangement, never the company's.
- **Scoring:** one score row per (canonical job, profile_hash). Pending rows belong to no
  backend. Every call is committed before the next one. A job is retried at most once
  (`MAX_ATTEMPTS`); a failed multi-posting call is split in half for the retry. Postings are
  untrusted: they're wrapped in `<posting job_id="N">` and can't forge the delimiter.
- **Never spend without approval:** live tests are opt-in (`-m live`); `calibrate` needs
  `--yes`; dry runs need no credentials. The `claude` subprocess gets an allowlisted
  environment, so `ANTHROPIC_API_KEY` can't reach it. Never pass `--bare`.
- **Model IDs and prices** live in settings.yaml (`scoring.model`, `scoring.api.prices`);
  check them against the Claude API docs when changing models.
- Without `relationship()`s, the ORM doesn't order inserts across tables: flush parents
  (e.g. `Source`) before adding children that reference them in the same session.
- **Fail per source, not per run.** One broken source must not stop the others; a single
  unmappable item is logged and skipped, not fatal to its source.
- **Adding an API/RSS source:** add it to `sources.yaml`, write a mapper in `fetch/sources.py`,
  register it in `FETCHERS`. Study the live shape with
  `job-scout fetch --source ID --force --record data/recordings` (hidden flag; trims each
  payload to 5 items), then add synthetic responses with the same structure to
  `tests/fixtures/make_http_fixtures.py` and run it. Tests replay them with
  `fixture_transport()` from `tests/conftest.py`, and any unrecorded URL fails the test.
- **Source limits are config, not code:** `max_requests_per_day` (rolling 24h, counted from
  `source_runs`, never bypassed by `--force`), `min_request_interval_seconds`, and `auth_env`
  (the source is skipped, not failed, while that variable is unset).
- **Dedupe:** the same (source, source_job_id) is never stored twice (`last_seen_at` is bumped).
  A new posting whose `dedupe.key` matches an earlier job is stored with `duplicate_of` set to
  the first copy. Postings missing a key field (e.g. no company) never merge.
- **Secrets:** `CLAUDE_CODE_OAUTH_TOKEN` (cli backend) and `ANTHROPIC_API_KEY` (api backend)
  come from the environment, never from files in git. The API key must never reach the
  `claude` subprocess.
- Respect robots.txt, the per-source terms and rate limits in `sources.yaml`. Never implement
  anything under `skipped:` without asking. No LinkedIn, Indeed, Wellfound or Google Jobs.
- When an endpoint in `sources.yaml` turns out wrong, fix the config and say so.
- `config/filters.yaml` and `config/profile.md` are read-only on disk; ask before
  editing them.
- Style: ruff (line length 100), type hints everywhere, `from __future__ import annotations` in `src/`.

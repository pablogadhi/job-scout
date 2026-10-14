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
uv run --env-file .env job-scout run             # daily: fetch, filter, score, digest (spends)
uv run --env-file .env job-scout backlog --dry-run  # plan the backlog; without --dry-run it spends
uv run --env-file .env job-scout digest --dry-run   # reports/digest-preview.html, nothing sent
uv run --env-file .env job-scout export --sqlite    # CSV of every posting + DB snapshot
uv run --env-file .env job-scout ats detect      # seed + watchlist -> company boards (resumable)
uv run --env-file .env job-scout feedback serve  # the 👍/👎 page (needs JOB_SCOUT_FEEDBACK_SECRET)
uv run --env-file .env job-scout feedback report # Claude's scores vs your votes
uv run --env-file .env pytest -m live tests/test_live_email.py  # sends 1 real email
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

- `src/job_scout/cli/`: typer app. Every command loads and validates all of `config/` first.
  One module per area (`sources`, `jobs`, `scoring`, `digest`, `pipeline`, `ats`,
  `feedback`) holds plain command functions and their printing; `__init__.py` registers
  them in `--help` order. `common.py`: consoles, `load()`, `open_db()`, `fail()`, shared
  options. Tests patch a helper in the module that defines it (`job_scout.cli.ats.HttpClient`).
- `src/job_scout/config/`: Pydantic models for the four config files. `extra="forbid"`
  everywhere, so typos fail at startup with exit code 2. `profile.md` is validated for its
  `## Output format` JSON block and hashed (SHA-256 of the raw bytes = `profile_hash`).
  `secrets.py`: `secret(environ, name)`, the one way to read a secret.
- `src/job_scout/db/`: SQLAlchemy 2.0 typed ORM models (`models.py`), engine with SQLite
  pragmas (`__init__.py`), `UTCDateTime` type (`types.py`). `groups.py`: duplicate groups
  (canonical job + linked copies) and their combined locations. Anything that judges a job
  (pre-filter, scoring) works on groups.
- `src/job_scout/migrations/`: Alembic (`script_location = job_scout:migrations`).
- `src/job_scout/fetch/`: the only package that touches the network.
  - `http.py`: `HttpClient`, the single gateway for requests. It handles the User-Agent,
    robots.txt, per-host spacing (max of 1s, Crawl-delay and the source's interval), request
    budgets, HTTP 429 retries (Retry-After, then more spacing for that host), and the
    fixture `Recorder`. `FetchError.retryable` marks temporary failures (429, 5xx, timeouts).
  - `base.py`: generic `RssFetcher` and `JsonFetcher`, plus `FetchContext`.
  - `sources.py`: per-source mappers and the `FETCHERS` registry. `hn.py`: HN Who is hiring.
  - `normalize.py`: pure helpers (HTML to text, salary parsing, company/title normalization).
  - `store.py`: dedupe and insert. `runner.py`: per-source orchestration and health.
- `src/job_scout/prefilter/`: `rules.py` (pure filters.yaml rules, `Prefilter`, no I/O),
  `runner.py` (applies them to stored groups, report and samples).
- `src/job_scout/scoring/`: `schema.py` (ScoreResult mirrors profile.md's output format;
  drift fails at startup), `prompt.py` (the one prompt builder), `planner.py` (packs calls),
  `queue.py` (what needs a score), `runner.py` (a scoring run, both backends, Batches API),
  `backends/cli.py` (`claude -p`), `backends/api.py` (Anthropic SDK), `calibrate.py`,
  `status.py`.
- `src/job_scout/ats/`: company job boards. `seed.py` (remoteintech tarball, filtered by
  `seed_regions`/`seed_remote_policies`), `platforms.py` (per-ATS token patterns and job
  mappers), `detect.py` (careers URL -> careers page -> slug probing), `store.py`
  (`ats_companies`), `refresh.py` (`ats detect` and `run`'s weekly upkeep; a detection
  that hit only temporary failures is retried next run, up to 3 times), `fetcher.py` (the
  `remoteintech` source polls every board with status ok, watchlist included).
- `src/job_scout/report/`: what the user reads. `data.py` (scored groups into sections:
  watchlist, top, unclear location, below threshold; source health), `render.py` (Jinja2
  templates in `templates/`, HTML + plain text), `build.py` (digest and backlog report:
  write, email, record in `digests`), `mailer.py` (SMTP), `export.py` (CSV, SQLite snapshot).
- `src/job_scout/feedback/`: your votes. `links.py` (signed per-job links, secret from
  `JOB_SCOUT_FEEDBACK_SECRET`), `store.py` (one `feedback` row per canonical job, with the
  score you reacted to), `examples.py` (which judgments go into the scoring prompt),
  `server.py` (the page: stdlib HTTP server, `FeedbackApp.handle` is socket-free for tests),
  `report.py` (agreement stats).
- `Dockerfile` + `deploy/`: the server setup (`deploy/README.md`). One image (Python deps from
  `uv.lock`, the pinned native `claude` binary); `compose.yaml` mounts `config/` read-only and
  `/var/lib/job-scout`, and runs the always-on `feedback` page service from the same image;
  `entrypoint.sh` migrates, then runs the command; `systemd/` has the
  08:15 timer, the run service (`TimeoutStartSec`, `OnFailure=`) and the failure-email
  service, which pipes the run's journal into `job-scout notify-failure`.
- `tests/`: pytest; `conftest.py` clears `JOB_SCOUT_*` env vars and gives a writable copy of
  `config/` (`config_dir` fixture).

## Conventions

- **Code fetches, Claude judges.** Network calls only through `fetch.http.HttpClient` (used by
  `fetch/` and `ats/`), the scoring backends and `report/mailer.py`. Everything else must be
  testable offline; tests use synthetic fixtures or fakes. `tests/conftest.py` makes any real
  HTTP request in an offline test fail at once; only `-m live` tests may use the network.
- **Never fetched, whoever asks:** LinkedIn, Indeed, Wellfound/angel.co and Google
  (`FORBIDDEN_DOMAINS` in `fetch/http.py`), since seed companies sometimes link there.
- **Company boards:** an ATS platform in `sources.yaml` must be in `SUPPORTED_ATS`
  (`config/sources.py`) and `ats.platforms.PLATFORMS`; add both, plus synthetic fixtures, to
  support a new one. A watchlist entry overrides detection for that company.
- **Digest bookkeeping:** a `digests` row (kind `digest` or `backlog`) records the canonical
  job ids it reported; a digest never repeats them. A digest whose email failed is not
  recorded, so its jobs carry over. The below-threshold list is never recorded.
- **Reports are untrusted-content HTML:** Jinja2 autoescape stays on, links go through the
  `http_url` filter (http/https only), and CSV cells that start like a formula get a `'`.
- **Email:** `digest.email` in settings.yaml; the recipient(s) from
  `JOB_SCOUT_DIGEST__EMAIL__TO_ADDRESS` (comma-separated) and the SMTP token only from
  `JOB_SCOUT_SMTP_TOKEN`, both checked at send time. The token is wrapped in `SecretStr`,
  never stored in settings or logged. Tests that load the repo config turn email off.
- **Deploy files are tested for consistency** (`tests/test_deploy.py`): the timer's time and
  zone must match `schedule.daily_run_time`/`timezone`, data paths must live under
  `/var/lib/job-scout`, and `deploy/job-scout.env.example` must list every secret the code
  reads. Bump `CLAUDE_CODE_VERSION` in the Dockerfile only on purpose.
- **Feedback:** the digest links to `<feedback.base_url>/f/<job_id>?v=…&s=<signature>`;
  opening a link saves nothing, only the page's POST does. No links while `base_url` or
  `JOB_SCOUT_FEEDBACK_SECRET` is unset. Judgments enter the system prompt as `<judgment>`
  elements (`scoring/prompt.py`) without changing `profile_hash`, so votes never make scores
  stale; `scoring_calls.feedback_job_ids` records which were sent. Posting-derived text in a
  judgment is untrusted, like a posting.
- **Pydantic for data from outside, dataclasses for values our code builds, SQLAlchemy for
  storage.** Pydantic: config files, normalized postings (`Posting`, which every source and
  ATS mapper builds; third-party payloads aren't modelled per source), the remoteintech seed
  (`SeedCompany`), Claude's replies (`ScoreResult`, the CLI's JSON envelope). Dataclasses:
  boards, groups, outcomes, reports (frozen for values, mutable for accumulators). Keep
  Pydantic and SQLAlchemy separate (no SQLModel).
- **One definition per set of allowed values.** The StrEnums in `db/models.py` (`Backend`,
  `ScoringMode`, `SalaryPeriod`, `Vote`, statuses) are used by settings and `Posting` too;
  compare against members (`Vote.UP`), not string literals. A `Literal` alias is defined
  once and imported (`PrefilterMode` in `config/sources.py`).
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
  `PREFILTER_VERSION` (`prefilter/rules.py`) whenever rule logic changes.
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
  `claude` subprocess. Read every secret with `config.secrets.secret()` (a `SecretStr`, or
  None when unset or blank) and call `get_secret_value()` only where it's handed over.
  The environment mapping is called `environ` everywhere.
- Respect robots.txt, the per-source terms and rate limits in `sources.yaml`. Never implement
  anything under `skipped:` without asking. No LinkedIn, Indeed, Wellfound or Google Jobs.
- When an endpoint in `sources.yaml` turns out wrong, fix the config and say so.
- `config/filters.yaml` and `config/profile.md` are read-only on disk; ask before
  editing them.
- Style: ruff (line length 100), type hints everywhere, `from __future__ import annotations` in `src/`.

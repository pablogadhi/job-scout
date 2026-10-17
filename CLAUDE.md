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
  Code, config, docs and tests never cite it: a comment states the reason itself, or
  nothing.
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
pyright                                         # type check src/ and tests/: 0 errors expected
git config core.hooksPath .githooks             # once per clone: ruff + pyright before each commit
uv run --env-file .env alembic revision --autogenerate -m "what changed"
```

`.env` (gitignored, copy from `.env.example`) holds secrets and per-machine settings, on the
laptop and on the server alike. Data goes to the repo's gitignored `data/` (relative paths in
`settings.yaml`, so run commands from the repo root).

**Instances:** another person's profile runs from the same checkout as an instance in the
gitignored `instances/<name>/`, laid out like the repo root (`config/`, `data/`, `.env`).
Run its commands from that directory (`cd instances/<name> && uv run --env-file .env
job-scout ...`; `alembic -c ../../alembic.ini`). On the server, compose selects it with
`--env-file ../instances/<name>/.env` (`deploy/README.md`). The repo's own `config/` is your
instance; an instance's files never go into git.

## Layout

- `src/job_scout/cli/`: typer app. Every command loads and validates all of `config/` first.
  One module per area (`sources`, `jobs`, `scoring`, `digest`, `pipeline`, `ats`,
  `feedback`) holds plain command functions and their printing; `__init__.py` registers
  them in `--help` order. `common.py`: consoles, `load()`, `open_db()`, `fail()`, shared
  options. Tests patch a helper in the module that defines it (`job_scout.cli.ats.HttpClient`).
- `src/job_scout/config/`: Pydantic models for the four config files. `extra="forbid"`
  everywhere, so typos fail at startup with exit code 2. `profile.md` is validated for its
  `## Output format` JSON block and hashed (SHA-256 of the raw bytes = `profile_hash`). Each
  output field is typed from its example (`"a | b"`, text, "… or null", a list, a number,
  true/false, or a list holding one example object of flat, non-null fields; `profile.py`);
  the core fields (`CORE_OUTPUT_KEYS`) must be there.
  `secrets.py`: `secret(environ, name)`, the one way to read a secret.
- `src/job_scout/db/`: SQLAlchemy 2.0 typed ORM models (`models.py`), engine with SQLite
  pragmas (`__init__.py`), `UTCDateTime` type (`types.py`). `groups.py`: duplicate groups
  (canonical job + linked copies) and their combined locations. Anything that judges a job
  (pre-filter, scoring) works on groups.
- `src/job_scout/migrations/`: Alembic (`script_location = job_scout:migrations`).
- `src/job_scout/fetch/`: the only package that touches the network.
  - `http.py`: `HttpClient`, the single gateway for requests (`get`, and `post` for APIs that
    only answer a POST, like Workday's job lists). It handles the User-Agent,
    robots.txt (RFC 9309 matching with `protego`, so `*` and `$` rules apply; not
    `urllib.robotparser`, which reads `*` literally), per-host spacing (max of 1s,
    Crawl-delay and the source's interval), request budgets, HTTP 429 retries (Retry-After,
    then more spacing for that host), and the fixture `Recorder`. `FetchError.retryable`
    marks temporary failures (429, 5xx, timeouts).
  - `base.py`: generic `RssFetcher` and `JsonFetcher`, plus `FetchContext`.
  - `sources.py`: per-source mappers and the `FETCHERS` registry. `hn.py`: HN Who is hiring.
  - `normalize.py`: pure helpers (HTML to text, salary parsing, company/title normalization).
  - `store.py`: dedupe and insert. `runner.py`: per-source orchestration and health.
- `src/job_scout/prefilter/`: `rules.py` (pure filters.yaml rules, `Prefilter`, no I/O),
  `runner.py` (applies them to stored groups, report and samples).
- `src/job_scout/scoring/`: `schema.py` (`score_schema(profile)` builds the reply models
  from profile.md's output format, in its order; `CoreResult` is how the code reads any
  stored result; a core field declared differently from `CoreResult` fails at startup),
  `prompt.py` (the one prompt builder), `planner.py` (packs calls),
  `queue.py` (what needs a score), `runner.py` (a scoring run, both backends, Batches API),
  `backends/cli.py` (`claude -p`), `backends/api.py` (Anthropic SDK), `calibrate.py`,
  `status.py`, `points.py` (the score computed from a stored result and the points table
  in `config/scoring.yaml`, modelled in `config/points.py`: `check_points` against the
  profile, `compute` → `Breakdown`, `fingerprint`, `refresh` of stored scores; Phase 12,
  not wired into the commands yet).
- `src/job_scout/ats/`: company job boards. `seed.py` (remoteintech tarball, filtered by
  `seed_regions`/`seed_remote_policies`), `platforms.py` (per-ATS token patterns and job
  mappers; Workday's tokens are `host/tenant/site`, its list is POSTed a page at a time and
  each job's description is one more request, made only for a job not stored with one yet
  whose title passes filters.yaml; Jobvite and iCIMS list jobs from a page or sitemap and
  read each job page's schema.org JobPosting under the same rule), `detect.py` (careers
  URL -> careers page -> slug probing, never for Workday, Jobvite or iCIMS), `store.py` (`ats_companies`: seed, watchlist and
  `companies` rows), `refresh.py` (`ats detect` and `run`'s weekly upkeep; the seed is
  downloaded only while `remoteintech` is the enabled board source; a detection that hit
  only temporary failures is retried next run, up to 3 times), `fetcher.py` (the enabled
  board source, `remoteintech` or `company_boards`, polls every board with status ok).
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
  `data/` at `/app/data`, reads the repo's `.env`, and runs the always-on `feedback` page
  service from the same image (an instance's `.env` sets `COMPOSE_PROJECT_NAME`,
  `JOB_SCOUT_INSTANCE_DIR` and `JOB_SCOUT_FEEDBACK_PORT`); `entrypoint.sh` migrates, then
  runs the command; `systemd/` has user units (the server runs everything from
  `~/job-scout`, nothing as root): the 08:15 timer, the run service (`TimeoutStartSec`,
  `OnFailure=`) and the failure-email service, which pipes the run's journal into
  `job-scout notify-failure`; for other instances, the template `job-scout@.service` and
  one `job-scout@<name>.timer` each, reporting failures through the same failure service and
  running after yours when both are due at once, so two instances never fetch together.
- `.claude/skills/`: the project's Claude Code skills. `job-scout-profile/` builds an
  instance's config from a CV and an interview; its `references/` (profile template, filters
  example) are tested to stay valid (`tests/test_profile_skill.py`). Claude Code's Bash
  sandbox mounts it read-only (a protected path that can't be exempted): tests read it,
  and Claude edits it with its file tools, which ask you first.
- `tests/`: pytest; `conftest.py` clears `JOB_SCOUT_*` env vars. Tests load
  `tests/fixtures/config/` (`app_config`, and a writable copy via `config_dir`): a pinned
  `settings.yaml`, plus links to the real `sources.yaml`, `filters.yaml` and `profile.md`.
  Editing `config/settings.yaml` never breaks a test, except the deploy checks, which read
  the real settings on purpose. A test that needs a setting value asserts the pinned one.

## Conventions

- **Code fetches, Claude judges.** Network calls only through `fetch.http.HttpClient` (used by
  `fetch/` and `ats/`), the scoring backends and `report/mailer.py`. Everything else must be
  testable offline; tests use synthetic fixtures or fakes. `tests/conftest.py` makes any real
  HTTP request in an offline test fail at once; only `-m live` tests may use the network.
- **Never fetched, whoever asks:** LinkedIn, Indeed, Wellfound/angel.co and Google
  (`FORBIDDEN_DOMAINS` in `fetch/http.py`), since seed companies sometimes link there.
- **Company boards:** an ATS platform in `sources.yaml` must be in `SUPPORTED_ATS`
  (`config/sources.py`) and `ats.platforms.PLATFORMS`; add both, plus synthetic fixtures, to
  support a new one. A watchlist entry overrides detection for that company. Companies come
  from the remoteintech seed and from `companies:` (an instance's own organizations, by
  careers URL). One board source polls them all: `remoteintech` (method `ats_seed`, with the
  seed) or `company_boards` (`ats_boards`, without it); at most one is enabled.
- **Digest bookkeeping:** a `digests` row (kind `digest` or `backlog`) records the canonical
  job ids it reported; a digest never repeats them. A digest whose email failed is not
  recorded, so its jobs carry over. The below-threshold list is never recorded.
- **A profile's own output fields** (beyond the core ones and those the templates name,
  `NAMED_FIELDS` in `report/data.py`) are listed generically in the digest and backlog
  report ("Travel required: frequent") and get a CSV column each. Templates guard every
  non-core field with `is defined` (they use `StrictUndefined`).
- **Reports are untrusted-content HTML:** Jinja2 autoescape stays on, links go through the
  `http_url` filter (http/https only), and CSV cells that start like a formula get a `'`.
- **Email:** `digest.email` in settings.yaml; the recipient(s) from
  `JOB_SCOUT_DIGEST__EMAIL__TO_ADDRESS` (comma-separated) and the SMTP token only from
  `JOB_SCOUT_SMTP_TOKEN`, both checked at send time. The token is wrapped in `SecretStr`,
  never stored in settings or logged. Tests that load a config turn email off.
- **Deploy files are tested for consistency** (`tests/test_deploy.py`): the timer's time and
  zone must match `schedule.daily_run_time`/`timezone` (each instance's timer against its
  own `settings.yaml`, when the instance is on the machine), `job-scout@.service` must stay
  `job-scout.service` plus the instance's `--env-file` and `After=job-scout.service`, data
  paths must be relative, under `data/`, no deploy file may name a system path (`/var/lib`,
  `/opt`, `/etc/...`), and
  `.env.example` must list every secret the code reads and every env var named in `src/`,
  `config/`, `deploy/` or the Dockerfile (add new ones there). Bump `CLAUDE_CODE_VERSION`
  in the Dockerfile only on purpose.
- **Feedback:** the digest links to `<feedback.base_url>/f/<job_id>?v=…&s=<signature>`;
  opening a link saves nothing, only the page's POST does. No links while
  `JOB_SCOUT_FEEDBACK__BASE_URL` (env only: it differs per machine) or
  `JOB_SCOUT_FEEDBACK_SECRET` is unset. Judgments enter the system prompt as `<judgment>`
  elements (`scoring/prompt.py`); they never re-queue a scored job;
  `scoring_calls.feedback_job_ids` records which were sent. Posting-derived text in a
  judgment is untrusted, like a posting.
- **Pydantic for data from outside, dataclasses for values our code builds, SQLAlchemy for
  storage.** Pydantic: config files, normalized postings (`Posting`, which every source and
  ATS mapper builds; third-party payloads aren't modelled per source), the remoteintech seed
  (`SeedCompany`), Claude's replies (the per-profile `ScoreResult`, read back as
  `CoreResult`; the CLI's JSON envelope). Dataclasses:
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
- **Scoring:** one score row per canonical job, scored once. Only `rescore` queues a scored
  job again, plus `enqueue` when a copy is linked to its group (its combined locations
  changed). Editing profile.md re-scores nothing; `profile_hash` on the row records which
  profile scored it (`explain` marks older ones). Pending rows belong to no backend. Every
  call is committed before the next one. A job is retried at most once (`MAX_ATTEMPTS`); a
  failed multi-posting call is split in half for the retry. Postings are untrusted: they're
  wrapped in `<posting job_id="N">` and can't forge the delimiter.
- **Never spend without approval:** live tests are opt-in (`-m live`); `calibrate` needs
  `--yes`; dry runs need no credentials. The `claude` subprocess gets an allowlisted
  environment, so `ANTHROPIC_API_KEY` can't reach it. Never pass `--bare`.
- **Model IDs and prices** live in settings.yaml (`scoring.model`, `scoring.api.prices`);
  check them against the Claude API docs when changing models.
- Without `relationship()`s, the ORM doesn't order inserts across tables: flush parents
  (e.g. `Source`) before adding children that reference them in the same session.
- **Fail per source, not per run.** One broken source must not stop the others; a single
  unmappable item is logged and skipped, not fatal to its source.
- **Researching a source:** look for an RSS/Atom feed first (a `<link rel="alternate">`, an
  RSS icon or link, `/feed`, `/rss.xml`, per-search feeds), then an API, then HTML. For
  each way in, check robots.txt for that exact URL (protego, our User-Agent) and say plainly
  in the recommendation which ones it allows and which it denies.
- **Adding an API/RSS source:** add it to `sources.yaml`, write a mapper in `fetch/sources.py`,
  register it in `FETCHERS`. Study the live shape with
  `job-scout fetch --source ID --force --record data/recordings` (hidden flag; trims each
  payload to 5 items), then add synthetic responses with the same structure to
  `tests/fixtures/make_http_fixtures.py` and run it. Tests replay them with
  `fixture_transport()` from `tests/conftest.py`, and any unrecorded URL fails the test.
- **Source limits are config, not code:** `max_requests_per_day` (rolling 24h, counted from
  `source_runs`, never bypassed by `--force`), `min_request_interval_seconds`, `auth_env`
  (the source is skipped, not failed, while that variable is unset), `max_pages` for APIs
  we stop reading early (`fetch --max-pages N` overrides it for one run), and `categories`
  for sources that serve every category in one list (workingnomads; empty keeps all).
- **Dedupe:** the same (source, source_job_id) is never stored twice (`last_seen_at` is bumped).
  A new posting whose `dedupe.key` matches an earlier job is stored with `duplicate_of` set to
  the first copy. Postings missing a key field (e.g. no company) never merge. A mapper whose
  title carries more than the role sets `Posting.dedupe_title` (HN: the header's role), which
  only shapes `title_normalized`; the stored title stays whole.
- **Secrets:** `CLAUDE_CODE_OAUTH_TOKEN` (cli backend) and `ANTHROPIC_API_KEY` (api backend)
  come from the environment, never from files in git. The API key must never reach the
  `claude` subprocess. Read every secret with `config.secrets.secret()` (a `SecretStr`, or
  None when unset or blank) and call `get_secret_value()` only where it's handed over.
  The environment mapping is called `environ` everywhere.
- Respect robots.txt, the per-source terms and rate limits in `sources.yaml`. A source's
  `ignore_robots_txt: true` (skips the allow/disallow rules, keeps Crawl-delay) is set only
  when the user decides so for that source (today: reliefweb's feed). Never implement
  anything under `skipped:` without asking. No LinkedIn, Indeed, Wellfound or Google Jobs.
- When an endpoint in `sources.yaml` turns out wrong, fix the config and say so.
- Style: ruff (line length 100), type hints everywhere, `from __future__ import annotations` in `src/`.

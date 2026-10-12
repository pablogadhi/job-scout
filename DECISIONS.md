# Decision log

Everything Claude decided on its own, every bug found and fixed, every config change, and
every decision you made, phase by phase, for review. Each phase lives on its own branch
(`phase-N-…`), stacked on the previous one and never deleted.

IDs: `D` = Claude's decision, `U` = your decision, `C` = config fix, `B` = bug found and
fixed, `R` = left for you to review. Status of each phase's acceptance check is at the top
of its section.

---

## Phase 0: Scaffold (branch `phase-0-scaffold`)

**Acceptance:** passed. `alembic upgrade head` creates the DB (WAL mode); `job-scout sources
check` validates all four config files and lists enabled sources.

### P0 Decisions (Claude)

- **P0-D1 Branch for the first commit.** The repo was empty on `main`; committed Phase 0 on
  `phase-0-scaffold` rather than `main`, per the "branch first" rule.
- **P0-D2 Strict config.** Every config model uses `extra="forbid"`, so a typo'd key fails
  at startup (exit code 2) instead of being ignored.
- **P0-D3 Env var names.** Nested form `JOB_SCOUT_SCORING__MODES__DAILY__CLI__MAX_CALLS`
  for every setting, plus the short aliases from PLAN.md (`JOB_SCOUT_BACKEND`,
  `JOB_SCOUT_MODEL`), which win over the nested form. `JOB_SCOUT_DATABASE_URL` overrides
  `paths.database` (for a future move to Postgres).
- **P0-D4 No `.env` loading inside the app.** Local runs use `uv run --env-file .env`;
  Docker will use `env_file`. Avoids a second precedence layer.
- **P0-D5 Enums stored as VARCHAR without CHECK constraints.** Portable to Postgres and
  adding a value needs no migration.
- **P0-D6 UTC-only timestamps.** A custom column type rejects naive datetimes on write and
  returns UTC-aware values from SQLite.
- **P0-D7 Extra tables/columns beyond PLAN.md §6:** none in Phase 0. `scores.backend` and
  `scores.model` are nullable because a pending score has no backend yet.
  `scoring_calls` has both `exit_code` (cli) and `http_status` (api) rather than one column.
- **P0-D8 Unbuilt commands exit 1** with the phase that implements them.
- **P0-D9 `profile.md` validation:** checks the `## Output format` JSON block parses and has
  the required keys. The Pydantic score model itself is derived in Phase 3.
- **P0-D10 Lint rule RUF012 disabled** (false positive for Pydantic defaults and SQLAlchemy's
  `type_annotation_map`).

### P0 Your decisions

- **P0-U1 Only tier 1 sources are enabled by default** (you changed the code). Others opt in
  with `enabled: true` per source.
- **P0-U2 Dedupe key is `[company_normalized, title_normalized]`** (`canonical_url`
  dropped so cross-posted jobs match).

### P0 Config fixes

- **P0-C1 `sources.yaml` `skipped`:** 9 entries had an unquoted comma in `reason:` inside a
  YAML flow mapping, which split the reason and created a stray key. Quoted them.

### P0 Bugs found and fixed

- **P0-B1** Typer's CliRunner left the root logger pointing at a closed stream between
  tests ("Logging error" noise). Tests now restore logging after each test.

---

## Phase 1: Tier 1 fetchers (branch `phase-1-fetchers`)

**Acceptance:** passed, except Findwork (needs `FINDWORK_API_KEY`, see P1-R1).
Live run on 2026-10-12: 12 sources stored 2,734 postings; a second run added 0 duplicates
(one genuinely new HN comment); `sources check` shows per-source health.

### P1 Decisions (Claude)

- **P1-D1 One generic fetcher per method** (`RssFetcher`, `JsonFetcher`) plus a small
  mapper per source; HN is the one bespoke fetcher (two-step API).
- **P1-D2 One HTTP gateway** (`fetch/http.py`) for every request: User-Agent, timeout,
  robots.txt (cached per host per run), spacing per host = max(1 s, robots `Crawl-delay`,
  the source's `min_request_interval_seconds`), request budgets.
- **P1-D3 robots.txt handling:** 404 = allow all; 401/403 = disallow all; 5xx or network
  error = the source fails for this run.
- **P1-D4 New table `source_runs`** (migration `832d7dbee9d9`): one row per source attempt
  with requests made, items fetched/new/duplicate/skipped, error. Used for the rolling-24h
  request budget and for source health.
- **P1-D5 New per-source config fields:** `max_requests_per_day` (enforced from
  `source_runs`, never bypassed by `--force`), `min_request_interval_seconds`, and
  `auth_env` (source is *skipped*, not failed, while the variable is unset).
- **P1-D6 `fetch --force`** ignores `min_interval_hours` only. `--source` may name a
  disabled source explicitly.
- **P1-D7 Fail per source, and per item:** a source error is recorded and the run moves on;
  an item that doesn't map is logged, counted and skipped.
- **P1-D8 A source is fetched fully before anything is written**, then stored in one
  transaction, so a failure mid-fetch never half-writes a source.
- **P1-D9 Dedupe semantics:** the same (source, source_job_id) is never stored twice
  (`last_seen_at` bumped). A new posting whose dedupe key matches an earlier job is stored
  with `duplicate_of` pointing at the first copy (kept, not dropped). A posting missing a key
  field (e.g. no company) gets a unique key and never merges.
- **P1-D10 Title and company normalization** for the dedupe key: lowercase; legal suffixes
  (Inc, LLC, GmbH…), domain TLDs and parentheticals dropped from companies; location/
  arrangement parentheticals and "- Remote…" tails dropped from titles; synonyms unified
  (sr→senior, back-end→backend, full stack→fullstack, dev ops→devops).
- **P1-D11 Salary parsing is conservative:** needs a currency marker; ignores funding
  amounts ("raised $20M", "seed", "Series B"); rejects implausible values in hard
  currencies (a yearly figure under 1,000, an hourly one over 1,000). Free-text descriptions
  are only parsed on lines labelled salary/compensation/pay/rate, never anywhere else.
  Claude still reads the full text in Phase 3.
- **P1-D12 4dayweek incremental fetch:** after the first success, `posted_after` = days
  since the last success + 1, minimum 2, so windows overlap. First run fetches everything
  (2,044 postings, 21 pages). Page cap 30.
- **P1-D13 Working Nomads:** keeps `category_name == "Development"` only.
- **P1-D14 HN:** title = the comment's first line; company = first segment when the line
  is split by `|`, ` — ` or ` – `; salary parsed per segment only; deleted comments and
  replies skipped.
- **P1-D15 Remote OK:** repairs its double-encoded UTF-8 (74 of 99 descriptions) by a
  Latin-1 round trip, applied only when the whole string round-trips.
- **P1-D16 `sources check --probe`** fetches every enabled endpoint without storing
  (recorded as `kind=probe` runs; requests still count against budgets).
- **P1-D17 Schema check:** commands that use the DB exit 2 with "run `alembic upgrade head`"
  if the DB isn't at the latest revision.
- **P1-D18 HTML to text** via selectolax's lexbor backend (the modest backend is deprecated).

### P1 Your decisions

- **P1-U1 Combined locations of linked copies.** A job and all its `duplicate_of` copies
  are judged together. Implemented in Phase 2 (see P2 section), reused by scoring in Phase 3.
- **P1-U2 Fixtures are always synthetic.** The recorded live responses were never committed;
  `tests/fixtures/make_http_fixtures.py` generates invented data that mirrors each source's
  payload structure and quirks. `fetch --record` refuses to write under `tests/` and is for
  studying shapes in the gitignored `data/recordings/`.
- **P1-U3 User-Agent without contact email.** *Interpretation to confirm:* you wrote "Do not
  send the User-Agent". I read it as "don't add the email to the User-Agent", so requests
  still send `job-scout/0.1 (personal job search)`, because PLAN.md requires a configured
  User-Agent and robots.txt rules match on it. If you meant "send no User-Agent header",
  say so; note that httpx would then send its default `python-httpx/…` instead.

### P1 Config fixes

- **P1-C1 Remotive:** category `software-dev` doesn't exist (valid: `software-development`).
  The free API also ignores the category filter (every category URL returned the same 15
  jobs), so it now has one endpoint. Added `max_requests_per_day: 4`,
  `min_request_interval_seconds: 31`. Marked checked.
- **P1-C2 Findjobit:** `/rss-feed` is an HTML page listing feeds, not a feed. Switched to
  `/jobs/country/guatemala/feed` (only postings open to Guatemala). Marked checked.
- **P1-C3 Remote First Jobs `alt_api`:** robots.txt disallows `/api`; marked "don't use"
  (we use the RSS feeds anyway).
- **P1-C4 Findwork:** endpoint now `?remote=true&sort_by=date`; auth documented as
  `Authorization: Token <key>`; `auth_env: FINDWORK_API_KEY`.
- **P1-C5 `user_agent` comment** updated to record P1-U3.

### P1 Bugs found and fixed

- **P1-B1 Query params replaced the endpoint's query** (`httpx.URL(url, params=…)`). The
  first live 4dayweek run lost `limit/category/work_arrangement` and paged 30 unfiltered
  pages before the page cap stopped it (30 requests, under their 60/min limit; nothing
  stored). Now merges params.
- **P1-B2 YAML quoting** of the Findwork `auth:` note (contained a colon followed by a space); caught by config
  validation at startup.
- **P1-B3 HN header merged into the body:** text before the first `<p>` had no line break.
  HTML-to-text now breaks before and after block elements.
- **P1-B4 Salary typo** "$95,00–$115,000" parsed as $95/year; then a re-match inside the
  number gave $0–$115k. Fixed with the plausibility rule (P1-D11) and a "not mid-number"
  guard.
- **P1-B5 WWR skills** "Java, PostgreSQL, and Spring" produced the tag "and Spring".
- **P1-B6 4dayweek salaries are in cents** (per their OpenAPI spec); converted to dollars.
- **P1-B7 Funding amounts parsed as salary** ("$1.5M seed round"); now excluded.

### P1 For you to review

- **P1-R1 Findwork** is implemented from its docs but unverified: create a free account,
  put the key in `FINDWORK_API_KEY`, then run `job-scout fetch --source findwork`.
- **P1-R2 4dayweek multi-country postings:** 469 of 2,044 are per-country copies of one job
  (e.g. one title ×9 countries). With your dedupe key they link to the first copy; P1-U1
  makes sure all their locations are considered.
- **P1-R3 Low-yield sources:** Remotive's free API serves only 15 delayed jobs across all
  categories. Kept, at 1 request per run.

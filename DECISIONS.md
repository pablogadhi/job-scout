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
- **P1-D19 Company parentheticals are dropped when normalizing** ("Lumen Ledger (Remote,
  LATAM)" and "Cobalt Systems (raised $20M)" become "lumen ledger", "cobalt systems"), so HN
  headers match the same company on other boards.

### P1 Your decisions

- **P1-U1 Combined locations of linked copies.** A job and all its `duplicate_of` copies
  are judged together. Implemented in Phase 2; how it works is explained in
  "How combined locations work" under Phase 2. Scoring reuses it in Phase 3.
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

---

## Phase 2: Pre-filter (branch `phase-2-prefilter`)

**Acceptance:** passed on the local data (fetched 2026-10-12): `job-scout filter` judged
2,511 job groups (3,023 postings, 512 linked duplicates), 1,120 passed (45%), with a pass
rate per source and the top drop rules. Spot check of 20 passed and 20 dropped (seed 7)
done by Claude, not by you: no verdict was wrong against the rules as written. Misses
caused by the rules themselves are listed under P2-R1. Please re-check (P2-R5).

### How combined locations work

1. **Linking (Phase 1).** When a posting is stored, its dedupe key
   (`company_normalized|title_normalized`) is looked up. If an earlier job has the same key,
   the new posting is stored with `duplicate_of` pointing at that first job (the
   *canonical* job). Example: 4dayweek lists one job once per country; Northwind Cloud's
   "Senior Platform Engineer" for Germany arrives first, the Mexico listing becomes its copy.
2. **Groups (`groups.py`).** A group is the canonical job plus all its copies, from any
   source. `Group.combined_locations()` returns each distinct location statement labelled
   with its source, e.g. `4dayweek: Germany (remote)`, `4dayweek: Mexico (remote)`.
3. **Pre-filter.** Each copy is checked on its own; the group passes if any copy passes,
   and the reason says which copy (`… (via copy #2073 on 4dayweek) [3 copies]`). So an
   EMEA-only first listing can no longer hide a Latin America listing of the same job.
   One verdict row is stored, on the canonical job.
4. **Late copies.** If a new copy is linked after the group was judged (the LATAM listing
   appears a week later), the next `job-scout filter` re-judges the group automatically.
5. **Scoring (Phase 3, planned).** The prompt for a group carries the canonical posting
   plus a "Locations across all listings of this job" list built from
   `combined_locations()`, so Claude judges eligibility on all of them. One score per group,
   stored on the canonical job. When a group gains a copy with a new location after it was
   scored, it will be re-queued for scoring (to be built in Phase 3).
6. **Seeing it.** `job-scout explain ID` (any member of the group) lists every copy with its
   location and prints the combined list exactly as Claude will get it.

### P2 Decisions (Claude)

- **P2-D1 Verdicts belong to groups;** a group passes if any copy passes (see above).
- **P2-D2 Regex rules never read descriptions.** The `filters.yaml` header says patterns
  match "title, location and description", but measured on the local data, matching
  descriptions would drop 186 more of the 980 passing non-HN groups, and about half of the
  sampled ones were wrong: "hybrid working mode, plus up to 3 months work from anywhere",
  "benefits only applicable to U.S.-based candidates", "in-office snacks", "teams in US and
  EMEA". The right ones ("3 days a week in office required", "must be within EMEA",
  "security clearance required") are what Claude catches from the full text. This follows
  the file's own rule of thumb: drop only clear non-fits. Side effect: patterns that only
  occur in descriptions (`us work authorization`, `must reside in…`) rarely fire.
- **P2-D3 Fields per rule:** title rules read the title; remote rules read title, location
  and the source's remote/workplace field; location rules read title and location.
- **P2-D4 Rule order** title include, title exclude, remote, location, salary; the first
  failing rule is the reason, quoting the pattern and the matched text. Passing verdicts
  also carry notes (location allowed / unclear, salary meets floor / unlisted / not
  comparable).
- **P2-D5 Salary floor only in `filters.salary.currency` (USD).** Other currencies and
  unknown periods pass to Claude. Day and week rates are derived from `hours_per_year`
  (÷8, ÷40), since filters.yaml only defines hourly and monthly.
- **P2-D6 HN rule as config:** new per-source field `prefilter: remote_mention_only` (set on
  `hn_whoishiring`): passes if "remote" appears anywhere in the comment.
- **P2-D7 Staleness:** each verdict stores a fingerprint of filters.yaml, the per-source
  modes and `PREFILTER_VERSION`; a mismatch, or a copy linked after evaluation, re-queues the group.
  `filter --rerun` re-judges everything.
- **P2-D8 `job-scout filter` is its own command** (not in PLAN.md §9); `fetch` doesn't
  filter. Phase 4's `run` chains fetch, filter, score, digest.
- **P2-D9 `filter --sample N [--seed S]`** lists random passed and dropped jobs for spot
  checks with `explain`.
- **P2-D10 `explain`** shows the job, its group with locations, the combined locations,
  the verdict (flagged if stale), and scores (ready for Phase 3).
- **P2-D11 Migration `1d16fbe34935` clears old verdicts** before adding the NOT NULL
  columns; they would be stale anyway and `filter` recomputes them.
- **P2-D12 Local dev data re-mapped** from the stored raw payloads after P2-B1/B2 (no
  re-fetch); only affects your local `data/job-scout.db`.
- **P2-D13 Alembic post-write hook** now also runs `ruff check --fix`, so autogenerated
  migrations pass lint.

### P2 Bugs found and fixed

- **P2-B1 4dayweek `remote_raw` included the company's policy** ("job: remote, company:
  hybrid"): 866 remote jobs at hybrid companies would have been dropped as hybrid. Now
  `remote_raw` is the job's own arrangement; the company's goes to a tag.
- **P2-B2 PyJobs "Office"** didn't say on-site, so the remote rule missed it; now
  "on-site (Office)".
- **P2-B3 Migration would fail on a populated DB** (NOT NULL columns without defaults);
  caught in review before running (see P2-D11).
- **P2-B4 `explain` salary formatting** would crash when only one end of the range is
  known; caught in review.

### P2 For you to review

- **P2-R1 Title include gaps** (filters.yaml is read-only, so not changed). Dropped but
  possibly relevant: "Staff Engineer", "Principal Engineer", "Senior Engineer (.NET)",
  "Forward Deployed Engineer" (often backend/DevOps), "Senior Integration Engineer"
  (integrations are in your profile). About 20 of 1,148 title drops. Possible additions:
  `'\b(staff|principal|senior|lead) engineer\b'` and
  `'\b(integration|forward deployed|deployment) engineer\b'`. The rest of the title drops
  are ML/AI, frontend, management, sales and data science, as intended.
- **P2-R2 Non-USD salaries are never filtered in code** (about 350 4dayweek jobs in CAD,
  EUR, GBP, PLN…). Option: approximate `fx_to_usd` rates in filters.yaml.
- **P2-R3 Description-only signals** (hybrid, US-only, clearance) are left to Claude
  (P2-D2). If you prefer the stricter behaviour, it's a one-line change per rule.
- **P2-R4 HN passes anything that mentions remote**, including non-engineering roles
  (e.g. a product manager post), as PLAN.md specifies; Claude scores those low.
- **P2-R5 Re-check the spot check:** `job-scout filter --sample 20`, then
  `job-scout explain ID` on a few.

---

## Phase 3: Scoring (branch `phase-3-scoring`)

**Acceptance:** built and tested offline; **live checks not run** (P3-R1). Both backends,
batching, failure handling, stop reasons, backend switching and the Batches API are
covered by 55 new offline tests (213 in total) plus 2 opt-in live tests, using a fake `claude` executable and a fake SDK client.
Not yet done: scoring the same sample live on both backends, confirming Haiku through the
CLI on your Pro account, and the calibration you approve (P3-R2). No credentials for the
pipeline were present (`CLAUDE_CODE_OAUTH_TOKEN` and `ANTHROPIC_API_KEY` unset). I did not
use the `claude` CLI that is logged in on this machine, because that would spend your Pro
prompts without your OK.

### P3 Config fixes

- **P3-C1 Model ID.** `claude-haiku-5-5` (PLAN.md and settings.yaml) is not a real model ID.
  The current Haiku is `claude-haiku-4-5` ($1 input / $5 output per million tokens). Changed
  in settings.yaml and the built-in default. You asked for Haiku explicitly, so no other
  model was considered.
- **P3-C2 New settings:** `scoring.api.prices` per model (input, output, cache write, cache
  read), `batch_discount: 0.5`, `batch_poll_seconds: 60`, `batch_max_wait_minutes: 120`, and
  `scoring.cli.token_created` (date `claude setup-token` was run; drives the expiry warning).

### P3 Decisions (Claude)

- **P3-D1 One schema for both backends.** `ScoreResult` mirrors profile.md's output format
  plus `job_id`; a startup check fails if the two drift apart. The JSON schema sent to
  Claude has the keywords structured outputs rejects (`minimum`, `maximum`, …) stripped;
  Pydantic still enforces them on every reply. The API backend sends the raw schema via
  `output_config.format` rather than `messages.parse()`, because the Batches API can't use
  the parse helper and the CLI needs the same raw schema anyway.
- **P3-D2 Prompt.** System prompt = profile.md verbatim + a section on reading postings
  (one result per job id, judge location on all listings, posting text is untrusted data,
  and injected instructions go in `red_flags`). Each posting is
  `<posting job_id="N">…</posting>`; any `<posting`/`</posting` inside the text becomes
  `posting_quoted`, so a posting can't close its element early or forge another one.
- **P3-D3 Postings are built from the whole duplicate group** (your P1-U1): canonical
  title/company, the "Locations across all listings of this job" list, salary from the
  first listing that states one, description from the first listing that has one.
- **P3-D4 Re-score when a group gains a listing.** A scored group is re-queued when a copy
  is linked after it was scored (new `scores.scored_at`), since its locations changed.
- **P3-D5 Planner:** tokens estimated as characters/4; the system prompt counts against every
  call's input budget; a posting too big for an empty call still gets a call of its own.
- **P3-D6 `cli` backend invocation:** `claude -p --model … --output-format json
  --json-schema … --system-prompt … --tools "" --permission-mode dontAsk
  --strict-mcp-config --setting-sources "" --disable-slash-commands
  --no-session-persistence`, postings on stdin, run in the empty `scoring.cli.workdir`.
  No `--bare` (it ignores subscription logins). `--max-turns` isn't in this CLI version's
  help; with every tool disabled the call is a single turn anyway.
- **P3-D7 The CLI subprocess environment is an allowlist** (PATH, HOME, locale, TMPDIR, XDG
  dirs, CLAUDE_CONFIG_DIR, CLAUDE_CODE_OAUTH_TOKEN, proxy variables), not "everything minus
  ANTHROPIC_API_KEY". This blocks ANTHROPIC_AUTH_TOKEN and any other credential too.
- **P3-D8 Failure handling.** A whole-call failure (refusal, truncated, invalid JSON, CLI
  error, timeout, HTTP error after SDK retries) splits the call in half and retries each
  half once. Each job has at most 2 attempts (`MAX_ATTEMPTS`), then becomes
  `score_failed`. Item-level problems affect only that job: an invalid result costs it an
  attempt; a missing id goes back to pending, also costing an attempt (PLAN.md says
  "back to pending"; counting it stops an id that is always omitted from being retried
  forever). Unknown ids are discarded and logged; for a duplicate id the first result
  wins. A usage limit never counts as an attempt.
- **P3-D9 Commit after every call;** an interrupted run loses at most the call in flight.
- **P3-D10 Spend guard (api only):** checked before each call; the run stops with
  `spend_limit` once the estimated spend reaches `max_spend_usd_per_run`. CLI costs are
  the CLI's API-equivalent estimate (not billed on Pro) and never stop a run.
- **P3-D11 Batches API.** One batch per run. Order: create the `scoring_calls` row
  (RUNNING) and mark its jobs as in flight, *then* submit, then store the batch id. The
  spend guard is applied up front by trimming the batch to the estimated budget. The run
  polls every 60 s for up to 120 min; if the batch is still processing it stops with a new
  stop reason, `batch_pending` (no migration needed), and the next run resumes polling
  from the stored id before submitting anything new. Jobs in a batch in flight are never
  handed to another run or backend. Canceled/expired requests retry later at no attempt
  cost. Known gap: a crash between submitting and saving the batch id leaves a batch the
  next run can't see (it would resubmit those jobs, paying twice for them).
- **P3-D12 API max_tokens** per call = min(16,000, 1,000 + 900 × postings), which keeps
  non-streaming requests inside SDK timeouts.
- **P3-D13 Prompt caching is marked but has no effect today:** Haiku 4.5 caches only
  prefixes of 4,096+ tokens, and profile.md plus the rules is about 3,000. It starts paying
  off automatically if the profile grows or the model changes.
- **P3-D14 `rescore --top N`** takes the top N jobs by their best score under any profile;
  `--since DATE` takes eligible jobs first seen on/after DATE (in your timezone). Then it
  runs a scoring pass with the usual flags.
- **P3-D15 `status`** shows pending groups, batches in flight, the last run and its stop
  reason, today's calls, API spend and CLI API-equivalent cost (day boundary in your
  timezone), and the token warning (missing date, within 30 days of expiry, or expired).
- **P3-D16 `calibrate`** (new command) scores a random sample of passed groups with
  `api x1` as the reference and `cli x20/x40/x60` as candidates, writes nothing to
  `scores`, prints one table per configuration (title, company, reference score, score,
  difference, summary) plus mean/max difference, the share within ±10 and pairwise rank
  agreement, and saves a JSON report under `paths.reports`. It requires `--yes`, after
  showing what it will spend.
- **P3-D17 Dry runs need no credentials** and show an estimated API cost.
- **P3-D18 Migration `a3a84e7eab32`:** `scores.attempts`, `scores.scored_at`,
  `scoring_calls.request_map` (batch custom_id → job ids, for resuming).

### P3 Bugs found and fixed

- **P3-B1 Enqueue query** passed the profile hash as a bare string in `select()`; caught
  by the first runner test.
- **P3-B2 Schema stripping** would have deleted any *property* named `title` or `default`;
  caught in review before any field had such a name.
- **P3-B3 Dry run with `--backend api`** demanded an API key; caught on the local data.
- **P3-B4 A test pinned the old model ID** (`claude-haiku-5-5`); updated with P3-C1.

### P3 For you to review

- **P3-R1 Live checks**, once credentials are in `.env`:
  1. `claude setup-token`, then put `CLAUDE_CODE_OAUTH_TOKEN` and the date in
     `scoring.cli.token_created`; put `ANTHROPIC_API_KEY` in `.env`.
  2. `uv run --env-file .env pytest -m live tests/test_live_scoring.py`: one Pro prompt
     plus about $0.01. The cli test is the plan's "does Pro accept Haiku through the CLI"
     check: it fails, never falls back.
  3. `uv run --env-file .env job-scout calibrate --sample 50 --yes`: 50 API requests
     (about $0.35) and 6 Pro prompts.
- **P3-R2 Batch-size defaults (open decision #5):** set from the calibration results.
- **P3-R3 Two CLI details unverified** until the first live call: whether
  `--setting-sources ""` (load no settings files) is accepted, and the exact wording of
  the Pro usage-limit message (detection uses a regex over several known phrasings).
- **P3-R4 Backlog backend (open decision #6),** estimated on the 1,120 groups in the local
  DB: `cli` backlog = 28 prompts at 40 per call (two runs, within Pro's limit of roughly
  10–40 prompts per 5 hours, shared with your chats); `api` with the Batches API ≈ $3.68
  for all of them; `api` daily without batches ≈ $3.16 for 500.
- **P3-R5 CLI calls per daily run (open decision #7):** still 5 (100 postings at 20 per
  call).

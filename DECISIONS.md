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
- **P1-D3 robots.txt handling** (401/403 part superseded by P5-U1): 404 = allow all; 401/403 = disallow all; 5xx or network
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

**Acceptance:** built and tested offline, then checked live by you on 2026-10-12. Both backends,
batching, failure handling, stop reasons, backend switching and the Batches API are
covered by 55 new offline tests (213 in total; 235 after the review changes below) plus 2 opt-in live tests, using a fake `claude` executable and a fake SDK client.
You ran both live tests (passed: Pro accepts `claude-haiku-4-5` through the CLI, and the API
key works) and the calibration (P3 Calibration results). Before that, I did not use the
`claude` CLI that is logged in on this machine, because that would spend your Pro prompts
without your OK.

### P3 Your decisions

- **P3-U1 Batch size 20 per CLI call** for daily and backlog runs (open decision #5), from
  the calibration. The backlog CLI limits went from 40 postings / 120k input / 24k output
  tokens to the daily values: 20 / 60k / 12k (settings.yaml and the built-in default).
- **P3-U2 Backends (open decision #6):** `cli` during development and testing (the default,
  `scoring.backend: cli`); you switch to `api` (Batches API) yourself for the real initial
  backlog. Nothing changed in code for this.
- **P3-U3 "Place (remote)" locations stay unclear for the pre-filter (P3-R6).** No
  filters.yaml rule drops them: you'd rather have Haiku read the whole posting and drop
  them than have a regex drop an opening that could pass. All 704 such groups keep going to
  Claude.
- **P3-U4 profile.md: remote roles listed under a country.** New bullet under location
  eligibility (you approved the edit): a label such as "United States (remote)" alone is
  unclear; it fails only when the description clearly requires being located in that
  country or region ("open to candidates residing in the US"), even though the role is
  remote. Customers, offices or headquarters in a country don't count. The wording is mine,
  from your rule. This changes `profile_hash`; no scores were stored yet, so nothing is
  re-scored. Note: filters.yaml still drops a location field that is exactly
  "Remote - US"-style (its last `exclude_any` pattern) without reading the description.
  (Superseded by P3-U5: that pattern is gone.)
- **P3-U5 Location filters drop only explicit restrictions.** Your rule: exclude only when
  it's super clear the applicant must be in the place; "usa" passes to Claude, "usa only"
  is dropped. You approved editing filters.yaml. `location.exclude_any` now has 4 patterns
  (the wording and place list are mine):
  1. `<place> only`, also `US/Canada only`, `US-only`, `EU applicants only`,
     `United States residents only`;
  2. `must (be located|be based|reside|live|be a resident) in (the) <place>`;
  3. `<US|USA|United States|UK|Canada|EU> (work authorization|work permit|citizenship|
     citizens|persons|residency) (is) required`, but not after "no ";
  4. `security clearance` / `clearance required`, but not after "no ".

  `<place>` is a fixed list of countries and regions that exclude Guatemala (US, UK, EU,
  Europe, Canada, EMEA, APAC, Brazil, Mexico, …). "US" must be upper case, so "join us
  only" doesn't match. Removed: `Remote - US` / `Remote (Canada)` as a whole field, bare
  `EMEA`/`APAC`, `US-based`, and any mention of "US work authorization". "North America
  only" stays unclear: Guatemala is in North America, and profile.md calls it unclear.
  Allow patterns still win over excludes. Effect on the local DB: the old rules dropped 2
  groups ("Remote - US", a title with "EMEA"); the new ones drop none, so 1,122 pass
  instead of 1,120. The only "USA Only" posting already fails on its title, and HN posts
  skip location rules (`remote_mention_only`). The location filter is now a safety net;
  Claude does the location judging. 15 new test cases (both directions).

- **P3-U6 Default model `claude-haiku-5-5`** (your decision, from the model comparison
  below). Changed in settings.yaml and the built-in default; `effort` stays null, so 5.5
  uses its default, medium. Haiku 4.5 still works (with `effort: null`). Both live tests
  pass on 5.5 (2026-10-12), including the CLI one.

### P3 Calibration results

`data/reports/calibration-20261012T232111Z.json`: 50 passed groups, `api x1` as reference.

| Config | Calls | Scored | Time | Cost |
| --- | --- | --- | --- | --- |
| api x1 | 50 | 50/50 | 429 s | $0.23 billed |
| cli x20 | 3 | 50/50 | 536 s (~3 min a call) | $0.44 API-equivalent, not billed |
| cli x40 | 2 | 17/50 | 239 s | $0.22 API-equivalent |
| cli x60 | 1 | 0/50 | 600 s timeout | – |

- x40: the 40-posting call returned only 7 results (the first six and the last); the
  10-posting call returned all 10. x60 hit the 600 s CLI timeout.
- x20 vs api x1: mean difference 9.7, max 62, 36 of 50 within ±10, rank agreement 0.60.
  Both disqualify about 40 of 50; they disagree on 11. The biggest gaps are the
  *reference* being wrong: 1274 and 2218 (Canada / US-only listings) got 78 and 82 from
  api x1, and 178 (max C$159k, about US$115k) got 71. Haiku also contradicts itself at x1
  (1363, "United States (remote)", got 0). The disagreements come from how
  "Country (remote)" labels are judged (P3-R6), not from batching.
- The x20 CLI cost ($0.44 for 50) is about double what I'd expect for Haiku; the report
  can't show why (P3-R7).

**Model comparison** (`data/reports/calibration-20261013T014624Z.json`, 2026-10-12): 50
passed groups, current profile.md (with P3-U4) and filters (P3-U5), `api haiku-4-5 x1` as
reference.

| Config | Scored | Disqualified | Time | Cost | Tokens in / out |
| --- | --- | --- | --- | --- | --- |
| api haiku-4-5 x1 | 50/50 | 38 | 429 s | $0.236 | 291k / 29k |
| api haiku-5-5 x1 (medium) | 50/50 | 16 | 333 s | $0.043 | 398k / 60k |
| api haiku-5-5 x1 low | 50/50 | 15 | 269 s | $0.035 | 398k / 46k |
| cli haiku-4-5 x20 | 50/50 | 22 | 688 s | $0.60 API-equivalent | 162k / 75k |
| cli haiku-5-5 x20 | 20/50 (2 of 3 calls failed) | 5 of 20 | 594 s | $0.10 API-equivalent | 616k / 140k |

- Haiku 5.5 uses about 37% more input tokens (tokenizer) and 2× the output tokens
  (thinking) of 4.5, yet costs about 5.5× less (10× cheaper per token, and caching now
  works). It's also faster.
- The two models disagree a lot (mean difference 18.5, 18 of 50 within ±10), and the
  disagreement is mostly 4.5 disqualifying on location. Checked against the descriptions,
  5.5 follows profile.md and 4.5 doesn't: 4.5 disqualified 1361 (Netflix) for "US residency
  required", which the description never says; 2505 (Grafana, "candidates in the USA time
  zones") although time zones never disqualify; and 2159 and 1889 on "United States
  (remote)" labels alone, which P3-U4 makes unclear. 5.5 marked those unclear and scored
  the rest.
- 5.5 isn't perfect: it disqualified 1278 ("Europe / Asia / Africa (remote)", label only).
  At low effort it disqualified 2824 (Czechia/Hungary/Ukraine, label only), where medium
  effort said unclear.
- 4.5 is also inconsistent with itself across batch sizes: 38 disqualified at x1 vs 22 at
  x20.
- `cli haiku-5-5 x20`: 2 of its 3 calls failed and it reported 616k input tokens for 3
  calls (vs 162k for 4.5). The report can't say why: that's P3-R7.

**Effort comparison** (`data/reports/calibration-20261013T023203Z.json`, 2026-10-12): the
same 50 groups, Haiku 5.5, `api x1` at medium (the default) as reference. The `claude` CLI
reported version 2.1.296 both before and after you restarted it, but every call succeeded
this time.

| Config | Disqualified | Time | Cost | Tokens in / out | vs medium: mean diff, within ±10, rank agreement |
| --- | --- | --- | --- | --- | --- |
| api x1 medium | 15 | 352 s | $0.043 | 398k / 61k | – |
| api x1 high | 15 | 434 s | $0.054 | 398k / 84k | 4.4, 47/50, 0.91 |
| api x1 low | 14 | 270 s | $0.036 | 398k / 49k | 5.5, 46/50, 0.88 |
| cli x20 medium | 18 | 381 s | $0.057 API-equivalent | 347k / 95k | 7.8, 40/50, 0.85 |
| cli x20 high | 12 | 421 s | $0.057 API-equivalent | 172k / 102k | 6.9, 34/50, 0.86 |

- Noise baseline: the same configuration (api x1 medium) in the two runs differs by 4.6 on
  average (max 25, 44/50 within ±10). High and low effort are within that noise, so
  effort barely changes the scores. High costs 26% more and takes 23% longer; low saves 15%.
  Effort stays at the default (medium).
- The location cases are now consistent across effort levels and backends: 1361, 2505,
  2159 and 1889 are unclear and scored 40–66 everywhere (4.5 disqualified all four); 1278
  ("Europe / Asia / Africa (remote)") is disqualified everywhere.
- High effort's largest API output was 2,772 tokens, far below `thinking_headroom_tokens`
  (8,000); nothing was truncated.
- CLI x20 is a bit noisier than the API (7.8 vs 4.6) but usable.
- **Why the CLI uses so many tokens (P3-R10):** per-call detail shows each `claude -p` call
  took 2 to 5 turns (`stop_reason: tool_use`), all on claude-haiku-5-5. `--json-schema`
  works through a structured-output tool, and every extra turn resends the whole prompt
  (20 postings, about 27–35k tokens). One call used 5 turns and 237k input tokens. That
  explains the 616k in the model comparison.

- **P3-C1 Model ID: my mistake, corrected.** I claimed `claude-haiku-5-5` (PLAN.md and
  settings.yaml) was not a real model ID and switched the default to `claude-haiku-4-5`.
  It is real: Claude Haiku 5.5, released 2026-10-07, after what I knew about. You caught it;
  the Models API on your key lists it (checked 2026-10-12). The default stays
  `claude-haiku-4-5` until you decide from the comparison (P3-R9). Haiku 5.5 vs 4.5, from
  the docs: 10× cheaper per token ($0.10 / $0.50 per million input / output, for prompts up
  to 100k tokens; 5× that above), about 30% more tokens for the same text (new tokenizer),
  adaptive thinking on by default at `medium` effort (thinking tokens are billed as output
  and count toward `max_tokens`), prompt caching from 512 tokens (so caching the ~3k-token
  profile works on 5.5), and `temperature`/`top_p`/`top_k` and assistant prefill are
  rejected (we send none of them).
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
- **P3-D19 `scoring.effort`** (new, default null = not sent): passed as
  `output_config.effort` (api) and `--effort` (cli). Haiku 4.5 rejects it, so it stays null
  for 4.5.
- **P3-D20 `scoring.api.thinking_headroom_tokens: 8000`** (new): added to `max_tokens` on
  every API request, because Haiku 5.5's thinking counts toward `max_tokens` and the old
  limit (1,000 + 900 per posting) would cut it off. It only raises the cap; unused tokens
  cost nothing. Capped at 20,000 per request (the SDK refuses larger non-streaming requests).
- **P3-D21 `calibrate --configs`** (new): compares any configurations,
  `backend:model:postings_per_call[:effort]`, the first being the reference, e.g.
  `api:claude-haiku-4-5:1,api:claude-haiku-5-5:1:low`. The report now also totals input and
  output tokens per configuration (output includes thinking). Without `--configs` it
  behaves as before.
- **P3-D18 Migration `a3a84e7eab32`:** `scores.attempts`, `scores.scored_at`,
  `scoring_calls.request_map` (batch custom_id → job ids, for resuming).

### P3 Bugs found and fixed

- **P3-B1 Enqueue query** passed the profile hash as a bare string in `select()`; caught
  by the first runner test.
- **P3-B2 Schema stripping** would have deleted any *property* named `title` or `default`;
  caught in review before any field had such a name.
- **P3-B3 Dry run with `--backend api`** demanded an API key; caught on the local data.
- **P3-B4 A test pinned the old model ID** (`claude-haiku-5-5`); updated with P3-C1.
- **P3-B5 Double-escaped HTML in descriptions** (found in the calibration, P3-R8; a Phase 1
  bug). Some boards send `&lt;p&gt;…`; `html_to_text` decoded it once, leaving literal
  `<div><p><strong>` for Claude. Now, if the text left after one pass still has at least two
  different HTML tags, it gets a second pass (one mentioned tag, like "use a `<div>`", is left
  alone). Data migration `f680c1d27195` re-cleans stored descriptions, because a job that's
  already stored is never re-mapped: 13 in the local DB (10 realworkfromanywhere, 3
  remoteok), 0 left after it. 2 new tests.

### P3 For you to review

- **P3-R1 Live checks** (done by you on 2026-10-12, kept for reference):
  1. `claude setup-token`, then put `CLAUDE_CODE_OAUTH_TOKEN` and the date in
     `scoring.cli.token_created`; put `ANTHROPIC_API_KEY` in `.env`.
  2. `uv run --env-file .env pytest -m live tests/test_live_scoring.py`: one Pro prompt
     plus about $0.01. The cli test is the plan's "does Pro accept Haiku through the CLI"
     check: it fails, never falls back.
  3. `uv run --env-file .env job-scout calibrate --sample 50 --yes`: 50 API requests
     (about $0.35) and 6 Pro prompts.
- **P3-R2 Batch-size defaults (open decision #5):** decided, P3-U1.
- **P3-R3 Two CLI details unverified** until the first live call: whether
  `--setting-sources ""` (load no settings files) is accepted, and the exact wording of
  the Pro usage-limit message (detection uses a regex over several known phrasings).
- **P3-R4 Backlog backend (open decision #6):** decided, P3-U2. Estimates on the 1,120 groups in the local
  DB: `cli` backlog = 56 prompts at 20 per call (Pro allows roughly 10–40 prompts per
  5 hours, shared with your chats); `api` with the Batches API ≈ $3.68
  for all of them; `api` daily without batches ≈ $3.16 for 500.
- **P3-R5 CLI calls per daily run (open decision #7):** still 5 (100 postings at 20 per
  call).
- **P3-R6 "Country (remote)" locations** (decided, P3-U3). 4dayweek lists where a role is open as
  `<place> (remote)` entries. 704 of the 1,120 passed groups list only such places, none of
  them allowed (e.g. 294 are just "United States (remote)"). profile.md doesn't say whether
  that label alone is a fail or unclear, and Haiku flips between the two. Checked against
  descriptions: of those 704, 53 mention LATAM / anywhere / South America / "open to
  candidates", and none of the ones I read opens the role to Guatemala (most confirm
  "residing in the US", or say "customers worldwide"). Options: a location `exclude_any`
  rule in filters.yaml on the location field (drops them before Claude; "North America
  (remote)", 12 groups, would have to stay unclear per profile.md), and/or one line in
  profile.md. Both files are read-only: your call.
- **P3-R7 Calibration report detail** (done): each configuration's `calls_detail` lists, per
  call, postings sent, status, error (up to 1,500 characters for CLI errors), results
  returned, valid, rejected (with the first validation error), unknown and missing ids,
  tokens, cost, seconds, and backend extras: for the CLI, `num_turns`, `stop_reason` and
  `model_usage` (per-model tokens, which shows if the CLI ran other models); for the API,
  `stop_reason`. `calibrate` prints a line for every call that failed or came back short.
  Originally proposed as: record each call's status, results
  returned vs rejected, and the CLI's per-model usage, so a result like x40's 33 missing
  scores or the x20 cost can be explained.
- **P3-R8 Double-escaped HTML** (fixed, P3-B5): some
  sources send descriptions as `&lt;p&gt;…`, and `html_to_text` decodes them only once,
  so Claude gets literal `<div><p><strong>` tags. 10 of 39 realworkfromanywhere jobs and 3
  of 99 remoteok jobs are affected.
- **P3-R9 Default model:** decided, P3-U6. Original note: the comparison favors `claude-haiku-5-5` at the default
  (medium) effort on the API: cheaper, faster, and it follows the location rules where 4.5
  doesn't. The CLI path with 5.5 isn't usable until its failures are explained (P3-R7).
- **P3-R10 CLI turns** (open): a `claude -p --json-schema` call takes 2–5 turns, each resending
  the whole prompt, so a CLI call can use several times the tokens of a single request.
  Every call still succeeded, and the Pro plan counts usage, not dollars, but it eats into
  the 5-hour limit faster. This CLI version has no `--max-turns`. If the Pro limit becomes
  a problem, options are smaller batches or the API backend.

---

## Phase 4: Backlog report and digest (branch `phase-4-digest`)

**Acceptance (2026-10-12, small CLI test you chose):** `backlog --max-calls 3` scored 30
groups and wrote a backlog report (6 unclear location, 24 below threshold). Then two
`run`s (5 CLI calls each): the first digest had 2 top + 21 unclear, all newly scored; the
second had 2 top + 1 unclear, only jobs scored in that run. No job id appears in two
reports. 13 Pro prompts in total; 1,380 groups still pending for the real backlog on the API.
Email is built but off until your alias and token exist (P4-R4). 258 offline tests.

### P4 Your decisions

- **P4-U1 Digest delivery (open decision #1): email, plus the HTML file on the home
  server.** How email is sent is still to be agreed (P4-R1).
- **P4-U2 Score threshold (#2): 50 for now,** to test. `digest.min_score: 50` in
  settings.yaml (PLAN.md's default was 60).
- **P4-U3 Daily run time (#4): 08:00 Guatemala time.** `schedule.daily_run_time: "08:00"`.
- **P4-U4 CLI calls per daily run (#7): 5,** the default. For the real runs you'll likely
  use the API backend too.
- **P4-U5 Title filter (P2-R1):** don't drop titles whose role or level is ambiguous
  ("Forward Deployed Engineer", "Staff Engineer"), and always keep "Senior … Engineer".
  filters.yaml (you approved the edit) now includes any title with "engineer(s)" or
  "developer(s)"; the exclude list still drops clear non-fits (sales/solutions/support
  engineer, QA, junior/intern, mechanical/electrical, hardware, recruiter/designer/
  marketing). I added `asic|fpga|rtl|physical design` to the excludes (chip design, never
  this profile). "Engineering Manager" still doesn't match (the word is "engineering"),
  which fits profile.md ("not of interest"). Note that an exclude still wins over
  "senior": "Senior Solutions Engineer" is dropped by your existing solutions-engineer
  rule. Effect on the local DB: 1,529 groups pass instead of 1,122 (+407: ML/AI
  engineers, forward deployed, staff/principal, frontend, iOS, security, …; Claude scores
  frontend-only and mobile-only in the 21–49 range per profile.md). 8 new test cases.
- **P4-U6 Crypto/Web3 boards (#3): stay skipped.** Crypto companies that post on the
  general boards (Coinbase etc.) keep coming through as before; nothing filters them.

- **P4-U7 Email via Proton SMTP submission from a dedicated alias** (decides P4-R1): you're
  creating an alias on your domain in Proton and a send-only SMTP token for it. The digest
  goes from the alias to your main address via `smtp.protonmail.ch:587` (STARTTLS);
  the token is read only from `JOB_SCOUT_SMTP_TOKEN`. Proton has no official sending API,
  and Bridge needs a desktop app, so SMTP submission it is. A token is send-only, works for
  its one address, and can be revoked on its own.
- **P4-U8 `job-scout export`:** a single command that writes everything to CSV for
  skimming, without filters: one row per posting with its pre-filter verdict and current
  score.
- **P4-U9 Backlog report gets a collapsed "Below threshold" section** (score and one-line
  summary of what Claude scored under `digest.min_score`).

- **P4-U10 Email on; the recipient comes from the environment.** You enabled email with
  `from_address: job-scout@gadhi.one` in settings.yaml (test email received 2026-10-12).
  `to_address` stays null there and is set by `JOB_SCOUT_DIGEST__EMAIL__TO_ADDRESS` (in
  `.env` locally, the server's env file later), so recipients change without editing
  config. Several addresses work, comma-separated. Because the recipient may now be absent
  at load time, the config check only requires `from_address` when email is enabled; a
  missing recipient is an email failure at send time (like a missing token): the digest
  isn't recorded and carries over. Offline tests switch email off themselves, whatever
  settings.yaml says.

### P4 Decisions (Claude)

- **P4-D1 HTML with Jinja2** (new dependency, `jinja2>=3.1`): one template
  (`report/templates/report.html`) for the digest and the backlog report, plus a plain-text
  twin for the email's text part. No scripts or external assets, so the file works as an
  email body; collapsed sections use `<details>` (clients without support show them open).
- **P4-D2 Sections** (PLAN.md §8), each job in one section, best score first (ties: newest
  posting): *Watchlist* (any listing's company matches a watchlist company by normalized
  name, not disqualified, score ≥ `watchlist_min_score`); *Top matches* (score ≥
  `min_score`, location pass); *Unclear location* (score ≥ `min_score`, location
  unclear; collapsed); *Below threshold* (the rest, including disqualified; backlog report
  only, collapsed). Watchlist matching is by company name until Phase 5 adds the ATS feeds.
- **P4-D3 What counts as reported:** each delivered digest or backlog report stores its
  canonical job ids in `digests` (new columns `kind`, `report_path`, `emailed`; migration
  `1a82f9c690ad`). A digest leaves out every job any earlier digest *or backlog report*
  showed, so after the backlog run the daily digest shows only new matches. The
  below-threshold list is never recorded. A digest whose email failed isn't recorded, so
  its jobs carry over to the next one rather than being lost.
- **P4-D4 Empty digests are still emailed** by default (`digest.send_when_empty: true`):
  the footer shows source and scoring health, so silence never hides a broken pipeline.
- **P4-D5 Email settings:** `digest.email` (enabled, smtp_host `smtp.protonmail.ch`, port
  587, from_address = SMTP username, to_address) replaces `digest.channel`. STARTTLS with
  Python's default certificate checks; text + HTML parts. The token is read from
  `JOB_SCOUT_SMTP_TOKEN` only when sending, never stored in settings, and never in errors.
- **P4-D6 `run`** = fetch (enabled sources) → filter → score (daily mode, configured
  backend) → digest. Every stage runs even if an earlier one failed, so a digest still goes
  out; the exit code is 1 if any stage failed (all fetches failed, filter or scoring error,
  missing credentials, email failure) so Phase 6's health check notices. Hitting the Pro
  usage limit is not a failure.
- **P4-D7 `backlog`** = fetch → filter → score (backlog mode, all scoring flags) → ranked
  report of *everything* scored so far under the current profile (cumulative across
  re-runs), which also records its reported jobs. `--dry-run` only plans the scoring: no
  fetch, no report.
- **P4-D8 Report files** under `paths.reports`: `digest-<UTC time>.html` and
  `backlog-<UTC time>.html`, each also copied to `digest-latest.html` /
  `backlog-latest.html`. `digest --dry-run` writes `digest-preview.html` only (no email, no
  database writes).
- **P4-D9 `export`** (P4-U8) writes `jobs-<UTC time>.csv` to `paths.exports` (new setting;
  `--out` overrides): one row per stored posting, duplicates included and linked by
  `group_id`, with the group's pre-filter verdict and current-profile score (status, score,
  disqualified and reason, location eligibility, summary, backend, model). `--sqlite` adds
  `job-scout-<UTC time>.db` via SQLite's online backup API, opened read-only, so it's
  consistent while the pipeline writes. Postgres is refused for `--sqlite`.
- **P4-D10 Untrusted posting text in reports:** autoescape on; every link goes through an
  `http_url` filter that only allows http(s) (`javascript:` becomes `#`); CSV cells starting
  with `= + - @` get a leading `'` so a spreadsheet won't run them as formulas.
- **P4-D11 Footer:** enabled sources with last fetch time, item count and a problem flag
  (never fetched, failed N runs in a row, 0 items 3+ runs in a row), plus pending groups,
  batches in flight, the last scoring run and the token warning.
- **P4-D12 Live email test** (`tests/test_live_email.py`, `-m live`): sends one message;
  skipped until the token and addresses are set.

- **P4-D13 4dayweek incremental window: at least 7 days** (was 2). Their OpenAPI spec says
  `posted_after` means "posted within the last N days". A run on Monday night got 0 new
  postings (nothing dated after Saturday), which is correct, but a job that appears on the
  board with a posting date several days old would fall outside a 2-day window. The
  overlap costs about 4 extra requests and comes back as "seen". sources.yaml note updated.
- **P4-D14 The `claude` subprocess gets `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`:** no
  auto-update (PLAN.md Phase 6 pins the CLI version), no telemetry or error reporting. The
  sandbox had blocked both (`downloads.claude.ai`, a Datadog intake host) during the runs.
- **P4-D15 The source-health footer** skips sources that aren't built yet (remoteintech,
  Phase 5) and shows "skipped: FINDWORK_API_KEY not set" instead of "never fetched".

### P4 Bugs found and fixed

- **P4-B1 Dry runs wrote to the database** (Phase 3 bug): `score --dry-run` (help: "no
  calls, no writes") committed the pending score rows it planned. Now it plans inside a
  transaction that's rolled back. Your local DB still holds 2,242 such pending rows from
  earlier dry runs (1,120 under the old profile, 1,122 under the current one); they're
  harmless, since a real run creates the same rows and old-profile rows are never used.

- **P4-B2 CLI errors said only "exit code 1":** when the CLI's JSON has an empty `result`,
  the failure reason is in `subtype`. Errors now include it, e.g.
  `error_max_structured_output_retries (exit code 1)`.
- **P4-B3 The backlog report's empty Top section said "Nothing new"**; it now says "No
  matches" (only the digest is about what's new).

- **P4-B4 The live email test never saw the token:** `tests/conftest.py` clears every
  `JOB_SCOUT_*` variable so your shell's settings can't leak into tests, and that included
  `JOB_SCOUT_SMTP_TOKEN` (found when you ran the test). Secrets that aren't settings are now
  kept (`KEEP_ENV`); offline tests pass their own environment.
- **P4-B5 Secrets in pytest tracebacks:** that failure printed the `environ` argument, with
  your `ANTHROPIC_API_KEY` truncated to `sk-...` but partly visible. The mailer no longer
  takes the environment: `read_token()` wraps the token in Pydantic's `SecretStr`, which
  prints as `**********` in any repr, log line or traceback.

### Answers to the Phase 1–3 review items

- **P1-U3** User-Agent interpretation confirmed: keep `job-scout/0.1 (personal job
  search)` without an email.
- **P1-R1** Findwork: you'll create the account and set `FINDWORK_API_KEY` later (I'll
  remind you).
- **P1-R2** 4dayweek per-country copies: fine as is.
- **P1-R3** Low-yield sources stay: after the first run, dedupe means only new postings
  reach Claude; remove them later if they prove useless.
- **P2-R2** (non-USD salaries left to Claude) and **P2-R4** (HN remote-mention mode):
  fine as is.
- **P2-R5** Spot check: you'll do it.
- **P3-R3** (usage-limit wording), **P3-R10** (CLI turns), the HN title/location merge,
  and the dry-run estimate for Haiku 5.5: accepted as is.

### P4 For you to review

- **P4-R1 How email is sent:** decided, P4-U7.
- **P4-R3 Live acceptance:** done with the small CLI test (see the top of this section). The
  real backlog (about 1,380 groups left, est. $0.45–0.60 on the API with batches) is yours
  to start: `job-scout backlog --backend api`.
- **P4-R6 CLI structured-output failures:** 2 of 13 CLI calls today ended with
  `error_max_structured_output_retries` (the CLI gave up producing valid JSON for 20
  postings). The split-and-retry recovered every posting. If it gets frequent, smaller
  CLI batches (`--postings-per-call 10`) are the lever; the API backend doesn't have it.
- **P4-R4 Email:** my sandbox can't reach `smtp.protonmail.ch:587` (DNS fails: only proxied
  HTTP(S) gets out), so the live email test must run on your side. Once the alias and its
  token exist, set `digest.email.from_address`,
  `to_address` and `enabled: true` (or the matching `JOB_SCOUT_DIGEST__EMAIL__*` env vars),
  put the token in `.env` as `JOB_SCOUT_SMTP_TOKEN`, then run
  `uv run --env-file .env pytest -m live tests/test_live_email.py` (one email).
- **P4-R5 Look of the report:** checked by tests, not by eye; please skim the first real
  `backlog-latest.html` and tell me what to change.

---

## Phase 5: ATS seed and watchlist (branch `phase-5-ats`)

**Acceptance (2026-10-13, partly live):** offline, `ats detect` (seed download, filters,
watchlist, all three detection steps, counts per ATS, unknown list) and board polling are
covered with synthetic fixtures (301 tests; 41 new, with P5-U1). Live: the real seed downloaded (884
companies, 402 kept), and detection ran for the first 40 alphabetically: 10 boards found
(Greenhouse 3, Lever 3, Workable 2, Ashby 2) plus Canonical from the watchlist, 30 unknown.
Polling fetched **Canonical's 311 jobs via Greenhouse** and 173 from five other companies
(453 new groups, 141 past the pre-filter). The other 361 companies need the full run on your
machine (P5-R2): my sandbox only reaches hosts listed in advance, and it blocked every redirect
to an unlisted host, so its hit rate understates the real one.

### P5 Your decisions

- **P5-U1 robots.txt follows RFC 9309** (decides P5-R1): a robots.txt answering any 4xx,
  401 and 403 included, means "unavailable", so everything on that host is allowed (Google
  does the same); 5xx and network errors still fail the request (the standard's "assume
  disallow"). Real `Disallow` rules are respected as before. This replaces Python's
  `urllib.robotparser` rule (401/403 = disallow all) from P1-D3. Effect: Ashby's posting API
  is polled now; re-polling on 2026-10-13 fetched AirGarage (18) and Andela (15) with no
  board errors.
- **P5-U2 You run the full `ats detect`** on your machine (decides P5-R2).
- **P5-U3 Company-board postings all go to Claude** (decides P5-R3), office jobs at
  remote-first companies included, so nothing is missed; about $1 extra on the backlog is
  acceptable. No pre-filter change.

### P5 Config fixes

- **P5-C1 remoteintech endpoint:** was the instruction "git clone …". Now GitHub's REST API
  tarball (`https://api.github.com/repos/remoteintech/remote-jobs/tarball`): one request,
  no git binary in the image. github.com's robots.txt disallows `/*/archive/` for bots, so
  not the website's archive link; the API is GitHub's documented programmatic route.
- **P5-C2 Seed filters are fields now:** `seed_regions` and `seed_remote_policies` on the
  source (they were prose in its notes); same values as before.
- **P5-C3 SmartRecruiters removed from `ats_platforms`:** `api.smartrecruiters.com`'s
  robots.txt is `User-agent: * / Disallow: /`. Its companies become unknown_ats (1 in the
  filtered seed).
- **P5-C4 Workable:** the API template gets `?details=true` (without it the widget API
  returns no descriptions), and `detect` also matches `<company>.workable.com`, the older
  board address 3 seed companies still use.
- **P5-C5 New settings `ats`:** `seed_refresh_days: 7`, `redetect_days: 30`,
  `max_detections_per_run: 40`.

### P5 Decisions (Claude)

- **P5-D1 Package `ats/`** (seed, platforms, detect, store, refresh, fetcher). Postings
  from company boards are stored under the `remoteintech` source (watchlist boards too), with
  `source_job_id` = `<ats>:<board token>:<job id>` and an `ats: <name>` tag.
- **P5-D2 Detection, in sources.yaml's order:** (1) the careers URL is a board (its host
  matches `detect`, the token comes from the URL); (2) fetch the careers page (or, with no
  careers URL, the website and its first same-site careers link) and look for board links or
  embeds; (3) probe the seed slug and the website's domain label on each platform's API:
  a match needs HTTP 200 with at least one job. A 404 means "not there"; a platform whose
  robots.txt disallows us is skipped for the rest of the run. Tokens found in pages aren't
  verified; polling records an error if one is wrong.
- **P5-D3 `ats_companies`** keyed by slug (migration `b4ca7cd8f705`; the table had never held
  rows). Statuses: pending, ok, unknown_ats, not_in_seed. It records how each board was
  found (`method`), when it was last detected, seen in the seed, and polled, plus the last
  poll error. A seed company whose careers URL or website changes is detected again; one
  that leaves the seed (or its filters) stops being polled. A watchlist entry (sources.yaml)
  overrides detection; removing it hands the company back to detection.
- **P5-D4 Upkeep:** `ats detect` downloads the seed, applies the watchlist and detects every
  new or stale company (`--refresh`: all; `--limit N`; `--no-seed`). Each detection is
  committed on its own, so it's safe to interrupt and resume. `run` and `backlog` start with
  an `ats` stage: download the seed when it's older than 7 days and detect at most 40
  companies, so the daily run stays short and re-detection spreads over runs.
- **P5-D5 Polling:** the `remoteintech` fetcher polls every board with status ok. One failing
  board is recorded (`last_poll_error`) and skipped; the source fails only if all fail; the
  fetch note says "N of M company boards failed". With no boards yet the source is skipped
  ("run `job-scout ats detect`").
- **P5-D6 Mappers** for Greenhouse, Lever, Ashby, Workable and Recruitee: location from all
  listed locations, the job's own workplace field as `remote_raw` (Lever, Ashby, Recruitee,
  Workable's telecommuting flag; Greenhouse has none), structured salary where the ATS has
  one (Lever `salaryRange`, Ashby compensation components, Recruitee `salary`), otherwise a
  labelled salary line in the text. Unlisted Ashby jobs and unpublished Recruitee offers are
  skipped. Large HTML fields are left out of `raw` (the text is in `description_text`).
  Lever and Ashby salary fields were written from their documented shape; the live samples
  had none filled in.
- **P5-D7 Unknown list:** `ats detect` prints the unknown companies and writes
  `reports/ats-unknown-<time>.csv` (company, careers URL, website, region, why nothing was
  found).
- **P5-D8 Seed URLs are cleaned:** a pasted Markdown link (`…/career](https://…)`), several
  URLs in one field, or trailing punctuation (`…/careers/,`) were in the live seed.
- **P5-D9 Forbidden hosts, enforced in the HTTP client:** LinkedIn, Indeed,
  Wellfound/angel.co and Google are refused before any request, robots.txt included. The
  live seed lists careers pages on LinkedIn and angel.co; PLAN.md's ground rules forbid
  them. Those companies fall through to probing.
- **P5-D10 Offline tests can't reach the network:** an autouse fixture makes any real HTTP
  request fail (MockTransport fixtures are unaffected; `-m live` is exempt). It caught the
  new `run` stage trying to download the seed from inside a test.
- **P5-D11 `sources check`** shows the company boards: polled per ATS, pending, unknown.
- **P5-D12 The "unbuilt command" helper is gone:** every command is built.

### P5 Bugs found and fixed

- **P5-B1 None tags rejected a posting:** a Lever job with no department or team mapped
  `None` into `tags`, so the posting failed validation. All ATS mappers drop empty tags.
  Found by the synthetic fixtures.
- **P5-B2 Escaped HTML with a single tag type stayed escaped** (Phase 4's check needed two
  different tags): `&lt;p&gt;…&lt;/p&gt;` alone wasn't re-parsed, so a salary line went
  unnoticed. The check now also accepts an opening plus a closing tag, and the Greenhouse
  mapper unescapes explicitly (its API always escapes `content`).
- **P5-B3 `run` reached the network from tests** (the new seed upkeep): stubbed in the
  pipeline tests, and the network guard (P5-D10) now catches any repeat.

### P5 For you to review

- **P5-R1 Ashby and robots.txt** (decided, P5-U1): `api.ashbyhq.com/robots.txt` answers
  HTTP 401. Our client follows Python's `urllib.robotparser` rule and treats 401/403 as
  "disallow everything", so Ashby boards are detected but never polled (2 so far:
  AirGarage, Andela). The robots.txt standard (RFC 9309, section 2.3.1.3) treats any 4xx,
  401/403 included, as "unavailable": crawlers may then access everything, which is also
  how Google behaves. Ashby's posting API is a public API meant for job boards. Options:
  follow RFC 9309 for all hosts (401/403 = no robots.txt), or keep the strict rule.
- **P5-R2 Run the full detection on your machine** (P5-U2, yours): `uv run --env-file .env job-scout ats
  detect` (361 companies left; it fetches roughly 400 company sites and probes the ATS
  APIs, about 15–30 minutes at one request per second per host; safe to interrupt and
  re-run). Then skim `data/reports/ats-unknown-*.csv`.
- **P5-R3 Volume and office jobs** (decided, P5-U3: keep all): company boards list every role, including office jobs
  at "remote-first" companies (Asana: Warsaw, Reykjavík), and Greenhouse has no
  remote/on-site field, so those pass the pre-filter and Claude rejects them. The first 11
  boards added 141 groups to score. Full detection may bring roughly 1,000–1,500 more for the
  first backlog (about $1 on the API with Haiku 5.5), then a trickle daily. If that's too
  much: keep only `fully-remote` seed companies, or require a remote mention in the title or
  location for company-board postings.
- **P5-R4 Redirects skip robots.txt checks:** the HTTP client follows redirects without
  checking the target host's robots.txt (e.g. a careers page that redirects to another
  domain). It mattered little for job boards; with ~400 company sites it's worth fixing if
  you want strictness.

---

## Phase 6: Deploy on the home server (branch `phase-6-deploy`)

**Acceptance:** built and checked here as far as the sandbox allows; the server steps are
yours (P6-R1). Here: `hadolint` and `shellcheck` pass, `docker compose config` validates, the
timer schedule resolves to 08:15 America/Guatemala (`systemd-analyze calendar`), and the
image's Python layer was simulated from the built wheel in a fresh virtualenv (migrations
from `/app/alembic.ini`, templates, `status` and `digest --dry-run` with config from
`JOB_SCOUT_CONFIG_DIR`). Not possible here: building the image (no access to the Docker
daemon) and `systemd-analyze verify` (read-only filesystem). 308 offline tests (7 new).

### P6 Your decisions

- **P6-U1 No external monitor** (replaces PLAN.md's healthchecks.io dead man's switch): the
  server is also your DNS and DHCP, so you notice at once if it's down. The daily digest
  (sent even when empty) is the "it ran" signal, and a failed or hung run emails its log
  (P6-D4). Acceptance becomes "a deliberately failed run sends the failure email".
- **P6-U2 Daily run at 08:15 Guatemala time** (was 08:00): the server boots at 08:00.
  `Persistent=true` runs a missed run right after a late boot.

### P6 Decisions (Claude)

- **P6-D1 One image** (`Dockerfile`, multi-stage): Python 3.12 slim (Debian bookworm) with the
  locked dependencies (`uv sync --frozen --no-dev`, project installed non-editable), plus the
  `claude` CLI pinned to 2.1.296. That npm package ships a native glibc binary per platform,
  so only the binary is copied into the final image; Node isn't needed at runtime. Runs as a
  non-root user (UID/GID 1000 by default, build args); `HOME` is in `/var/lib/job-scout/home`.
  `.dockerignore` keeps `.env`, `data/`, databases and `config/` out of the image.
- **P6-D2 Config and data are mounted** (`deploy/compose.yaml`): `config/` read-only at
  `/app/config` (`JOB_SCOUT_CONFIG_DIR`), `/var/lib/job-scout` read-write (the paths
  settings.yaml already uses). Secrets come from `/etc/job-scout/job-scout.env` (mode 600) via
  `env_file`; the API key still never reaches the `claude` subprocess (its env allowlist).
- **P6-D3 Migrations run automatically:** the entrypoint runs `alembic upgrade head` before
  every command, except `notify-failure`, which must work even when a migration is what
  failed.
- **P6-D4 Failure email:** `job-scout.service` has `OnFailure=job-scout-failure.service`, which
  pipes that run's journal (by `MONITOR_INVOCATION_ID`) into the new `job-scout
  notify-failure` command. That command emails the result, exit status and last 120 log
  lines through the digest's Proton settings, and masks anything shaped like an Anthropic
  key or token, or `NAME=value` for our secret variables. If it can't send, it prints the
  report to the journal and exits 1.
- **P6-D5 Hangs become failures:** `TimeoutStartSec=2h` kills a stuck run, which triggers the
  failure email with result `timeout`.
- **P6-D6 The deploy files are tested** (`tests/test_deploy.py`): timer vs `daily_run_time`
  and timezone, OnFailure and timeout present, compose mounts vs the settings paths, every
  secret the code reads listed in the env template, secrets in `.dockerignore`.
- **P6-D7 Offline tests can't open SMTP connections either** (the network guard from P5-D10
  now also blocks `smtplib.SMTP.connect`).
- **P6-D8 The failure-path check doesn't break anything:** the README uses a throwaway
  `systemd-run` unit that exits 1 with `OnFailure=job-scout-failure.service`, so the real
  failure email is exercised without touching config or credentials. (Breaking the config to
  force a failure would also break `notify-failure`, which loads the same config.)
- **P6-D9 Markdown tables** in `DECISIONS.md` now use `| --- |` separators and inline HTML is
  quoted, to pass your markdownlint config (MD060, MD033).

### P6 Bugs found and fixed

- **P6-B1 Real URLs in a test** (found in a fixture audit you asked for): Phase 5's
  `test_seed_urls_are_cleaned` used two real companies' careers URLs, copied from malformed
  entries in the live seed. They're now `acme.example` / `acme.notion.example` with the same
  malformed shapes. Audited the rest: every file in `tests/fixtures/http/` (the remoteintech
  tarball included) is built by `make_http_fixtures.py` from invented data and regenerates
  byte for byte; Phase 6 added no fixture files, and its tests use inline synthetic logs and
  `example.test` addresses.

### P6 For you to review

- **P6-R1 On the server** (`deploy/README.md`): clone to `/opt/job-scout`, create
  `/var/lib/job-scout` (owned by 1000:1000) and `/etc/job-scout/job-scout.env`, set
  `scoring.cli.token_created`, optionally copy your local database snapshot to keep history,
  `docker compose build`, run `ats detect`, install and enable the timer, then the two checks:
  a real run (`systemctl start job-scout.service`) and the failure email (`systemd-run …`).
- **P6-R2 A git remote** is still needed to get the code onto the server (or copy it).
- **P6-R3 CPU architecture:** the image builds for the server's own architecture (npm picks
  the matching `claude` binary). Building elsewhere for another architecture would need
  `docker buildx --platform`.

---

## Phase 8: Feedback loop (branch `phase-8-feedback`)

**Acceptance:** built and tested offline. End to end on a throwaway copy of the local
database: a signed link opened the page (200, Claude's score shown, 👎 preselected), an
unsigned one got 403, Save stored the vote (303 back to the page, "Saved"), and
`feedback report` listed it as a disagreement. Not yet seen: a real digest with links, the
page on the server, and a scoring run with your judgments in the prompt (P8-R1, P8-R2).
322 offline tests (14 new). Phase 7 (Tier 2 scrapers) is skipped for now (P8-U1).

### P8 Your decisions

- **P8-U1 Phase 7 skipped for now**, straight to the feedback loop. `phase-8-feedback`
  starts from `phase-6-deploy`; there's no `phase-7-*` branch yet.
- **P8-U2 Feedback through a page on the home server**, linked from each digest item.
  LAN only; to reach it from outside later, a VPN into your network (Tailscale or
  WireGuard) and a different `feedback.base_url`. Proton VPN can't do it: it has no way
  in to your LAN, and its port forwarding gives random ports on P2P servers only.
- **P8-U3 No `job-scout feedback <id> up|down` command:** you'd not use it.
- **P8-U4 A `feedback report` command:** how often Claude agreed with your votes and the
  disagreements with your reasons, to spot what belongs in `profile.md`.

### P8 Decisions (Claude)

- **P8-D1 Python's built-in HTTP server**, one request at a time (`feedback/server.py`):
  no new dependency for a one-person LAN page. A 15-second socket timeout stops a stalled
  client from holding it; request bodies are capped at 16 KB.
- **P8-D2 Signed links, and opening one saves nothing.** A link is
  `<base_url>/f/<job_id>?v=up|down&s=<signature>`: HMAC-SHA256 of the job id with
  `JOB_SCOUT_FEEDBACK_SECRET` (env only, 32+ characters), so editing the number gives a
  403. Opening it shows the job, Claude's score and summary, your vote preselected and a
  reason box; only Save (a POST carrying the same signature) writes, because mail apps
  sometimes open links on their own. Save redirects back to the page (Post/Redirect/Get).
  Responses forbid scripts, framing and external assets (Content-Security-Policy), aren't
  cached, and send no referrer; the request log leaves the signature out.
- **P8-D3 One `feedback` row per job group** (canonical job), the latest vote wins. The row
  copies the score you reacted to (score, disqualified, summary, profile hash), so the
  judgment stays readable after a rescore or a profile change. A reason is capped at 1,000
  characters. Votes on jobs never scored are kept, with no score.
- **P8-D4 Judgments go into the system prompt** after the profile and the posting rules, as
  a "recent judgments" section of `<judgment>` elements: title and company, the earlier
  score and summary, your vote, and your reason. The instructions say they calibrate how
  the rubric is applied, the profile's hard requirements still decide, and a posting isn't
  scored up or down just for resembling an example. Titles, companies and summaries are
  marked as untrusted posting data, can't open or close a `<judgment>` or `<posting>`
  element, and are flattened to one line, so posting text can't forge a reason line.
- **P8-D5 Which judgments:** up to `feedback.prompt_examples` (8) from the latest 200.
  Disagreements first (👎 on a job that would have been in the digest, 👍 on one that
  wouldn't, judged against `digest.min_score`), newest first; at most half of each vote
  while the other vote has enough; listed oldest first. The same set for every call in a
  run, so the API's prompt caching still works. About 100 tokens per judgment.
- **P8-D6 Judgments don't change `profile_hash`:** a new vote doesn't make existing scores
  stale or trigger re-scoring. Each `scoring_calls` row records which jobs' judgments were
  in its prompt (`feedback_job_ids`). `rescore --top N` applies new feedback to older jobs
  when you choose. `calibrate` sends the same judgments, so it measures the real prompt.
- **P8-D7 Links on every item** of the digest and the backlog report, in the HTML and the
  plain-text part, including the below-threshold list: a 👍 there is the most useful
  signal. No links while `feedback.base_url` or the secret is unset; the digest still goes.
- **P8-D8 Deployment:** a second compose service, `feedback`, from the same image
  (`command: feedback serve`, `restart: unless-stopped`, so it comes back with Docker at
  boot; `pull_policy: never`). It listens on 0.0.0.0:8765 in the container and publishes
  8765 on the server. The setting's default host is 127.0.0.1, so running it on a laptop
  doesn't expose it. `JOB_SCOUT_FEEDBACK__BASE_URL` goes in the server's env file (it's
  that machine's address), not in `settings.yaml`.
- **P8-D9 `feedback report`** counts the 2×2 of your vote against "Claude put it in the
  digest", the agreement rate, mean scores per vote, how many votes reacted to an older
  profile, and lists the disagreements with your reasons (`--all` lists every vote,
  `--since` limits the period). `explain ID` shows your vote too.

### P8 Bugs found and fixed

- **P8-B1 Rate-limited board checks marked companies "unknown" for 30 days** (Phase 5 code,
  found when you asked about the unknown-ATS list). In the detection run here, 125 of the
  272 unknown companies had only hit Workable's rate limit (HTTP 429) during the slug
  probe, and about 40 more were Recruitee probes blocked by the sandbox's proxy. Fixes:
  - `HttpClient` waits and retries on HTTP 429, up to twice: as long as `Retry-After` says
    (if 60 seconds or less), else 5 then 10 seconds. That host then gets more spacing
    between requests (2 seconds, doubling per 429 up to 10) for the rest of the run.
  - Errors carry `retryable`: 429, 5xx (robots.txt included) and timeouts. Connection and
    DNS failures aren't retryable: they usually mean the site is gone.
  - Detection that finds nothing *and* hit a retryable error is inconclusive: the company
    stays as it was and is detected again next run (`ats detect` or `run`'s capped upkeep),
    counted as "retry later". After 3 inconclusive tries in a row it becomes unknown,
    with the note saying so; a board that's already polled is kept instead (new column
    `ats_companies.detection_retries`, migration `ddc39a2e53b6`).
  - Companies already marked unknown by a rate limit aren't reset automatically: run
    `job-scout ats detect --refresh` once to re-detect every company (P8-R4).

### P8 For you to review

- **P8-R1 On the server:** generate `JOB_SCOUT_FEEDBACK_SECRET` (`openssl rand -hex 32`),
  give the server a name in your DNS (e.g. `jobscout.lan`), set
  `JOB_SCOUT_FEEDBACK__BASE_URL` in `/etc/job-scout/job-scout.env`, then
  `docker compose build` and `docker compose up -d feedback` (`deploy/README.md`, "Feedback
  page"). On your phone, Proton VPN's "Allow LAN connections" must be on at home.
- **P8-R2 Look at the page** on your phone the first time and tell me if anything's off
  (layout, wording, the 👍/👎 labels).
- **P8-R4 Re-detect with the fix:** your local database has the sandbox run's results
  (including the rate-limited and proxy-blocked ones). For the full detection on your
  machine or the server, use `job-scout ats detect --refresh`, which re-detects every
  company. A fresh server database needs only `ats detect`.
- **P8-R3 Number of examples:** 8 to start. More examples cost about 100 tokens each per
  call; fewer may teach too little. Revisit once `feedback report` has a few dozen votes.

---

## Phase 9: Questions and refactors (branch `phase-9-refactor`)

**Scope:** your questions about the code and the refactors that come out of them. No new
features planned; Phase 7 (Tier 2 scrapers) and the Findwork key are still open.

### P9 Your decisions

- **P9-U1 Phase 9 is for code questions and refactors**, on its own branch from
  `phase-8-feedback`.
- **P9-U2 Keep the `src/job_scout/` layout.** Moving the subpackages directly into `src/`
  would make `config`, `db`, `log` and the rest top-level packages (name collisions in
  site-packages, imports without the `job_scout.` anchor). The flat layout (`job_scout/`
  at the repo root) was the alternative; not needed.
- **P9-U3 Split the 1,200-line `cli.py`** so each command is easy to find. Behaviour,
  flags, help text and exit codes unchanged.

### P9 Decisions (Claude)

- **P9-D1 `cli/` package, one module per area.** `sources` (fetch, sources check), `jobs`
  (filter, explain, export), `scoring` (score, rescore, status, calibrate), `digest`
  (digest, notify-failure), `pipeline` (run, backlog), `ats` (ats detect, the weekly
  upkeep), `feedback` (serve, report), `common` (consoles, config and DB loading, shared
  options). Commands are plain functions; `__init__.py` registers them in the old order,
  so it reads as a table of contents and `--help` is byte-identical (checked by diffing
  all 20 help screens before and after). The entry point `job_scout.cli:app` is
  unchanged. Helpers used across modules lost their leading underscore; three tests now
  patch helpers in the module that defines them.
- **P9-D2 Small cleanups that came with the split:** `fail()` replaces about ten copies
  of "print a red error, raise `typer.Exit`"; `exit_if_all_failed()` and `send_digest()`
  remove the code `run` duplicated from `fetch` and `digest`; `run`'s inner ATS upkeep
  is now `cli.ats.upkeep()`; `run_scoring()`'s batching flags default to None; in
  `notify-failure` the journal text was named `log`, shadowing the module logger, and is
  now `journal`. One visible difference: errors raised through `fail()` are now always
  escaped and soft-wrapped (the unknown-source-id and `--record` messages weren't), which
  only matters for text containing Rich markup or wider than the terminal.
- **P9-U4 No domain modules at the package root.** Only something general like `log.py`
  stays there.
- **P9-D3 `prefilter.py` → `prefilter/rules.py`, `prefilter_run.py` →
  `prefilter/runner.py`** (the same split as `scoring/`: pure pieces next to `runner.py`).
  **`groups.py` → `db/groups.py`:** the pre-filter, scoring, the reports, the feedback
  page and `explain` all load groups, so it sits next to the models it queries rather than
  in one stage's package. Imports only; no logic changed. The package root now holds
  `__init__.py`, `log.py` and the subpackages.
- **P9-U5 Consistency pass.** You asked why fetched postings use Pydantic but ATS boards
  don't, agreed to make the seed a Pydantic model, and asked for an audit of the whole
  project for inconsistencies of that kind. (ATS board postings do use Pydantic: every
  ATS mapper builds the same `Posting`. `Board` is a dataclass because our code builds it
  from our own rows.) The rule is now written in CLAUDE.md: Pydantic for data from
  outside, dataclasses for values our code builds, SQLAlchemy for storage.
- **P9-D4 The remoteintech seed is a Pydantic model.** `SeedCompany` (frozen,
  `extra="forbid"`) validates each company file's frontmatter. A file with the wrong type
  in a field (a list where a URL belongs) is logged and skipped like malformed YAML,
  instead of being turned into a string. Checked against the real seed downloaded to a
  scratch directory: the old and new parsers return the same 884 companies, field for
  field, with no warnings.
- **P9-D5 The claude CLI's JSON reply is validated** (`_CliReply` in
  `scoring/backends/cli.py`; every field optional, unknown fields ignored because the CLI
  adds some between releases). See P9-B1.
- **P9-D6 One definition per set of allowed values.** Settings use the `Backend` and
  `ScoringMode` enums instead of their own Literals, which had the same name as the enum.
  That removes the `Backend(scoring.backend)` and `.value` conversions. `Posting.salary_period`
  is a `SalaryPeriod`, which removes two workarounds: the pre-filter's `_period_value()` ("the
  ORM hands back an enum; Postings carry plain strings") and the conversion in
  `fetch/store.py`. `Posting.with_salary` now validates (it used `model_copy`, which
  doesn't). `PrefilterMode` is defined once, in `config/sources.py`. Calibrate's
  `ConfigSpec.backend` is a `Backend`.
- **P9-D7 Enum members instead of string literals** wherever a DB enum exists (source run
  and board status, votes, backend, stop reason). In-memory statuses without a DB enum
  (`SourceOutcome.status`, `CallStatusName`) stay typed Literals.
- **P9-D8 One way to read secrets:** `config/secrets.secret(environ, name)` returns a
  `SecretStr` (stripped; blank counts as unset) for the feedback secret, the SMTP token,
  `ANTHROPIC_API_KEY` and `FINDWORK_API_KEY`, and for the "is it set" checks in fetching
  and `sources check`. Before, the API key and the Findwork key were plain strings, and
  the SMTP token wasn't stripped.
- **P9-D9 Calibrate's per-call record is a `CallDetail` dataclass**, replacing a dict with
  14 string keys. The saved JSON has the same keys.
- **P9-D10 Naming:** the fetch package's `env` is now `environ`, like everywhere else; the
  ATS mappers are `_map_greenhouse` etc., like the source mappers. Left alone:
  `Calibration.diff_stats()` still returns a dict, because the CLI prints it as is and
  changing it would change the output.

### P9 Bugs found and fixed

- **P9-B1 A malformed claude CLI reply could end the whole scoring run.** A non-numeric
  token count made `int()` raise, and a string `total_cost_usd` broke the runner's cost
  sum. Either one stopped the run with stop reason `error`, instead of failing one call
  (which is retried once, split in half). Now that call is `invalid`, and its error
  names the bad field. Not seen in practice (the CLI version is pinned), found in the P9-U5
  audit.
- **P9-U6 Fix the type checker's errors.** `pyright src` reported 62 errors. It now
  reports 0.
- **P9-D11 What the 62 were, and the fixes.** None changed behaviour on today's data:
  - 24 in `migrations/env.py`: the kwargs passed to Alembic were typed `object`; now `Any`.
  - 14 "argument missing": Pydantic `Field(None, …)` with a positional default, which type
    checkers don't read as a default. Every such field now uses `Field(default=…)`.
  - 10 in SQLAlchemy queries: `Select[tuple[int]]` is the 2.0 spelling; 2.1 uses
    `Select[int]`. The two `.rowcount` reads go through a small `_rowcount()` helper,
    since only a DML result has that attribute.
  - 5 "possibly unbound" in `HttpClient.get`: the retry `for` loop became `while True`.
    Same retries, and the checker can now see that `response` is always set.
  - 4 in the API backend: `params()` is typed with the SDK's own TypedDicts, and batch
    results are narrowed on `result.type`, which also removed a `getattr`.
  - 2 for the pre-filter: `JobLike` was a Protocol only so the tests could pass a fake,
    and ORM columns don't match a Protocol. The rules now take `Job`, and
    `tests/test_prefilter.py` builds unsaved `Job`s.
  - The rest: Remote OK's salary amounts go through `as_float()` (moved from the ATS
    mappers into `fetch/normalize.py`); `requests_last_24h` returns `or 0`; `effort` is
    annotated `Effort` in both backends (pyright widened it to `str`).

  Fixing these exposed 5 more, all fixed: Remote OK's fields and Working Nomads' tags
  were untyped. Tests now use the enums for `Scoring.limits()`. Pyright on `tests/`
  still reports 36 errors, all from before today: `Optional` values used inside asserts,
  and test doubles that don't match the code's types. Left for you to decide (P9-R1).
  `pyright src` is in CLAUDE.md's commands. Pyright isn't a project dependency; you
  have it through Neovim (Mason).

### P9 Bugs found and fixed (continued)

- **P9-B2 A non-numeric salary could have been stored in a number column.**
  `Posting.with_salary` used `model_copy`, which skips validation. So a Remote OK salary
  like "competitive" would have reached the `salary_min` Float column as text (SQLite
  accepts that). `with_salary` now validates (P9-D6), and Remote OK's amounts go through
  `as_float()`, so such a posting is kept without a salary. Not seen in practice.
- **P9-B3 `calibrate --configs` accepted any effort** (`api:model:1:extreme` would have
  reached the API and failed there). It's now checked against the allowed values when
  parsed, with a test.

### P9 For you to review

- ~~**P9-R1 The 36 pyright errors in `tests/`.**~~ Decided: fix them too (P9-U7).

### P9 Your decisions (continued)

- **P9-U7 Type-check the tests too.** `pyright` now checks `src/` and `tests/`
  (`include` in `pyproject.toml`'s `[tool.pyright]`, so a bare `pyright` does both):
  0 errors.

### P9 Decisions (Claude, continued)

- **P9-D12 How the 36 test errors were fixed:**
  - About 25 were strings that may be None (`reason`, `error`, `description_text`), used
    with `in` or `.startswith` inside asserts. They now use `(x or "")`, the pattern
    `test_ats.py` already used. Two other values that may be None get an
    `assert x is not None` first, and email parts go through a `body_of()` helper.
  - The mailer's `SmtpFactory` claimed to return `smtplib.SMTP`, so the tests' fake
    server didn't fit. It now returns `SmtpConnection`, a Protocol with the four methods
    the mailer uses. The real `_smtp()` is typed with it too, so pyright checks that
    `smtplib.SMTP` still matches. That check caught a wrong `__exit__` signature in
    the first draft of the Protocol.
  - Two test helpers had wrong hints: `conftest.load_http_fixtures` returns tuples, not
    responses; `test_ats.web()` and `detector()` take a `Mapping`. The live scoring test
    passes `Backend` members. One test asserts the `ApiLimits` type before reading
    `use_batches_api`.
- **P9-U8 Pyright in the pre-commit checks.** There were no pre-commit checks yet, so
  this adds them.

- **P9-D13 A plain git hook, not the pre-commit framework:** `.githooks/pre-commit` runs
  `uv run ruff check .`, `uv run ruff format --check .` and `pyright`. It installs
  nothing; the framework would be one more tool to install. Ruff comes from the project
  environment (the version in `uv.lock`); pyright from your PATH (Mason, currently
  1.1.413). If pyright is missing, the hook stops with a message instead of skipping.
  It checks the working tree rather than only the staged files, which is simpler and the
  same thing for normal commits. Tests aren't part of it (about 15 seconds); say if you
  want them.

### P9 For you to do

- ~~**P9-R2 Turn the hook on**~~ Done on your machine. On another clone, run it once there:
  `git config core.hooksPath .githooks`.
  The sandbox can't write `.git/config`, so it isn't on yet. Skip it for one commit with
  `git commit --no-verify`.
- **P9-U9 No references to this log in comments.** Removed every "(DECISIONS.md P4-U5)"
  style reference from code, config, deploy files and tests (11 places). Where a reference
  carried the reason, the comment now says it directly. Example: `postings_per_call: 1`
  now says "the prompt is too short to cache". CLAUDE.md's working agreement now forbids
  them. `config/filters.yaml` had one: it was made writable for that comment edit only,
  then set back to read-only. The pre-filter fingerprint hashes the parsed rules, not
  the file, so no verdicts went stale.
- **P9-U10 `config/filters.yaml` is writable** (it's edited often). Its mode is back to
  `rw-r--r--`, and CLAUDE.md no longer calls it read-only. `profile.md` stays read-only.
  Git doesn't track the write bit, so nothing in the repo changes except the docs.
- **P9-U11 No read-only config files.** `profile.md` is writable too, and the CLAUDE.md
  rule about read-only files is gone. The general rule stays: ask before changing what
  `config/` looks for. CLAUDE.md's scoring notes now say that any edit to `profile.md`,
  even a comment, changes `profile_hash` and queues every eligible job again.
- **P9-U12 Score each job once; editing `profile.md` re-scores nothing.** Only `rescore`
  queues a scored job again. You kept one automatic re-score: when a copy is linked to a
  scored job's group (e.g. the same job posted later for another country), it's queued
  again, because the combined locations can change where you're eligible.

- **P9-D14 How it's built:**
  - `scores` is unique per job instead of per (job, `profile_hash`).
  - `profile_hash` stays on the row as a record of the profile that scored it. It's set
    when the score is written, and `explain` still marks scores from an older profile.
  - The queue, the reports, the CSV export, `status` and the feedback page no longer
    filter by profile.
  - `rescore` overwrites the job's row; the old result isn't kept.
  - The migration (`cae2ac4fd837`) collapses existing rows to one per job: the latest
    real score, or else the newest pending row. Downgrading restores the old constraint
    but not the deleted rows.
  - On a scratch copy of your local database it removed the 1,120 pending rows left over
    from before the Phase 3 profile edit, and kept all 190 scores and the 1,380 pending
    jobs under the current profile.
  - New tests: a profile edit re-scores nothing, and a leftover pending row is scored
    and stamped with the current profile. A migration test covers the three
    collapse cases.
- **P9-U13 The feedback page's address is an env var only.** `base_url` is gone from
  `settings.yaml`; it's set with `JOB_SCOUT_FEEDBACK__BASE_URL` because it differs per
  machine (`jobscout.lan` on the server, `127.0.0.1` locally), like the email recipient.
  The setting still exists in the model, so the env var keeps working. The same goes for
  the token date: `JOB_SCOUT_SCORING__CLI__TOKEN_CREATED` is now in
  `deploy/job-scout.env.example`. The deploy README sets it there instead of editing
  `settings.yaml`, which keeps the server checkout clean for `git pull`.
- **P9-U14 `.env.example` lists every env var.** It was missing `JOB_SCOUT_CONFIG_DIR`,
  `JOB_SCOUT_SCORING__EFFORT`, `JOB_SCOUT_SCORING__CLI__TOKEN_CREATED`,
  `JOB_SCOUT_FEEDBACK__HOST` and compose's `JOB_SCOUT_UID`, `JOB_SCOUT_GID` and
  `JOB_SCOUT_FEEDBACK_PORT`. Those last three are marked as read by `docker compose`, not by
  job-scout. A new test (`test_dotenv_example_lists_every_env_var`) collects every env var
  name in `src/`, `config/`, `deploy/` and the Dockerfile and fails on any that's missing.
- **P9-B4 `sources check`'s health line didn't use `secret()`** (`report/data.py`): a blank
  `FINDWORK_API_KEY` counted as set there but as unset when fetching. Left over from P9-D8.
- **P9-U15 Nothing on the server runs as root or lives in a system path.** You keep
  everything in your home: the repo in `~/job-scout`, its data in the same folder. So:
  - Data paths are relative: `data/job-scout.db`, `data/reports`, `data/exports`,
    `data/claude-cwd` (settings.yaml and the built-in defaults). They resolve against the
    working directory: the repo root locally, `/app` in the container. Locally that's what
    `.env` already set, so the path lines in `.env.example` are now commented out.
  - compose mounts `../data` at `/app/data` (was `/var/lib/job-scout`), and the
    container's `HOME` is `/app/data/home`.
  - One `.env`: `deploy/job-scout.env.example` is deleted, and compose reads the repo's
    `.env` (`env_file: ../.env`, was `/etc/job-scout/job-scout.env`). Its notes on
    `claude setup-token` and the feedback address moved into `.env.example`, and the
    secrets test reads `.env.example`.
  - A new test fails if the Dockerfile or any deploy file names `/var/lib`, `/opt`,
    `/etc/job-scout` or `/etc/systemd`.
- **P9-D15 The systemd units are user units** (`~/.config/systemd/user`, `systemctl --user`),
  with `loginctl enable-linger` so the timer fires without a login. Consequences:
  - `WorkingDirectory=%h/job-scout/deploy`.
  - A user unit can't depend on the system's `docker.service` or `network-online.target`,
    so the run waits up to 5 minutes for `docker info` to answer (`ExecStartPre`). That only
    matters for a catch-up run right after boot (`Persistent=true`).
  - The failure email reads the user journal: `journalctl --user` matching the run's
    `_SYSTEMD_INVOCATION_ID` (its output; checked on this laptop that user-unit lines carry
    their own invocation id) or `USER_INVOCATION_ID` (the user manager's messages about it,
    e.g. a timeout kill, which the system version didn't include).
  - Docker itself stays rootful: your user needs the `docker` group. With rootless Docker
    the container's UID 1000 would map to another host UID and couldn't write `data/`.
  - `systemd-analyze verify` can't run in the sandbox; the README runs it on the server.
- **P6-R1 and P6-R2 updated:** the server steps are now the ones in `deploy/README.md`
  (clone to `~/job-scout`, `.env`, user units). The git remote exists
  (`github.com/pablogadhi/job-scout`, private), so updates are `git pull` in `~/job-scout`.
- **P9-B5 Tables were cut to 80 columns whenever the output wasn't a terminal**
  (`docker compose run -T`, a pipe, the journal): Rich assumes 80 columns there and squeezed
  `sources check` down to "ti…", "h…" and even an item count of 300 to "3…". The daily run's
  tables in the journal were cut the same way. The CLI's consoles now use 200 columns
  without a terminal, so tables print at their natural width (`COLUMNS` still wins). The
  CLI tests had hidden it by setting `COLUMNS=200`; a new test builds a non-terminal console
  without it. `COLUMNS=0` (set but not a width) made the CLI print nothing at all
  without a terminal; it now counts as unset.
- **Findwork checked against the live API (2026-10-14)** with your key: the mapper's
  fields match the real responses, and the first fetch gave 474 postings (404 new, 70
  duplicates). `sources.yaml` now has `checked: 2026-10-14`, and its notes describe what
  Findwork relists.
- **P9-D16 Findwork's HN items are skipped.** Findwork relists the HN "Who is hiring"
  comments that `hn_whoishiring` already fetches: 34 of its 40 HN postings were companies
  our HN source had. None of them merged, because our HN title is the comment's whole
  header line ("Mastra | AI Engineer | REMOTE …"), so each would have been scored twice.
  The other 26 HN items had no title at all and showed up as "26 items unmappable". They
  were all HN, checked by re-reading the same 5 pages. The skip is silent, not counted as
  unmappable. The 40 Findwork HN rows already in your local DB stay.
- **P9-B6 HTML-escaped titles and company names** ("Dynamics 365 CE/CRM &amp; F&amp;O",
  "O&#39;Brien"): 32 Findwork postings and 1 RemoteOK posting had them. Besides looking
  wrong in the digest, an escaped copy can't merge with the same job from another source,
  since the dedupe key is built from the names. `Posting` now decodes entities in `title`
  and `company` for every source. Rows already stored keep their text.
- ~~**P9-R3 HN postings never merge with other sources.**~~ Decided: fix it (P9-U16). Our HN title is the comment's
  header line (company | role | location …), on purpose, since Claude parses the rest. It
  never equals a plain job title, so an HN job that's also on a board is scored twice. It's
  rare now that Findwork's HN copies are skipped, but it can still happen with ATS boards
  and Remotive. A fix would be to take the role from the header's second field for the
  dedupe key only. Say if you want it.
- **P9-U16 HN postings are matched on their role.** A new `Posting.dedupe_title` (not
  stored) replaces the title when building `title_normalized`, so the dedupe key uses it.
  The HN mapper sets it to the header's role, and the title shown everywhere keeps the
  whole header.
- **P9-D17 How the role is picked:** the first header segment after the company that isn't
  a link, a domain, a work arrangement (remote, onsite, hybrid), an employment type, or
  pay. Parentheses are ignored for that test, so "Data Engineer (Remote)" counts as a
  role. On your 260 stored HN headers it found a role for 220.
  - Sometimes a place is picked instead ("Berkeley, CA"). That's harmless: a place never
    equals another job's title, so the posting just doesn't merge, which is how it was
    before.
  - A header with no role segment keeps matching on the whole header.
  - Side effect: the same company posting the same role in next month's thread now
    merges with this month's copy. That's correct, and as with any linked copy, the
    group is queued for a re-score.
  - HN rows already stored keep their old key, so a board copy of an October comment
    won't merge with it. From next month's thread on, every comment uses the new key.
- **P9-U17 More titles go to Claude.** You noticed titles like "Team Lead, Software
  Development", "Software Development Specialist II" and "Software Architect (L6)" being
  dropped, and suggested passing anything with "software" in it except managers. New
  `titles.include_any` patterns in `filters.yaml`:
  - "software" anywhere;
  - "<engineering|software|python|backend|platform|infrastructure> (team) lead";
  - "<software|platform|infrastructure|cloud|technical> architect";
  - "member of technical staff";
  - "desarrollador", "programador" and "desenvolvedor".
- **P9-D18 The "not management" check is inside the new patterns, not a new exclude.**
  The first two patterns skip titles naming a manager, director, head of, VP, sales or
  alliances. A global exclude for "manager" or "sales" would have dropped 20 titles that
  pass today, e.g. "Lead Software Engineer (Secrets Manager & AI Identity)", "Senior
  Software Engineer, Full Stack - Sales Planning" and "Tech Lead / Engineering Manager".
  Tested on the 3,208 titles in your local DB: 47 newly pass, none newly drop. A few
  weak fits now reach Claude ("ServiceNow Certified Technical Architect", "Salesforce
  Platform Lead", the "Post Training" research roles); `filters.yaml` sends unclear titles
  to Claude on purpose. The next `filter` re-checks every verdict, since the rules'
  fingerprint changed.
- **P9-B7 Remotive's robots.txt disallows the API we were calling.** It has `Disallow:
  /api/*` and `Disallow: /*category=`, which cover both `/api/remote-jobs?category=…` and
  `/api/remote-jobs/categories`. `HttpClient` let them through because
  `urllib.robotparser` reads `*` literally (found during Phase 10's source research;
  fixed in P9-B8).
- **P9-U18 Remotive reads its documented RSS feed instead**
  (github.com/remotive-com/remote-jobs-feed), which robots.txt allows. Done on this branch.
- **P9-D19 How the Remotive feed is read:**
  - One request to `https://remotive.com/remote-jobs/feed`: the same 15 jobs the free API
    served (the category feeds are subsets of it, and the README's slugs are outdated:
    `software-dev` is a 404).
  - Items carry `jobId`, the id the API used, so it stays the `source_job_id` and jobs
    already stored from the API keep matching instead of being stored again.
  - Company, location and job type come from the feed's own tags. There's no salary field:
    a salary is parsed only when the description labels it ("Salary: …"), otherwise it's
    left to Claude (1 of the 15 live items had one).
  - The first-run check of category slugs went with the API, and so did
    `JsonFetcher.before`, which only it used. The budget (4 requests a day) and the 31s
    spacing stay. A live fetch into a throwaway database stored 15 jobs with 1 request.
- **P9-B8 The robots.txt check ignored wildcards.** `urllib.robotparser` matches rule
  paths as literal prefixes, so RFC 9309 rules with `*` or `$` (`Disallow: /api/*`,
  `/*category=`, `/*/rss.xml`) never matched, and a byte-order mark at the start of the
  file hid the first `User-agent` line. Found during Phase 10's source research.
- **P9-U19 Fix it on this branch** (it changes the running instance).
- **P9-D20 `protego` replaces `urllib.robotparser`.** It's Scrapy's RFC 9309 parser
  (wildcards, `$`, longest match wins, our own `User-agent` group before `*`), so no
  matcher of our own to maintain. It doesn't strip a byte-order mark, so `HttpClient`
  does that before parsing. The 4xx/5xx handling is unchanged. New tests cover the
  wildcard, `$`, longest-match, byte-order-mark and own-group cases (5 of them fail
  with the old parser). Checked live: every enabled source's endpoint and the ATS APIs
  are still allowed, so the daily run loses nothing. Careers pages that `ats detect`
  reads may now be refused where their robots.txt uses wildcards, which is correct.
- **P9-U20 Findwork's page cap is config, with a one-run override.** The 5-page cap was a
  constant in the code, so a fetch only reached the newest ~470 of Findwork's 2,089 remote
  jobs (back to Sept 29). It's now `max_pages: 5` in `sources.yaml`, used by every daily
  run. `fetch --max-pages N` raises it for one run, for sources that set `max_pages`, to
  read further back for the backlog. The code keeps 5 as the fallback when the config sets
  none.
- **P9-D22 Findwork requests are 6 seconds apart** (`min_request_interval_seconds: 6`).
  Its responses carry `x-ratelimit-limit: 10`, and the remaining count dropped by one per
  request. They don't say over what window, so 6 seconds keeps us under 10 a minute. A
  5-page daily fetch takes about 30 seconds; a full read of ~22 pages takes about 2
  minutes.
- **P9-B9 CrazyGames was polled at a board that isn't theirs** (`careers-analytics`).
  Careers pages using Recruitee load a script from `careers-analytics.recruitee.com`, and
  the Recruitee pattern (`<token>.recruitee.com`) took that host for the company's board.
  The API answered 403 every day; their real board is `crazygames`. Recruitee's own hosts
  (`careers-analytics`, `app`, `cdn`, `assets`) are now non-tokens.
- **P9-B10 A deleted board was polled forever when the seed's careers URL named it.**
  Detection accepted a board from the careers URL without a request, so the monthly
  re-detection marked it ok again even after the board was gone. Found from a fetch where 3
  boards answered 404: Lambda School (`lever:BloomTech`), ReCharge Payments
  (`greenhouse:recharge`) and Sketch (`ashby:sketch`); all three still 404 on 2026-10-14.
- **P9-D23 A board found in a careers URL or page must answer on its API.** Any answer
  counts, even with no jobs: the company may just not be hiring right now. The probe
  still needs at least one job, since a slug guess that answers with an empty board is
  weak evidence. On a 404 the board doesn't count and detection moves on to the next
  method. A temporary failure makes the detection inconclusive, so the company keeps its
  board and is retried next run. Costs one request per company found this way per
  detection, about 130 a month for your seed.
- **P9-U21 Weaker watchlist jobs are collapsed.** Watchlist jobs scoring under 50 go in a
  collapsed "Watchlist below 50 (N)" block under the watchlist section, so a watchlist
  company posting many jobs at once doesn't swamp the digest. The cutoff is a setting,
  `digest.watchlist_collapse_below: 50`. Jobs between `watchlist_min_score` (21) and 50
  still appear and still count as reported; only their layout changes.
- **P9-D24 In the plain-text email part they're one line each** ("[34] Title - Company
  (job #N)"), since plain text can't collapse. The split happens in the templates, so the
  digest's bookkeeping (`Sections.watchlist`, recorded job ids) is unchanged.
- **P9-U22 Travel is not a work-life-balance concern.** Claude listed "travel twice a
  year" under WLB concerns. The user is fine with up to about 6 trips a year, maybe more,
  to anywhere in the world. `profile.md` now says so next to the on-call rule.
- **P9-D25 How the travel rule reads:** offsites, retreats, conferences and occasional
  customer visits are neutral up to ~6 trips a year; only travel clearly beyond that
  ("25%+", monthly or weekly trips) is a mild negative, never a disqualifier. Occasional
  travel never makes a role hybrid. Editing the profile re-scores nothing: jobs already
  scored keep their old travel flags.
- **P9-U23 Descriptions are cut at 20,000 characters instead of 6,000.** Measured on the
  local DB: 732 of 1,711 groups that passed the pre-filter were longer than 6,000, and in
  150 of them the salary, in 143 a location rule, appeared only in the part that was cut
  (one US-only security clearance among them). At 20,000 only the longest few are cut.
  Extra input is about 400k tokens for the whole backlog, ~$0.04 on Haiku 5.5.
- **P9-D26 The CLI's call limits stay as they are.** A packing simulation of the 1,531
  pending groups: per posting the median stays ~1,570 tokens, p90 goes from 1,660 to
  2,420, the max from 2,000 to 5,500. Calls stay at 20 postings (77 calls either way);
  the median call grows from 32k to 36k input tokens and the largest reaches 59k, so the
  60k input budget splits just one call. One live CLI call (approved by the user) sent the
  largest planned call, 11 long postings at ~59k estimated input tokens: all 11 results
  came back in 63 seconds, well inside the 600-second timeout. Earlier 20-posting calls
  took 47-178 seconds; the 2 of 11 that failed did so at the old cap too (exit code 1,
  `error_max_structured_output_retries`) and were retried split in half.
- **P9-U24 Cost estimates assume real reply lengths.** The dry-run price, the spend guard
  and a batch's stored estimate counted 450 output tokens per posting; real replies run
  ~1,250-1,550 with Haiku 5.5's thinking (190 postings over 11 CLI calls averaged 1,256;
  the live 11-posting call 1,530). They now use `COST_OUTPUT_TOKENS_PER_POSTING = 1_500`.
  For a 1,500-job backlog on the Batches API that's about $0.80 more than before, closer
  to what the user was billed.
- **P9-D27 Call packing keeps the 450.** `max_output_tokens_per_call: 12000` is tuned
  against it; raising it there would cut CLI calls from 20 postings to 7. API requests
  are capped at 1,900 + 8,000 thinking headroom = 9,900 output tokens, ~6× a typical
  reply, so that cap stays too. The spend-guard test's budget went from $0.004 to $0.01
  so it still admits part of the batch.
- **P9-D28 The cost estimate is 1,100 output tokens per posting, not 1,500.** The user ran
  the check on the server: the API backlog averaged 1,115 output tokens per posting over
  2,767 single-posting requests. That's the backend that bills, so it replaces the CLI
  figure (CLI calls hold 20 postings and ran longer per posting).
- **P9-U25 The user's settings changes:** `scoring.effort: medium`, daily CLI `max_calls:
  10` (was 5) and the Batches API for daily API runs (`use_batches_api: true`). A daily
  run on the API now waits up to `batch_max_wait_minutes` (120) for its batch; jobs whose
  batch isn't back in time go into the next digest. The user's formatter also realigned
  a few inline comments. Committed as the user made them.
- **P9-U26 Tests no longer depend on `config/settings.yaml`.** The user: "it's pointless
  to have a settings file that can change at anytime if the tests are going to break".
  Their `max_calls: 10` broke `test_limits_from_settings`.
- **P9-D29 How:** tests load `tests/fixtures/config/`, a pinned `settings.yaml` (the
  committed values before P9-U25, comments dropped) plus symlinks to the real
  `sources.yaml`, `filters.yaml` and `profile.md`. `test_limits_from_settings` reads the
  pinned settings, as it read the real ones before (the user: a test that builds its own
  limits checks nothing). `test_repo_config_is_valid` still loads the real `config/` but no longer
  asserts its backend and model. The deploy tests override `app_config` with the real
  config, since the timer, ports and paths must match what gets deployed. Checked by
  bumping every number and flipping every flag in a copy of `config/settings.yaml`: 15
  tests failed before, now only the feedback-port deploy check does, as it should.
- **P9-R5 Tests still read the real `sources.yaml`, `filters.yaml` and `profile.md`.** Some
  assert their content (the watchlist's first entry is Canonical, Findwork's `max_pages`
  is 5, the dedupe key). Not pinned yet, since the user asked about settings; left for
  review.
- **P9-U27 Findjobit reads every job, not only the Guatemala feed.** The user: "What if
  eventually some posts a remote job in another country though. Even if we don't find any
  right now, doesn't mean that there won't be any". A poster ticks the countries a job is
  open to; one who forgets Guatemala on a remote LATAM job would never reach us.
- **P9-D30 One feed, `/jobs/feed`, not one per country.** Checked live on 2026-10-16: it
  had 58 jobs back to Jul 24, and the Guatemala feed's 24 were all among them, so every
  country feed is a subset. None of the other 34 ticked Guatemala; most were hybrid or
  on-site in Chile, Mexico or El Salvador. A fetch into a copy of the local DB stored 34
  new jobs and matched the 24 known ones by link. The pre-filter sends 15 of the new ones
  to Claude as unclear location (a bare "Chile" is neither allowed nor excluded), a
  one-time ~15 scores, then a few a month. A synthetic fixture job (region "Chile",
  remote for all of LATAM) checks that it reaches Claude.

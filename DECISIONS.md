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

## Phase 7: Tier 2 scrapers (branch `phase-7-scrapers`)

Built after Phase 12 (P8-U1 had skipped it), so `phase-7-scrapers` starts from
`phase-12-points-score`. Every site was checked live on 2026-10-19 (robots.txt, feeds,
JSON-LD, job pages) before building.

### P7 Your decisions

- **P7-U1 NoDesk added, and scrapers for 4DayJob and RemoteJobs.lat tried**, the two
  Tier 2 sites you're interested in. The other Tier 2 sites stay unbuilt: hiring.lat
  (most salaries "$0-$0", listings up to two years old), Real Jobs From Anywhere (its
  `feed.xml` is a leftover template), Remote.io (a JavaScript app), Daily Remote and Remote
  Backend Jobs (aggregators: duplicates), DevOpsJobs (narrow), JobsByCulture (company
  culture, not jobs).
- **P7-U2 The ngo profile's scraping sources, now** (deferred by P10-U25 until this
  phase): unvacancies' job pages, the "unclear location" fixes, the HTML-only NGO boards.

### P7 Config fixes

- **P7-C1 NoDesk has feeds.** `sources.yaml` said "No feed"; `/remote-jobs/index.xml` (all
  jobs) and `/remote-jobs/engineering/index.xml` exist, 10 newest jobs each (the devops and
  remote-first pages it listed have none). NoDesk moved to Tier 1 as `rss`.
- **P7-C2 4DayJob answers only on `www.4dayjob.com`;** the bare domain returns 403.
- **P7-C3 unvacancies' budget: 12 requests a day → 200.** The 12 was for its feeds
  (P10-D34); now each new job's page counts too, up to 50 per feed. Changed in the repo's
  config, the pinned test config and the ngo instance's own `sources.yaml`.

### P7 Decisions (Claude)

- **P7-D1 NoDesk reads each new job's page** (like pyjobs): the feed has a summary, the
  page's JobPosting the whole posting. Its applicant location is always "Anywhere", even
  for a job "anywhere in India", so the place comes from the page's location line
  ("Remote" over "India", "Worldwide", "DE" or nothing), and the page's tags are added.
  On the laptop: 11 new jobs, 4 of 11 groups passed the pre-filter.
- **P7-D2 4DayJob is not built.** It lists 4dayweek.io's jobs (the same companies; logos
  served from `media.fourdayweek.io`), and its copies are worse: a hybrid Slovak bank job
  shows as remote in the US at $50k–$150k, posted in April. The `4dayweek` API source
  already has them, with their real arrangement. It stays in `sources.yaml` (Tier 2,
  unbuilt) with that note; say if you want it skipped for good.
- **P7-D3 RemoteJobs.lat is the first HTML scraper** (`fetch/remotejobs_lat.py`, Tier 2
  with `enabled: true`). Endpoints: the programming and devops-qa category pages (the
  Guatemala country page lists ~30 jobs, almost none in engineering). A page lists 50
  cards, newest first, each with its place, company, type, salary text and whole
  description; reading stops at a page with no new job, at most `max_pages: 5` per
  category. Each new job whose title passes gets its page read once, for the salary's
  period (the card shows "$2500 - $3000" with none) and the exact posting date; a card
  is otherwise dated from "10 days ago". A job listed in both categories is read once.
- **P7-D4 RemoteJobs.lat's applicant countries are ignored.** Every job whose card says
  "Remote" or "Remote Latin America" gets the same six (Argentina, Mexico, Colombia,
  Chile, Peru, Brazil) in its JobPosting: 66 of 67 on the first try. Worded "must be based
  in …", they'd have made the pre-filter drop nearly every job. The card's place is used.
- **P7-D5 Expect little from RemoteJobs.lat.** Most salaries are monthly, $1,500–$8,000
  ($18k–$96k a year). On the laptop, 10 of 97 jobs passed the pre-filter; 55 dropped as
  under $100k a year (no Claude spend), 32 on their titles. Many listings come from Get on
  Board (getonbrd.com), which has its own API; not researched yet.
- **P7-D6 A `scrape` source without a scraper** is skipped as "scraper not built"
  (`runner.not_built`), replacing the "Phase 7" placeholder; `sources check` and the
  digest's source health use the same rule. `ld_salary` (`fetch/pages.py`) reads a
  JobPosting's baseSalary for both RemoteJobs.lat and the Jobvite/iCIMS boards (same
  rules as before).
- **P7-D7 Scraped jobs can't be refreshed** (`refresh` maps only feed and API payloads
  again). The stored card is enough to add that later.
- **P7-D8 unvacancies reads each new job's page** (P10-R10). The pages carry no
  JobPosting, so the HTML is read by its `data-` attributes: the header's place ("Remote
  (linked to Dushanbe, Tajikistan)"), the decision strip (who can apply, contract,
  experience, languages; the closing date left out, worded "tomorrow"), the work mode and
  the official notice. The place and who can apply become `location_raw` ("…; who can
  apply: Nationals of Tajikistan only"), so ngo's location rules can drop "Colombia only"
  before Claude; the work mode replaces the feed's "Remote and home-based"; the strip's
  facts head the description. Checked live for ngo (laptop): 48 new jobs, 33 passed the
  title filter and all 33 pages gave a place and the full notice (4–20k characters, was
  ~140); 8 of 33 name who can apply (4 for one nationality only).
- **P7-D9 Only unvacancies needed the "unclear location" fix.** In ngo's database every
  other source's descriptions average 3–9k characters; unvacancies' were one line (138 of
  139 under 400). Its 76 unclear verdicts get pages through `refresh` (your server steps).
  `company_boards`' 34 unclear have full postings that don't say.
- **P7-D10 Idealist and Impactpool not built.** Their terms forbid bots without
  permission (P10-D4), and nothing about that changed; a scraper would break them whoever
  asks. They need permission or an API agreement first (P10-R4 is back open as P7-R1).
  DevNetJobs stays skipped (your P10-U20). No other HTML-only NGO board was found
  scrapable (P10-D4: robots `Disallow: /`, bot walls, 403s).
- **P7-D11 A job page is read once per run,** also when two feeds list the job
  (`with_details`); unvacancies' functions overlap, and so do NoDesk's two feeds.
- **P7-D12 unvacancies' robots.txt has a Content-Signal line:** `search=yes, ai-train=no,
  use=reference`, and nothing on `ai-input` (giving pages to an AI to answer, which the
  scoring does). Per its own legend, unset means neither granted nor restricted. Its feed
  has gone to Claude since Phase 10; noted in `sources.yaml`.

### P7 Bugs found and fixed

- **P7-B1 `refresh` saw every unvacancies job as changed.** A payload mapped again lacks
  what the feed's URL gave (`remote_raw` "Remote and home-based", `RssFetcher.shared`);
  the empty field wasn't written back, but it counted as a change, so the dry run would
  have reset 106 verdicts and queued 64 scores again for nothing. A field the posting
  lacks no longer counts (`refresh._apply`), with a test.

### P7 For your review

- **P7-R1 Idealist and Impactpool:** ask either one for permission or an API agreement,
  or leave them out (P7-D10).
- **P7-R2 4DayJob:** move it to `skipped:` for good (P7-D2)?

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
  review. Resolved in Phase 12 (P12-D27).
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

## Phase 10: A second profile (branch `phase-10-second-profile`)

The brief is `PLAN-phase-10.md`. Work happens in a separate worktree, step by step, with
your approval between steps.

### P10 Your decisions

- **P10-U1 A second profile, for one other person, alongside yours.** Its own instance
  (config, database, digest, feedback page) on the same server; the pipeline code is shared.
  Several profiles in one database was ruled out as not worth it for two.
- **P10-U2 The profile:** nonprofits and social development programs, an entirely different
  industry; strongest experience as a Program Director. Guatemala, remote only, work-life
  balance a priority.
- **P10-U3 Their instance scores with the `api` backend,** not your Pro subscription.
- **P10-U4 Research first:** the work starts with researching sources for this field
  (Step 1, no code), then continues one step at a time with your checks and approval in
  between.
- **P10-U5 A separate agent works on it** in its own git worktree
  (`~/Documents/Projects/job-scout-phase-10`), in a named session
  (`phase-10-second-profile`).
- **P10-U6 Remotive switches to its documented RSS feed** (`/remote-jobs/feed`, allowed by
  robots.txt, same 15 jobs, same job ids), on `phase-9-refactor` since it changes the
  running instance (P9-U18, P9-D19). It reaches this branch when phase 9 is merged in.
- **P10-U7 Fix the robots.txt check (P10-B1)** on `phase-9-refactor`. Done there with
  `protego` (P9-B8, P9-D20); this branch gets it with the phase 9 merge.
- **P10-U8 Sources to build:** ReliefWeb (API) and unvacancies.org first (Step 5), Workday
  as the first new ATS platform (Step 7). Step 1 is done.
- **P10-U9 The second instance is `ngo`:** daily run at 08:30 (after yours at 08:15),
  feedback page on host port 8766.
- **P10-U10 You request the ReliefWeb appname** (P10-R1).
- **P10-U11 The duplicate P9 IDs stay as they are** (P10-R8).
- **P10-U12 `skill_fit` and `seniority_fit` stay unchecked for 0–5** (P10-D17): no range
  notation in profile.md.
- **P10-U13 The profile skill is a project skill,** not one of your user skills (replaces
  P10-D23).
- **P10-U14 The skill asks one question at a time.**
- **P10-U16 The skill lives at the standard path,** `.claude/skills/job-scout-profile/`
  (you moved it; replaces the symlink in P10-D23).
- **P10-U15 Two instances never run at once.** Of the three options for shared rate limits,
  only this one: no check that a capped source is enabled in one instance only, and no
  shared request log across instances.
- **P10-U17 Feed before API, then robots.txt:** when researching a source, look for RSS
  first, then an API, then check robots.txt for each (now a convention in CLAUDE.md). You
  proposed ReliefWeb's job search feed
  (`/jobs/rss.xml?advanced-search=(TY263)_(CC6867)&search=remote`); robots.txt denies it
  (P10-R1).
- **P10-U18 Read ReliefWeb's feed despite robots.txt:** the feed is linked from every search
  page, so the `Disallow: /*/rss.xml` rule doesn't apply to it here. Settles the feed side
  of P10-R1; the API stays open.
- **P10-U19 Look up unvacancies.org's feed filters** (P10-R5).
- **P10-U20 Skip DevNetJobs.** Its terms don't allow its listings to be "stored or made
  available … in electronic … media … without prior permission", and few would be lost
  (P10-D38). It's under `skipped:` with that reason.
- **P10-U21 Company boards: Workday first, then Jobvite and iCIMS** (Step 7, from P10-D39).
  An instance lists its own organizations, and their boards are polled without the
  remoteintech seed.
- **P10-U22 The first real run for ngo (2026-10-16):** you set the instance's digest
  recipient to the second person's address (in its gitignored `.env`), then had the
  backlog scored on the api backend and the digest emailed to her (P10-D48).
- **P10-U23 No feedback links for ngo.** Its votes won't be used, so its `.env` gets no
  `JOB_SCOUT_FEEDBACK_SECRET` or base URL: its digests carry no 👍/👎 links and its feedback
  service isn't started.
- **P10-U24 No ReliefWeb API appname:** the feed is enough (closes P10-R1).
- **P10-U25 Scrapers wait** until they're built for your profile (Phase 7's scrapers):
  Step 6, unvacancies' job pages (P10-R10), the "unclear location" fixes for sources whose
  feeds lack the full posting, and HTML-only boards such as Idealist and Impactpool (P10-R4).
- **P10-U26 ngo's first digest looks right:** Step 5 is done (P10-R13). With Step 6
  deferred (P10-U25) and Step 7 met (P10-D48), every step of the phase is done or deferred.
- **P10-U27 The server setup waits** (P10-R7 stays open, for later).
- **P10-U28 No Jobvite pagination watch** (closes P10-R12).

### P10 Decisions (Claude)

- **P10-D1 How Step 1 was researched.** Docs and terms with web search and fetch. Every
  request that needed our User-Agent went through job-scout's `HttpClient` from a scratch
  script outside the repo: at most 3–4 requests per site, robots.txt included, saved
  only under the gitignored `data/recordings/phase10/`. Robots verdicts come from an RFC 9309
  matcher, not `urllib.robotparser` (P10-B1). The deliverable is
  `docs/phase-10-research.md`, with counts and structure only, no posting text. No code,
  config or tests changed.
- **P10-D2 Recommended first set: ReliefWeb (API) and unvacancies.org.** ReliefWeb is the
  widest development and humanitarian board, with a remote/roster view and facets for
  consultancy, program management and experience. unvacancies.org indexes 43 UN-system
  portals plus World Vision, IRC, Save the Children and Oxfam, and invites feed reuse.
  Together they cover the UN system, big NGOs and the consultancy market, in English and
  Spanish.
- **P10-D3 None of the general remote boards for this profile.** Working Nomads, Remote
  OK, We Work Remotely, Remote First Jobs and 4dayweek have about 0–6 nonprofit postings per
  100, mostly incidental. On the `api` backend each one would add pre-filter noise and
  spend for maybe one fit a week.
- **P10-D4 Skipped on terms or access, not on fit:** Idealist (terms forbid bots; API only
  by agreement), Impactpool (terms forbid scraping without permission), UNjobs and UNDP
  (robots `Disallow: /`), UN Talent, Devex, PND/Candid, Work for Good, DevelopmentAid and
  Bond (bot walls or 403 to our client; Devex and DevelopmentAid are also paywalled).
- **P10-D5 Skipped on fit:** 80,000 Hours, Probably Good, Escape the City (EA, UK-centred,
  few LATAM program roles), CharityJob and Hacesfalta (UK and Spain markets), Toptrabajos
  and Tecoloco (general Central American boards, almost no NGO postings). The "wrong field"
  entries in `skipped:` stay skipped. UN Talent's reason would become "bot challenge" if the
  list is touched.
- **P10-D6 Workday first among new ATS platforms (Step 7).** 17 of the 40 sampled NGOs
  have an identifiable ATS. Workday is the most common (Habitat, PATH, IRC, World Vision,
  WFP). Its career sites allow robots, publish a sitemap per site, and serve job JSON. The
  platforms job-scout supports today cover 1 of the 17 (GiveDirectly on Greenhouse). UKG's
  search path is robots-disallowed, and ADP blocks us.

### P10 Decisions (Claude, Step 2: instance support)

- **P10-D7 One file selects an instance in compose.** An instance's `.env` sets
  `COMPOSE_PROJECT_NAME`, `JOB_SCOUT_INSTANCE_DIR` and `JOB_SCOUT_FEEDBACK_PORT`, and
  `docker compose --env-file ../instances/<name>/.env ...` uses it for its project, mounts,
  secrets and port. Without the flag, every path resolves as before (`..`, the repo root).
  Checked with `docker compose config`; `job-scout` ignores the extra variables in the
  container. Shell exports win over the file, so the docs say not to export them.
- **P10-D8 Your units stay as they are; other instances use a template.** The plan moved
  yours to `job-scout@default`. That would need the repo-root instance special-cased (its
  path and project name differ from `instances/<name>`), and moving a running timer risks
  a missed or doubled run. Instead `job-scout@.service` runs any other instance, and a
  test keeps it identical to `job-scout.service` apart from the `--env-file`, so a fix
  can't land in only one of them.
- **P10-D9 Every instance's failures go through your `job-scout-failure.service`**, which
  runs as your instance: failure emails, with the run's log, go to you, never to the
  instance's recipient. If two runs failed at the same moment, the second report could
  merge into the first; the runs are 15 minutes apart.
- **P10-D10 One timer file per instance, committed** (`job-scout@ngo.timer`: the name and
  time aren't personal data). The test checks it against `instances/ngo/config/settings.yaml`
  when the instance is on the machine and skips it otherwise. The plan's synthetic stand-in
  instance was dropped: it would only have tested the test.
- **P10-D11 Local commands run from the instance's directory**, where the relative
  `config/` and `data/` paths point into the instance and uv still finds the project. No
  new code or variable; `alembic -c ../../alembic.ini` for migrations.
- **P10-D12 `instances/` is in `.gitignore` and `.dockerignore`.** No `COPY` takes it into
  the image, but without the ignore the build would send the other person's `.env` and
  database to the Docker daemon as build context.
- **P10-D13 The local `ngo` instance** is a copy of your config with `backend: api` and the
  08:30 run time, and a `.env` with only the three compose lines. Checked: migrate,
  `sources check` and `digest --dry-run` from its directory, the deploy tests (its timer
  matches its settings) and `docker compose config`. Not checked here: a real container
  run and `systemd-analyze verify` (the sandbox reaches neither Docker nor a systemd
  runtime directory).

### P10 Decisions (Claude, Step 3: a scoring format for both profiles)

- **P10-D14 Every output field is typed from its example in profile.md.** `"a | b"` is one
  of those values, text is a string ("… or null" or `null` allows null), `["text"]` a list
  of strings, and numbers and true/false keep their type. `role_category`'s values
  therefore come from the profile like any other field's. Nested objects and other examples
  fail at load. A profile can add any field without a code change.
- **P10-D15 Core fields:** `disqualified`, `disqualify_reason`, `remote`,
  `location_eligibility`, `salary`, `matched_skills`, `gaps`, `red_flags`,
  `overall_score`, `summary`. The brief's list plus `disqualify_reason`, which the reports
  show next to `disqualified`. A profile must declare them with the same values the code
  types them with (`CoreResult`), or every command stops with a config error.
  `role_category`, `skill_fit`, `seniority_fit` and `four_day_week`, required until now,
  became your profile's own fields.
- **P10-D16 Two models:** a strict one per profile for Claude's replies (built in the
  profile's order, job_id first, extra keys rejected, cached per profile hash), and
  `CoreResult` for reading any stored result: the core fields typed, every other field kept
  as it is. Reports, feedback and export read with `CoreResult`, so a score made under an
  older profile still reads. Stored results keep the profile's field order.
- **P10-D17 Your profile's schema is unchanged, byte for byte** (key order included;
  `tests/fixtures/score_schema_repo_profile.json` holds the old one, and a test compares).
  One difference in validation: `skill_fit` and `seniority_fit` lost their 0–5 check. The
  range is in profile.md's prose, which the code doesn't read, and the API already stripped
  it from the schema, so what Claude sees is the same.
- **P10-D18 Reports show a profile's own fields generically.** The templates keep their
  rows for the fields they name (`NAMED_FIELDS`), now each guarded with `is defined`.
  Any other field becomes a "Label: value" row after the work-life rows, and a CSV column
  after the fixed ones. Your profile has no such field, so its outputs don't change. The
  role row needs your profile's five role fields; a profile without them gets a short
  form ("program-director · Director · remote: yes").
- **P10-D19 Checked on your data:** the backlog report (HTML and text, all 190 scored
  jobs), a dry-run digest (32 jobs, after clearing `digests` in a copy) and the CSV (4,089
  rows) are byte-identical before and after, rendered from a copy of your laptop database
  with a fixed clock and fixed feedback links.
- **P10-D20 Working Nomads' categories are config:** `categories: [Development]` on the
  source (empty keeps all). `JsonFetcher` got a `keep(ctx, item)` hook, run inside the
  mapper so an item it can't read is skipped like any other.
- **P10-D21 Tested end to end with a synthetic nonprofit profile** (`tests/test_profile_fields.py`):
  own role categories, travel, contract length and donor fields, none of your profile's
  extras. Claude is asked for exactly its fields; a reply with an unknown travel value is
  rejected; the backlog report and the CSV show the new fields.

### P10 Bugs found (not fixed: Step 1 changes no code)

- **P10-B1 `HttpClient`'s robots.txt check ignores wildcards.** `urllib.robotparser`
  compares rule paths as literal prefixes, so RFC 9309 rules like `Disallow: /api/*`,
  `/*category=` or `/*/rss.xml` never match and the client fetches disallowed URLs. It also
  doesn't strip a leading byte-order mark, which drops the first group's `User-agent` line.
  Fix options in P10-R3.
- **P10-B2 Remotive's robots.txt disallows both configured Remotive URLs** (`/api/*` and
  `/*category=`). The `sources.yaml` note "robots.txt allows job-scout's UA" (2026-10-12)
  comes from P10-B1. Every other existing endpoint, the ATS APIs included, is allowed
  under RFC 9309.

### P10 Decisions (Claude, Step 4: the profile skill)

- **P10-D22 The skill is instructions and references, no code:** `skills/job-scout-profile/`
  with `SKILL.md` (the workflow: read the CV, draft, interview one question at a
  time, write the three files, check, hand over) and `references/` (the questions,
  each with where its answer goes; a profile.md template; a filters guide with a full
  example for program roles in English and Spanish). Ground rules in the skill: the CV and
  everything generated stay in `instances/<name>/`, no spending or email without a yes,
  network only through `job-scout fetch`, posting text is untrusted.
- ~~**P10-D23 Installed by a chezmoi symlink** into `~/.claude/skills/`.~~ Replaced by
  P10-U13: the files stay in `skills/job-scout-profile/` (the sandbox can't write under
  `.claude/skills`, so that's where they can be edited and tested), and a committed relative
  symlink, `.claude/skills/job-scout-profile` → `../../skills/job-scout-profile`, makes it a
  project skill. Your session cleanup hook only removes empty read-only stub files directly
  in `.claude/`, so it leaves the link alone.
- **P10-D24 Proposed sources go into an instance's `sources.yaml` with `enabled: false`** and
  a note. The fetch runner skips a source without a fetcher ("no fetcher registered"), so
  they're harmless until Step 5 builds them.
- **P10-D25 Request budgets count per instance database.** A source with
  `max_requests_per_day` enabled in two instances gets both budgets from the same site. The
  skill keeps such sources in one instance, or halves the budget in each.
- **P10-D26 `hn_whoishiring` should be off for a profile outside tech:** its
  `remote_mention_only` mode skips the title rules, so every remote-mentioning post would
  reach Claude (54% of HN posts passed the example filters in the smoke test).
- **P10-D27 Checked:** tests keep the references valid (the template loads and declares
  the core fields as the code types them; the filters example validates and keeps or drops
  ten sample titles as intended). A throwaway instance built from the references passed
  `sources check`, and its filters ran over the 3,468 postings in a copy of your database
  (all tech boards): 6% passed, mostly program and lead titles. The throwaway instance and
  the copy were deleted. Not done: a run with the real CV, which is yours (P10-R9).

- **P10-D29 With the skill in `.claude/skills/`:** Claude Code's Bash sandbox mounts that
  directory read-only (it's one of the sandbox's protected paths; while it didn't exist, the
  sandbox blocked the path entirely, which is what looked like "unreadable" before). So the
  skill's test reads it as before, in Claude's sessions too, and shell commands can't
  change it. The docs say no `allowWrite` entry or `Edit` rule exempts a protected path;
  only `filesystem.disabled` (every path, and not settable in a project's settings) turns it
  off. Claude changes the skill with its Edit and Write tools, which run outside the sandbox
  and ask you first (or once per session for the project's `.claude/`). `skills/README.md`
  is gone; CLAUDE.md describes the layout.
- **P10-D28 How:** `After=job-scout.service` on `job-scout@.service`. Each instance counts
  its request budgets in its own database, and the spacing between requests to a host lives
  in each run's process, so two runs fetching at once don't see each other's requests. That
  only happens when the server boots after both run times: `Persistent=true` then fires both
  timers together, and `After=` makes the instance's run start once yours has finished (for a
  oneshot service, once its command exits). On a normal day it changes nothing. After a late
  boot, the instance's digest waits for your whole run (up to its 2h timeout). The test that
  keeps the template equal to `job-scout.service` now expects this one extra line.

### P10 Decisions (Claude, Step 5: ReliefWeb)

- **P10-D30 `ignore_robots_txt` is per source** (`sources.yaml`, default false), not a host
  or URL list: the exemption is visible where the source is defined and covers only its
  requests. It skips the allow/disallow rules only: robots.txt is still read and its
  Crawl-delay, the 1s spacing and the budget still apply. Budget 4 requests a day.
- **P10-D31 The `reliefweb` source is in the repo's `config/sources.yaml` with
  `enabled: false`,** so your instance doesn't fetch it, and the ngo instance's copy turns
  it on. Its endpoint is your saved search (type Job, category Program/Project Management,
  text "remote"); others can be added as more endpoints.
- **P10-D32 Mapping:** `source_job_id` is the numeric job id from the link (the API's id,
  so a later switch to the API still matches stored jobs); company from `<author>`; the
  duty-station country from the description's `Country:` line as `location_raw`. No country
  means the site's Remote / Roster / Roving list, left as no location (unclear, so Claude
  judges it). Checked on the 20 live items of 2026-10-15 (recorded in the gitignored
  `data/recordings/`): 17 have a country, 3 don't; most are on-site jobs that mention remote
  work, since "remote" is a full-text search.

- **P10-D33 unvacancies' feed filters** come from its partner kit's script (the page that
  builds feed URLs for partners): `remote=true`, `function`, `contract_type`, `grade`,
  `location`, `organization`, sorted. robots.txt allows `/feed.xml` with any of them.
  Requests made: robots.txt, the partners page, its script chunks, the plain and three
  filtered feeds, `/jobs/function` and one job page.
- **P10-D34 One feed per function, remote only:** each feed holds its 50 newest items, and
  the plain remote feed had 37 from one day, so a daily run could miss jobs. Starting
  functions: programme-management and monitoring-evaluation (the categories the Step 1
  research matched to the profile); the profile skill picks the instance's final list.
  `sources.yaml` lists every function and filter. Budget 12 requests a day. In the repo's
  config it's `enabled: false`, on in ngo (like reliefweb). First fetch: 51 jobs (the
  remote M&E feed had 1).
- **P10-D35 `RssFetcher(shared=...)`:** the fields every item of one endpoint shares, read
  from its URL. Items don't say they're remote; the feed's `remote=true` does, so they get
  `remote_raw` "Remote and home-based" (the site's label), which Claude sees and no remote
  rule drops. The guid (the link without its `utm_` parameters) is the id and the URL; the
  agency (`category`) is the company.

### P10 Decisions (Claude, the ngo profile and more sources)

- **P10-D36 The profile skill ran with the real CV** (P10-R9). The operator answered the
  interview; the generated `profile.md`, `filters.yaml` and `sources.yaml` stay in the
  gitignored instance. Findings that apply beyond it: filter rules match one field at a time,
  so a profile that accepts hybrid work in one country can't drop "hybrid" by regex (the
  remote field and the location field are matched separately); and the general remote
  boards stay off for a nonprofit profile, as P10-D3 found.
- **P10-D37 More feeds for the ngo instance, each checked with a recorded fetch.**
  ReliefWeb's advanced search joins groups with `_` (AND) and values within a group with `.`
  (OR), and its full-text `search` takes `OR` and quoted phrases; facet IDs come from the
  search page (type: Job TY263, Consultancy TY264; career category: Program/Project
  Management CC6867, Monitoring and Evaluation CC6868, Donor Relations/Grants Management
  CC20966; country: Guatemala C109; theme: Agriculture T4587, Education T4592, Food and
  Nutrition T4593, Gender T4594, Climate Change and Environment T4588). Every recorded item
  carried the requested facets. unvacancies' feed ignores `location=` with the site's own
  place names (the "Guatemala City, Guatemala" feed was the unfiltered one, from New York to
  Addis Ababa), so that feed was dropped and the repo's comment says so.
- **P10-D38 DevNetJobs measured before deciding.** robots.txt allows it (`Allow: /`). Its
  recorded home page (2026-10-14) had 90 listings: 46 pass the ngo title filter, but nearly
  all are on-site in a named city; 5 say remote (1 already in the ngo database from
  ReliefWeb) and 3 say "Regional / Global". None in Latin America. Terms: `termsofuse.aspx`.
- **P10-D39 Company boards for this field are spread over many job systems.** The careers
  pages of 38 organizations that fit the ngo profile, read through `HttpClient`: Jobvite
  (Heifer), iCIMS (Pact; ACDI/VOCA by web search), Workday (Room to Read), Zoho Recruit
  (Fundación Capital), Taleo (CARE), UKG (Rainforest Alliance), ADP (Grameen Foundation,
  Global Communities, DAI). 13 answered HTTP 403 to our client (TechnoServe, Root Capital,
  CRS, IDB, Chemonics among them); the rest list jobs on their own pages or load them with
  JavaScript. IDB's vacancies are on an Impactpool career site, whose terms forbid scraping
  (P10-R4).

### P10 Decisions (Claude, Step 7: company boards, Workday)

- **P10-D40 `HttpClient.post`** for APIs that only answer a POST: the same checks as a GET
  (forbidden hosts, robots.txt on its URL, spacing, budget, 429 retries). A recorded POST is
  keyed by method, URL and JSON body (`request_key`), since Workday's pages differ only in
  their bodies; the fixture transport looks requests up the same way.
- **P10-D41 Workday, checked live on two NGO boards (2026-10-15).** A token is
  `host/tenant/site`; an API template may have several placeholders, filled from the
  token's parts. The tenant comes from the board page's config (`tenant`, `siteId`): the
  API's isn't always the host's (Room to Read's host says `osv-rtr`, its API answers only
  as `osv_rtr`, HTTP 422 otherwise), so detection reads the page once. The list is POSTed 20
  at a time (at most 50 pages); only the first page's `total` is used. No slug probing: a
  board's host (wd1, wd3, wd5…) and site name can't be guessed. robots.txt is read per host
  (Room to Read: `Allow: /RoomToRead/`, the API path not disallowed; myworkdaysite.com
  answers robots.txt with HTTP 422, which RFC 9309 treats as allowed). No Workday-wide terms
  for career-site visitors turned up; terms are each employer's.
- **P10-D42 A description costs a request per job, so only for jobs that need it:** not
  stored with a description yet, and a title that passes filters.yaml (`Prefilter.
  title_passes`). The others are stored from the list (a dropped title never reaches
  Claude) or only marked seen. When the rules change and a job stored without a description
  now passes, the next fetch gets its details and the store fills in the missing fields
  (never the source id, title or company). First run of the ngo instance's six boards: 863
  jobs, 350 requests, 9 minutes; the next run, after loosening its title rules: 80 requests,
  33 jobs filled in.
- **P10-D43 An instance's own companies, polled without the seed.** `companies:` in
  `sources.yaml` (company and careers URL) is detected like seed companies; a watchlist entry
  for the same company wins, and a company taken off the list goes back to the seed or stops
  being polled. A new source `company_boards` (method `ats_boards`) polls `companies` and the
  watchlist; `remoteintech` (`ats_seed`) polls them too, with the seed. At most one is
  enabled (config error otherwise), so no board is polled twice: off in the repo's config,
  on in ngo, where remoteintech is off.
- **P10-D44 The ngo instance's company boards.** Detected: Room to Read, WFP, World Vision
  International, Habitat for Humanity (its jobs site and its consultancies site) and IRC,
  all on Workday. World Vision's careers page answers 403 to us, so its careers URL is the
  board found by a web search; IRC's careers site names no board, so its URL is the board
  from the Step 1 recordings. Heifer, Pact and ACDI/VOCA are unknown until Jobvite and iCIMS
  are supported. First filter: 294 of 732 board jobs pass (40%). Overlap with unvacancies is
  small: one WFP and one World Vision job under both names ("WFP" vs "World Food Programme").

- **P10-D45 Jobvite and iCIMS, checked live on Heifer's and Pact's boards (2026-10-15).**
  Neither has a public jobs API. Jobvite's board page (`jobs.jobvite.com/<company>/jobs`)
  links every job with its title; iCIMS's sitemap (`<portal>.icims.com/sitemap.xml`) lists
  every open job's page, whose slug stands in for the title. Each job page carries a
  schema.org JobPosting in JSON-LD (iCIMS's in its `?in_iframe=1` view), read by one shared
  mapper, under P10-D42's rule (only new jobs whose titles pass). Robots: jobs.jobvite.com
  has no robots.txt; iCIMS disallows only login, referral and candidate paths. Neither is
  slug-probed. Jobvite's pagination wasn't seen (Heifer has 7 jobs): a board page showing
  it is logged, and only its first page is read. The iCIMS token pattern skips its CDN
  hosts, which careers pages also link.
- **P10-D46 The ngo instance's boards, final:** six Workday (P10-D44), Heifer (Jobvite) and
  Pact (iCIMS). ACDI/VOCA is off the list: its site answers 403 and both of its iCIMS
  portals are gone. A third fetch filled in Heifer's "Manager - MELS" after its filters
  learned "MELS". 312 of 754 board jobs pass the pre-filter; scoring every passing job of
  the instance is estimated at $0.31 (459 calls, `score --dry-run`).

- **P10-D47 Paid check of the ngo profile (your yes, 2026-10-15):** `calibrate` on 10 job
  groups, Haiku one posting per call against five per call, $0.012 in all. The reference
  scores read right: on-site and local-only roles disqualified (8–10), a remote GEF
  consultancy 60, an M&E consultancy roster 45 (location and pay unclear), a remote English
  tutoring job 22. Five per call disagreed on hard requirements (up to 22 points, rank
  agreement 0.81), so the instance stays at one posting per call. Before it, Workday's
  explicit on-site jobs are dropped by a remote rule in ngo's filters.yaml (your yes to
  P10-R11): 69 groups, 390 still pass.

### P10 Decisions (Claude, the first real run)

- **P10-D48 What the run did:** 390 calls, one posting each (claude-haiku-5-5), $0.25
  estimated, 0 failures. The digest (2026-10-16 03:44 UTC) had 2 top matches and 40 with
  unclear location, and its source health lists `company_boards` with 886 items, which is
  Step 7's "done when". Most unclear locations are unvacancies jobs: its feed gives only a
  one-line summary, so eligibility can't be read (deferred, P10-U25).
- **P10-D49 Deploy docs: an instance's feedback page is optional** (`deploy/README.md`,
  step 4 and the update note), and ngo doesn't run one (P10-U23).

### P10 Bugs found and fixed (Step 7)

- **P10-B5 The profile skill's paid check couldn't run.** It gave `calibrate` one config;
  `calibrate` compares, so it needs a reference and at least one more. The skill now
  passes two (one and five postings per call).

- **P10-B4 `run` would have downloaded the remoteintech seed in an instance that turned it
  off.** The weekly upkeep took the first `ats_seed` source, enabled or not, so the ngo
  instance's first `run` would have downloaded the seed and detected 40 tech companies a
  run. Never happened (ngo has never run `run`). Now the seed is downloaded only while
  `remoteintech` is the enabled board source.

- **P10-B6 The digest warned about the Claude Code token on the api backend.** ngo's first
  digest said "CLAUDE_CODE_OAUTH_TOKEN creation date unknown", though the api backend uses no
  OAuth token. `scoring_status` now takes the backend and warns only for `cli`; `status` and
  the digest pass it.

### P10 Bugs found and fixed (Step 3)

- **P10-B3 The plain-text report ran its lines together.** The text part of every digest
  email (and the backlog report's text) read "…Vercel  Strong backend fit…" and "Salary
  (meets): … USD  Matched: …  Gaps: …  Apply: …" on one line, with no blank line
  between jobs. `report.txt` places its own newlines, as Jinja does by default, but it was
  rendered with the HTML template's settings, which trim the newline after every block
  tag. It now has its own environment with the defaults; the template itself is
  unchanged. Found while comparing Step 3's output; a test checks one field per line.
  HTML mail clients show the HTML part, which was never affected.

### P10 For you to review

- ~~**P10-R1 ReliefWeb access.**~~ The API needs a pre-approved `appname` (request form on
  reliefweb.int; yours to submit), and `api.reliefweb.int/robots.txt` disallows everything
  while the docs invite API use. Recommended: ask about the robots rule in the appname
  request, and exempt that host from the robots check once approved. The RSS feeds are no
  way around it: `reliefweb.int/robots.txt` has `Disallow: /*/rss.xml` for every agent
  (checked again on 2026-10-15), so `HttpClient` refuses `/jobs/rss.xml`. Robots allows the
  first page of a filtered `/jobs?...` listing (`&page=` is disallowed), which a scraper
  could read. Feed side decided: read it anyway (P10-U18); the API part is still open.
  Closed: no appname (P10-U24).
- ~~**P10-R2 Remotive in the existing instance.**~~ Decided: read its RSS feed instead
  (P10-U6).
- ~~**P10-R3 The robots fix (P10-B1).**~~ Decided: fix it on `phase-9-refactor` (P10-U7).
- ~~**P10-R4 Idealist and Impactpool:** ask either for permission or an API agreement, or
  leave them out.~~ Deferred with the scrapers (P10-U25).
- ~~**P10-R5 unvacancies.org's feed filters aren't documented.**~~ Decided (P10-U19);
  found (P10-D33).
- ~~**P10-R6 Choose the sources to build.**~~ Decided (P10-U8).
- **P10-R7 On the server**, later (P10-U27) (`deploy/README.md`, "Another instance"):
  copy `instances/ngo` over, `sources check` through compose, install `job-scout@.service`
  and `job-scout@ngo.timer`, run `systemd-analyze --user verify` on them, and check
  `list-timers` shows both runs. Rebuild the image first (protego, ReliefWeb, unvacancies,
  Workday, Jobvite and iCIMS).
- ~~**P10-R8 Five P9 IDs are defined twice** (P9-B7, B8, U18, D19, D20).~~ Decided: leave
  them (P10-U11).
- ~~**P10-R9 Run the profile skill with the real CV** (Step 4's "done when"): put the CV in
  `instances/ngo/`, start a new Claude Code session in this worktree and ask for it
  (`/job-scout-profile`). Then review the generated `profile.md`, `filters.yaml` and
  `sources.yaml`.~~ Done (P10-D36).
- ~~**P10-R11 Drop Workday's explicit on-site jobs in ngo?** 65 of the 294 board jobs that
  pass say "In Person" or "Fully Present" in Workday's work-mode field, which the ngo
  profile disqualifies. A remote rule matching that whole field
  (`'^(in person|fully present|on-site)$'`) would drop them before scoring (about 65 calls
  once, a few a day after). The profile skill tightens filters only on the operator's say-so.~~
  Done (P10-D47).
- ~~**P10-R12 Jobvite pagination:** only a board's first page is read (P10-D45). If the log
  ever says a Jobvite board "has more pages", record that board and model its next pages.~~
  Dropped (P10-U28).
- ~~**P10-R10 unvacancies job pages:** feed items carry only a one-line summary, so Claude
  scores them on little more than the title. Each job page has the full notice, work mode,
  duty station and deadline (robots allows `/jobs/`). Reading them is one request per new
  job (a few a day per feed): a small scraper, which needs your approval.~~ Deferred with
  the scrapers (P10-U25).
- ~~**P10-R13 Your verdict on ngo's first digest** (Step 5's "done when": "a real digest for
  the second profile looks right to me"):
  `instances/ngo/data/reports/digest-20261016T034412Z.html`.~~ It looks right (P10-U26).

## Phase 12: A points score computed by code (branch `phase-12-points-score`)

The plan was approved on 2026-10-16. Claude extracts facts from each posting (requirements
with match levels, signals with quotes, seniority, role, remote, location, salary), and
the code computes the score from a points table in `config/scoring.yaml`. Phase 11 (ATS
boards from postings) stays deferred and keeps its number. Work goes step by step, with
your approval between steps.

### P12 Your decisions

- **P12-U1 A score whose calculation is visible.** You: "I feel like the scoring right now
  is a bit arbitrary... Can you think of a more deterministic way to do the scoring, one
  where it's clear how the score was calculated because of the things found in the job
  posting?" Measured on the local DB: jobs with the same skill fit, seniority fit, salary
  and location verdicts got scores from 28 to 60 (8 jobs at 4/4, unlisted, unclear) and
  35 to 66 (15 jobs at 3/4).
- **P12-U2 Start after Phase 10,** from its branch. The branch starts at `d4b2bc0`
  (`phase-10`), which already contains `phase-9-refactor`, so the planned merge of the
  phase-9 fixes wasn't needed.
- **P12-U3 Every stored score is redone once** under the new format (an approved backlog
  run), instead of keeping Claude's old numbers alongside.
- **P12-U4 After a weight change, an old unreported job reaches a digest only if it was
  first seen within the last 14 days,** so a change can't flood a digest.
- **P12-U5 The time-zone rule in profile.md was ambiguous.** "No time-zone requirement
  disqualifies a posting on its own" could read as "a posting without a time-zone
  requirement is disqualified". You: "If the job posting is remote and there's not timezone
  requirement at all, it does quialify". It now says a posting naming no time zone is fine,
  and a working-hours time zone never disqualifies; a UTC band stated as a residency rule
  is still a location rule. The same edit added the blank lines markdownlint wanted around
  the skill headings and lists (no wording change).
- **P12-U6 English or Spanish are both fine, any other required language disqualifies.**
  You: "I don't really care about the language as long is either english or spanish. A role
  in spanish being a signal would increase points, I don't want that. What should be
  disqualifying is a role that requires any other language." The `spanish_valued` signal
  (+3) is gone from profile.md and scoring.yaml. A new field `language: english-or-spanish |
  other` is hard requirement 5, and a disqualify rule turns `other` into a disqualification
  ("requires a language other than English or Spanish"). Claude's choices, for your review:
  - `other` also when the posting is written in another language with no sign English or
    Spanish is enough; a language that's only a plus ("French is an asset") stays
    `english-or-spanish`, and so does a posting that doesn't say (never guess a fail).
  - Spoken languages are no longer `requirements` entries, so they add or take no skill
    points either.
  - Like any profile field, it's listed in the digest ("Language: english-or-spanish") and
    gets a CSV column.
  - The profile skill now says a dealbreaker can be a field of its own named by a
    `disqualify` rule, not only `remote`, `location_eligibility` or `salary`.
- **P12-U7 A new domain: human capital management.** You: "Add one more domain more: Human
  Capital Management (HCM), I worked in a project for [two HCM companies]." profile.md's
  domain experience names HCM (HR, payroll, workforce and talent software), and the new
  `domain_hcm` signal is worth +2 in scoring.yaml, like fintech and healthcare (Claude's
  pick, for your review: CRM and B2B platforms are +1). You took the company names out of
  profile.md: they don't go in the repo.
- **P12-U8 You approved the profile.md rewrite** (Step 3, with P12-U5 to U7): Claude
  reports facts and the code scores them.

### P12 Decisions (Claude, Step 1: a list-of-objects field)

- **P12-D1 Step 1 adds only the new field type;** the core fields move later. Removing
  `overall_score`, `disqualified` and `disqualify_reason` from the core before
  `profile.md` is rewritten would fail every command at startup, so that change lands
  with the profile rewrite.
- **P12-D2 A list of objects is declared by one example object:** `[{"skill": "text",
  "match": "core | missing", "primary": false}]`. Its fields are typed like top-level
  ones but must be flat and never null: a nullable field is a union, and the API limits
  unions in a schema. Each becomes a nested model (`extra="forbid"`, named
  `<Field>Item` in the JSON schema's `$defs`), so Claude can't add or drop keys. In
  reports and the CSV, an item reads "first value (the others)", with a true flag shown
  by its name and a false one left out: "M&E (required, primary)".

### P12 Decisions (Claude, Step 2: the points engine)

- **P12-D3 The engine works on output fields, not on engineering terms,** so the second
  profile can use it with its own vocabulary. `config/scoring.yaml` has three component
  kinds: `enum` (points per value), `requirements` (`max` × the weighted share matched,
  over a list of objects) and `signals` (points per signal, summed and clamped), plus
  `caps` and `disqualify` rules over enum fields. Each enum map must list every value its
  field allows, so a new value in profile.md forces a decision on its points;
  `check_points` (exit code 2 once it's wired) names what's missing or unknown.
- **P12-D4 First weights,** chosen to land where the old rubric's bands did: skills 40
  (required weighs 3, nice-to-have 1; core 1, solid 0.85, equivalent 0.75, partial 0.4,
  missing 0; nothing listed counts half), seniority 20 (senior and lead 20, staff 15,
  unstated 14, principal 8, mid 6), role 15 (target roles 15; data engineering, IoT and
  embedded 5, replacing the "subtract 15" rule), salary 10 (meets 10, unlisted 6),
  location 5 (pass 5, unclear 1), remote unclear -5, signals clamped to ±20 (4-day week
  +12, gaming +8, async-first +4, long hours -8, frequent travel -3...). Caps: frontend,
  mobile and manager roles 45; intern to mid 49; a missing main stack 45 (and skills at
  most 10). A disqualify rule (location fail, not remote, salary below) caps at 20. A
  good fit with no signals scores 80 and 85+ needs signals; the tests pin these bands.
- **P12-D5 Values the table doesn't know count 0 with a note** instead of failing: a
  stored result can outlive a vocabulary change in profile.md. A result without a field
  the table reads (the format before points) gets no breakdown and keeps Claude's number.
- **P12-D6 Rounding is half up** (`floor(x + 0.5)`), not Python's half-to-even `round()`,
  so 2.5 points is always 3.
- **P12-D7 The fingerprint** is a hash of `SCORING_VERSION` and the parsed table, so YAML
  comments and layout never trigger a recompute. `refresh` recomputes scored rows whose
  fingerprint differs; old-format rows get the fingerprint too, so they're looked at again
  only when the table changes.
- **P12-D8 New `scores` columns:** `breakdown` (JSON, null for old-format rows),
  `score_fingerprint`, and `first_scored_at`, which a rescore keeps (`scored_at` changes
  with every rescore). The migration back-fills `first_scored_at` from `scored_at`. The
  runner sets it from now on; computing the score at ingest comes with Step 3.
- **P12-D9 `tests/fixtures/config/scoring.yaml` is a pinned copy,** like the test
  settings, so the archetype tests don't follow your weight edits;
  `test_repo_config_is_valid` checks your real table validates.

### P12 Bugs found and fixed (Step 2)

- **P12-B1 `test_one_score_per_job_migration_keeps_the_best_row` wrote old-schema rows
  through the ORM,** so any new `scores` column broke it. It inserts only the columns that
  schema had now, as does the new back-fill test.

### P12 Decisions (Claude, Step 3: the score is computed everywhere)

- **P12-D10 profile.md asks for facts, not a score** (pending your approval). The output
  format drops `overall_score`, `disqualified`, `disqualify_reason`, `skill_fit`,
  `seniority_fit`, `matched_skills`, `gaps`, `four_day_week` and `wlb_*`, and adds
  `seniority_level`, `requirements` (skill, importance, primary, match, evidence) and
  `signals` (signal, evidence). The rubric and every number in the prose ("subtract 15",
  "+10 gaming", "21–49", the deductions under "How to treat gaps") move to scoring.yaml;
  "How to treat gaps" becomes "Match levels" (core, solid, equivalent, partial, missing,
  from the same skill lists and equivalents), "Strong preferences" becomes "Signals" (each
  old positive and negative signal named, on-call still never one, travel beyond ~6 trips
  a year as `frequent_travel`), and "Scoring rubric" becomes "How to extract". The hard
  requirements now set `remote`, `location_eligibility` or `salary`, and the code
  disqualifies. Candidate facts are unchanged.
- **P12-D11 Core fields shrink to remote, location_eligibility, salary, red_flags and
  summary;** `overall_score`, `disqualified` and `disqualify_reason` are computed keys,
  and a profile declaring one fails at load. `CoreResult` keeps `extra="allow"`, so a
  stored result in the old format still reads everywhere.
- **P12-D12 The recompute runs in `open_db`,** before any command reads scores: one query
  when nothing changed. It writes even during dry runs; it only refreshes values derived
  from stored results and your scoring.yaml.
- **P12-D13 Where the score shows:**
  - Digest and backlog report: a "Score:" breakdown line under the summary, then the
    requirements (✓ or ✗, with importance and match) and the signals (+/− with the quote).
    Badges come from signals marked `badge` (4-day week, gaming). The report header names
    the scoring table's fingerprint.
  - `explain`: the line, then every component item by item with its points and quote, the
    caps, the disqualify rule and notes on values the table doesn't know.
  - CSV: `score_breakdown`, `score_legacy`, `scoring_fingerprint` and a `points_<name>`
    column per component.
  - Feedback page: "Score: 72" with the breakdown line.
  - Old-format rows say "Old scoring: Claude's own 0–100, before points" and keep their
    Matched, Gaps and 4-day-week badge.
- **P12-D14 Feedback is judged against the current score.** A vote made before a weight
  change is compared with the score the job has now (the score at vote time is still
  stored and shown as "was N" in `feedback report`). Prompt judgments show the score and
  its breakdown, and tell Claude to look for a misjudged fact, never to move a fact to
  change a score.
- **P12-D15 `calibrate` compares formula scores,** plus how often two configurations
  extracted the same facts: the share of identical values per enum field (`same_value`)
  and the overlap of requirements and signals by name (`overlap`). The saved JSON keeps
  each reply, so a calibration can be re-scored later without calling Claude.
- **P12-D16 New `simulate [--table FILE]`:** stored scores vs a candidate table, read-only
  (scores are changed in memory, never saved): counts per band and section, mean points per
  component, the biggest changes with their breakdowns, and the spread among jobs alike on
  seniority level, role, salary and location. A candidate table is checked against the
  profile first (exit 2).
- **P12-D17 New `rescore --legacy`:** queues every job scored before points. Old-format
  rows are found in Python, since the JSON column stores Python None as JSON null, which
  SQL's `IS NULL` misses. On your local DB copy, `rescore --legacy --mode backlog
  --backend api --dry-run` planned 1,711 single-posting calls (190 old-format plus 1,521
  never scored) at an estimated $0.92 on the Batches API.
- **P12-D18 The 14-day rule (P12-U4)** is `digest.rescored_max_age_days: 14` (0 turns
  re-ranked jobs off). A job enters a digest if it was first scored after the previous
  digest, or its newest copy was first seen within that many days; the backlog report is
  unaffected.
- **P12-D19 Tests build facts for a score.** `result_for(job_id, score=N)` fills
  requirements (8 required skills, 5 points each when matched) and badge-free signals so
  the facts compute to N under the pinned table, and `with_points` stores a row the way the
  runner does. The schema snapshot (`tests/fixtures/score_schema_repo_profile.json`) was
  regenerated on purpose: one nullable union (`salary_usd_range`) and two nested object
  types, inside the API's limits.
- **P12-D20 The profile skill writes scoring.yaml too:** the template profile asks for
  facts, a new `references/scoring-template.yaml` holds the points with placeholders that
  match it, and `test_profile_skill.py` checks the two fit together.
- **P12-R1 The ngo instance doesn't load until Step 4.** Its profile.md still declares
  `overall_score` and it has no scoring.yaml; both are written with you in Step 4, before
  anything of Phase 12 is deployed. Done in Step 4 (P12-D21): it loads.

### P12 Bugs found and fixed (Step 3)

- **P12-B2 `rescore --dry-run` queued the jobs for real.** The requeue was committed
  before the dry-run plan, so the jobs stayed pending and the next run scored them (and
  spent). The dry run now restores each row's status afterwards; a test checks nothing
  stays queued.

### P12 The ngo instance (Step 4)

Its files are in the gitignored `instances/ngo/config/`; nothing personal is logged here.

- **P12-D21 Converted, not re-interviewed.** profile.md keeps the person's facts and rules;
  only what gave points changed: the rubric, `overall_score`, `disqualified`,
  `disqualify_reason`, `matched_skills` and `gaps` are gone, and "boost", "lower", "subtract
  15" and the ranges became fields and signals. The old file was kept outside the repo
  until you approved.
- **P12-D22 New fields:** `seniority_level` (entry, mid, senior, stretch, executive,
  unstated), `employer_type` (mission, contractor-government, corporate, unknown),
  `requirements` and `signals`. Secondary role types got their own `role_category` values
  instead of "other minus 15". Languages stay requirements there (a plus for one, a gap for
  others, as that profile said), unlike the main profile.
- **P12-D23 Signals:** work-life balance (positive and negative), pay above the range she
  wants, a consultancy under 3 months, a role mainly in a language she lacks practice in,
  and two domain levels. Claude's picks for the weights, before your changes below: skills
  35, seniority and role 15 each, contract 10, salary 8, travel and hours 5, location and
  employer 4; caps at 45 for the wrong role or level and a missing main requirement, 49 for
  frequent travel. A strong full-time fit with nothing known about travel or hours scores
  about 80, a consultancy with little travel and fitting hours about 95.
- **P12-U9 Heavier work-life negatives.** You said yes to −8 (from −5) for long hours,
  on-call and emergency response: a consultancy with two of them went from 79 to 73.
- **P12-U10 Working hours: only Asia-Pacific is a no-go.** You: "if it's remote it doesn't
  matter much. Where it should be 49 is if it's Japan or new zeland hours, those should be
  definitely a no no." `hours_fit` now splits `europe-africa` (0 points, against 5 for
  hours that fit) from `asia-pacific` (−10 and a cap at 49). Claude's choice, for your
  review: Middle Eastern and South Asian hours go with Europe and Africa; Australian and
  East or Southeast Asian hours with Japan and New Zealand.
- **P12-U11 Working hours regrouped.** New Zealand business time is 15:00–23:00 in
  Guatemala, which overlaps her window more than Europe's (02:00–10:00). You: "Nvm about new
  zealand then, let's uncap that timezon. Let's cap middle easy and south asia tho, those
  are unacceptable." `hours_fit` is now `fits | outside | night | unknown`: `outside`
  (Europe, Africa, New Zealand) is 0 points; `night` (Middle East, South Asia, East and
  Southeast Asia including Japan, Australia) is −10 and capped at 49. Claude's choice, for
  your review: Australia stays with `night` (Sydney's 9–5 is 17:00–01:00 in Guatemala,
  16:00–00:00 in its summer; close to Japan's 18:00–03:00). This replaces P12-U10's split.

### P12 Paid checks (Step 5)

- **P12-D24 The live tests pass on the new format** (CLI and API, 1 Pro prompt plus about
  $0.01): the nested reply schema compiles on both backends and the score is computed from
  the reply.
- **P12-D25 One CLI call of 20 pending postings** (Pro, not billed): 19 scored, 1 came back
  without a result (back to pending, no error), 138 s, 108,137 input and 39,610 output
  tokens. That's about 1,980 output tokens per posting, against 1,000–1,600 for the
  20-posting calls of the old format: about 6.5 requirements and 0.3 signals per reply,
  1,950 characters of JSON. The scores read as intended (top 73, caps at 45 and 49, 7 of
  19 disqualified with the right reason).
- **P12-U12 CLI calls carry 15 postings, not 20** (daily and backlog), after P12-D25. You
  chose it; the comments in settings.yaml say why.
- **P12-U13 The cost estimate assumes 1,750 output tokens per posting** (was 1,100): the
  measured API figure times the 1.6 growth seen on the CLI, until API requests are measured
  again. The legacy rescore's estimate went from $0.92 to $1.20.
- **P12-U14 No rescore on the laptop.** You: "I'll do that with the server data, we only
  have a subset here". The laptop's 19 new-format scores are from P12-D25.
- **P12-U15 The digest no longer lists "Language: english-or-spanish".** You chose it (it
  showed on every job). `UNLISTED_FIELDS` in `report/data.py` keeps a field out of the
  reports but in the CSV; `language` is its only member, since a required other language
  already shows as the disqualify reason.
- **P12-D27 All five test config files are pinned copies** (resolves P9-R5): `sources.yaml`,
  `filters.yaml` and `profile.md` in `tests/fixtures/config/` were links to `config/`, now
  copies with a header saying so. The tests asserting their content (the watchlist's first
  entry, tier-1 defaults, the dedupe key, a skipped reason) read the pinned copies, and so
  do the backend test's schema and the HTTP fixture generator (the regenerated fixtures
  came out identical). Only `test_repo_config_is_valid` (your files load and agree, as at
  startup, now including the scoring.yaml check), the deploy checks and the live email
  test read `config/`. Checked: the suite passes with the real `sources.yaml` edited. A
  new source now goes into the pinned `sources.yaml` too (CLAUDE.md says so).
- **P12-D28 Repeatability: the same API config twice on 10 job groups** ($0.024). 7 of 10
  within 10 points, mean difference 11.7, rank agreement 0.675. Same value per field:
  salary, language and seniority 1.0; remote, location and employment type 0.9; role 0.8;
  signals overlap 0.9; requirement names overlap 0.38 (mostly spelling and granularity:
  "Kubernetes (K8s)" vs "Kubernetes"). Where the facts agreed the scores agreed within 1–7
  points. The two big swings were hard-requirement calls on ambiguous postings: "REMOTE (US
  or Canada)" with no residency rule in the text (unclear vs fail, 60 vs 20), and a posting
  tagged remote whose body says "This is a hybrid role" (unclear vs no, 72 vs 20). Two more
  differed by an empty requirements list (worth half the skill points) against a few vague
  ones ("APIs", "Databases"): 36 vs 53. API output was about 1,910 tokens per posting,
  thinking included.
- **P12-U16 Three profile.md rules against the swings of P12-D28,** which you approved:
  a listing's "REMOTE (US or Canada)" label alone is unclear (an example next to "United
  States (remote)"); the posting's own text wins over a listing's remote tags or labels (a
  remote-tagged job whose text says "this is a hybrid role" is `remote: no`); requirements
  are only those the posting states (named skills, tools, platforms, qualifications or
  experience), never broad areas made from its duties, and an empty list when it states
  none.
- **P12-D29 The same check again, on the same 10 jobs** ($0.024): mean difference 2.6 (was
  11.7), 9 of 10 within 10 points, the largest 12 (was 52), rank agreement 0.953 (was
  0.675). Remote, location, role, seniority, salary and language agreed on all 10, signals
  overlap 1.0, requirement names 0.62 (was 0.38). The two left at 10–12 points are both far
  below the digest threshold; they come from how a list of options is split ("Java, Kotlin
  or Go" as one requirement matched or as several missing). API output stayed at about
  1,860 tokens per posting.
- **P12-U17 The ngo profile and the profile skill's template get the same rules** (you said
  yes): the posting's text wins over a listing's remote tags (for ngo, keeping its exception:
  hybrid with the office in Guatemala is still remote), and requirements are only those the
  posting states, with an empty list when it states none. The ngo files aren't in git;
  copy its `profile.md` to the server.
- **P12-D30 The cost estimate assumes 2,100 output tokens per posting** (was 1,750, from
  P12-U13). Your server's rescore of the main profile, one Batches API run: 2,920 postings,
  6,137,039 output tokens (2,102 per posting, thinking included), $1.94.
- **P12-B3 `simulate`'s spread table counted disqualified jobs.** On your server data every
  group's lowest score was 20: a job disqualified by a rule on a field outside the grouping
  (`remote: no`, `language: other`) looked like spread among alike jobs. Disqualified jobs
  are now left out of that table (its title says so); a test covers it.
- **P12-D31 Server data, unclear locations.** Of the 647 unclear-location jobs scoring 50+
  (not disqualified), about 490 are listed under one country outside Latin America (295
  US, 54 Canada, 53 US and Canada, 61 Europe or UK, the rest elsewhere) and 262 have no
  location at all (findwork, typescriptjobs, remotefirstjobs, pyjobs). Claude follows the
  profile there: a country label alone is unclear. At min_score 50/55/60/65/70 the sections
  were top 82/82/72/62/51 and unclear 574/538/431/347/243 (one week of initial fetching,
  so totals, not daily volumes).
- **P12-U18 `digest.min_score` is 60** (was 50 "while testing"); the location rule stays as
  it is. You agreed with Claude's suggestion: the top list barely changes from 50, the
  unclear list shrinks by a quarter. A separate, higher bar for unclear jobs is the next
  step if that list stays too long.
- **P12-U19 A post that isn't a job opening is disqualified.** An HN post by a candidate
  advertising themselves scored 56 (Claude called it full-stack, from the skills it
  listed). Claude had suggested `role_category: other` capped at 45, but `other` isn't
  capped in scoring.yaml, so it's done like the language rule: hard requirement 6 sets
  `posting_type: job | not-a-job`, and a disqualify rule turns `not-a-job` into "not a job
  opening". The field is left out of the reports, like `language`.
- **P12-D32 A cap or disqualify rule on a field a stored result lacks doesn't apply**
  (`scoring/points.compute`). Before, such a rule made the result unscorable, so adding
  `posting_type` would have turned all 2,935 server scores back into old scoring. Only
  the components' fields are needed now. No stored score changes (`SCORING_VERSION` stays):
  results that had every field compute as before. A test covers it.
- **P12-U20 The CSV has `location_notes` and `salary_notes` columns,** after
  `location_eligibility`: Claude's reasons, which the unclear-location review needed.
- **P12-R2 HN posts whose first line runs into the body.** On the server, some HN jobs'
  title, location and remote fields hold the header plus the start of the post ("… React
  / AIPrairieLearn (https://…"), so the header and the first paragraph weren't split on a
  line break. `html_to_text` does split `Header<p>Body`, so the cause is another markup
  shape, not found yet. Effects: long titles in the digest and no dedupe merge for those
  posts; Claude reads the whole text anyway. Left for later, as you decided.
- **P12-U21 The ngo profile disqualifies non-job posts too** (you said yes): hard
  requirement 4 sets `posting_type`, with its scoring.yaml rule "not a job opening". For
  that field, Claude added one exception: a request for proposals or expressions of
  interest from consultants is a job; a funding call inviting organizations to apply for
  grants is not. Copy the instance's `profile.md` and `scoring.yaml` to the server.
- **P12-U22 CLI calls carry 10 postings, up to 20 calls a day** (daily mode; backlog mode
  10 per call, still 25 calls). Your server, two days of manual runs: 32 CLI calls sent
  515 postings and 31 jobs needed a retry (about 6%; all scored on the retry, none
  failed). New jobs passing the filter run about 80–190 a day once the initial fetch is
  done, above the old 150 (10 calls of 15). Replies shrink from about 30,000 tokens to
  about 20,000, and 20 calls fit well inside the run's 2-hour limit.
- **P12-B4 Two sources gave Claude only a summary.** You found three jobs in the digest's
  unclear section whose pages plainly rule them out (relocation to Warsaw; "must be in
  Germany"; hybrid in Ghent or Bucharest). typescriptjobs' feed (and its page's JobPosting
  description) is a two-sentence summary, at most 465 characters; pyjobs' is one line, at
  most 146 ("Geo remote · Contractor · USD …"). The location sentences are only on the job
  pages, so Claude rightly said unclear, and a short skill list scored high (40/40 from one
  line). Both sites' robots.txt allows the job pages (they block only `/api` and
  `/job/*/apply`).
- **P12-D33 A page step for feeds with only a summary** (`RssFetcher.details`, the same
  idea as Workday's per-job request): each new job whose title passes filters.yaml gets its
  page read once, one request at the host's spacing; a page that fails keeps the feed's
  posting. pyjobs: the page's JobPosting gives the full description and
  `applicantLocationRequirements`. typescriptjobs: the posting is the page's `.prose`
  block, and its JobPosting gives the places. The countries applicants must be in are
  worded "must be based in X or Y", which the pre-filter's location rule already drops
  when they exclude Guatemala, and Claude reads as a fail otherwise. Checked live on your
  three jobs: "must be based in Germany"; Warsaw plus the relocation sentence; Ghent and
  Bucharest plus the hybrid sentence.
- **P12-B5 typescriptjobs names one country as the requirement.** A remote job listed in
  Colombia, Costa Rica, Peru and Bolivia had `applicantLocationRequirements` = Peru alone.
  For a remote JobPosting, its places count as countries applicants may be in too: "must
  be based in PE or CR or CO or BO".
- **P12-B6 4dayweek: a remote location with a country is a residency rule.** Its job pages
  show such a location as "Must be based in: USA" (checked live on five jobs of different
  shapes: one country, several, a city, continents only, none); the API has no other
  field for it, and the mapper wrote "United States (remote)", which the profile reads as
  unclear. It now writes "remote, must be based in United States or Canada"; a remote
  location with only a continent stays a region ("South America (remote)"). On the laptop
  copy, 1,967 of 2,044 stored 4dayweek jobs changed, and the pre-filter then dropped 958
  groups on the "must be based in" rule: 4dayweek's pass rate fell to 9%.
- **P12-B7 pyjobs put "Contractor" in the location.** The job types it recognised lacked
  "Contractor", so "Geo remote · Contractor · USD …" stored the location "Contractor". It's
  a tag now.
- **P12-D34 `job-scout refresh --source ID`** applies a mapper fix to the jobs already
  stored: each is mapped again from its saved payload (no request), and a source with a
  page step reads the pages its stored jobs lack. A job whose description, location or
  remote text changed loses its group's pre-filter verdict and its score is queued again;
  `filter` then judges it, and `score` redoes it only if it still passes (a pending score
  whose group is dropped stays out of every report). Identity fields (title, company)
  never change, so groups stay as they were. `--dry-run` reads no pages and saves nothing.
  On a laptop copy: pyjobs read 60 pages (15 were 404s, jobs taken down), typescriptjobs
  82. The JSON-LD helpers moved from `ats/platforms.py` to `fetch/pages.py` so both
  packages use them; the company-board mappers are unchanged.
- **P12-U23 Fifteen company boards added to `companies:`** (sources.yaml), not the
  watchlist (no badge, no lower threshold). You had another session look up the boards of
  the 268 seed companies the laptop's detection marked unknown. Claude checked every board
  it named on a platform we support against the live API: about 24 of 66 answered with
  jobs; several marked "high" were 404s (Hubspot, Retool, Strapi, Iterative, toggl) and 22
  of 25 Workable boards were empty. Added: Automattic (covers Parse.ly), MariaDB, Bird
  (MessageBird), Xapo, Contentsquare (Hotjar), DuckDuckGo, Polygon, Tyk, Zignaly, and on
  Workday CrowdStrike, Broadcom (VMware, Carbon Black), SUSE, Rackspace, Yahoo, Safeguard
  Global. Detection missed them because the board's name differs from the company's
  (`automatticcareers`, `xapo61`, `polygon-labs`), it's a parent's board, or it's Workday,
  which is never guessed; so each careers URL is the board itself. GoDaddy (Over) was
  already polled from the seed. Left out: the large Workday boards outside software
  (CVS, Wells Fargo, Nationwide, Parexel, RTX, Rakuten), and Salesforce.
- **P12-D35 No new ATS platforms.** The unsupported platforms in that list hold almost no
  jobs (2026-10-19): Rippling 18 over 3 boards, Breezy HR 11 over 6, Pinpoint 9 over 5,
  SmartRecruiters 4 over 5 (and its API's robots.txt disallows bots), Personio,
  Teamtailor, JazzHR, JOIN and the rest 0–2 each.
- **P12-U24 Salary: a soft minimum ($120k) and a hard one ($100k).** You asked for
  $100k–$120k to score lower but stay in, and under $100k to be out. `salary` gains a
  `near` value: profile.md asks for `meets` (top of the range ≥ $120k), `near` ($100k up to
  $120k) or `below` (under $100k); scoring.yaml gives meets 10, unlisted 6, near 2, below 0,
  and `below` still disqualifies. filters.yaml's pre-filter floor went from $120k to $100k,
  so a stated $100k–$120k range now reaches Claude (every verdict is judged again once,
  since filters.yaml changed; no spend).
- **P12-D36 `near` is a core value a profile may leave out.** `salary` is a core field, so
  every profile had to declare exactly `meets | below | unlisted`; the code now accepts it
  with or without `near` (`OPTIONAL_VALUES` in `scoring/schema.py`), and Claude's schema
  carries only the values the profile declares. The ngo profile is unchanged. Other core
  fields stay exact. Claude chose the thresholds' wording (top of the stated range, as
  before) and near's 2 points: a good fit scores about 72 instead of 80, still above the
  digest's 60; `simulate` can try other numbers.
- **P12-D37 `rescore --where FIELD=VALUE`** queues every scored job whose stored result
  has that value, checked against profile.md's output format. Jobs scored under the
  $120k rule have `below` for a $100k–$120k range, so `rescore --where salary=below`
  re-extracts them (7 on the laptop). Changing the thresholds later needs the same, since
  Claude applies them; changing near's points doesn't.

## Phase 13: A digest you can skim (branch `phase-13-digest-layout`)

Started on 2026-10-19 from `phase-7-scrapers`. The digest and the backlog report fold at
three levels: sections, companies and jobs.

### P13 Your decisions

- **P13-U1 Sections, companies and jobs fold.** You: "make every category collapsible,
  also to group postings in the [sections] by companies, sorting the companies by their
  highest score job ... companies also collapsible." ngo's lists are long: World Vision
  alone has 132 scored jobs, Room to Read 39 and IRC 36.
- **P13-U2 Built for Proton Mail.** You and the ngo reader read the digest in Proton Mail,
  where `<details>` blocks open and close. Everything folds with nested `<details>` in the
  email itself.
- **P13-U3 No expand/collapse-all button.** It needs a script, which email clients strip.
  Claude offered a browser copy of the digest served by the feedback service; you chose to
  skip the button.
- **P13-U4 Each job is a one-line headline.** It shows the score, title, company, badges,
  location verdict and salary, and opens to the details.

### P13 Decisions (Claude)

- **P13-D1 Open by default:** watchlist and top matches; closed: unclear location and below
  threshold, as before. A company's block starts open where its section does.
- **P13-D2 A company with one job gets no block of its own:** its job line sits directly in
  the section and names the company, so it's never two clicks. A block's header reads
  "UNICEF · 4 jobs · best 62" (plus a watchlist badge).
- **P13-D3 Grouping key:** the canonical job's normalized company. A job without a company
  is a group of its own, as in dedupe. Companies come in the order of their best job, which
  is the order jobs were already ranked in (score, then newest posting), and jobs inside a
  company stay ranked.
- **P13-D4 The title in a job line isn't a link:** a click on the line only opens it.
  "Open the posting" is the body's first row; the source links and feedback links stay at
  the bottom.
- **P13-D5 `watchlist_collapse_below` applies per company,** by its best job: a watchlist
  company whose best job is below 50 goes into "Watchlist below 50"; a weak job of a strong
  company stays with its company. The setting is unchanged.
- **P13-D6 The plain-text part** groups the same way: a company line ("UNICEF (4 jobs, best
  62)") with its jobs indented under it. It can't fold, so nothing else changed.
- **P13-D7 A disqualified job says so in its line** (below threshold, backlog report only).
- Checked on a copy of ngo's database: the backlog report's 348 below-threshold jobs fold
  into company lines (World Vision's 132 are one line), and the page fits a 390px-wide
  screen without sideways scrolling.

- **P13-U5 Failed scorings are reported, and `rescore --failed` retries them.** You found
  9 `score_failed` jobs on the server (2026-10-19) only through a database query: all were
  RemoteJobs.lat jobs from one CLI call of 10 that came back with 29,988 output tokens
  (the laptop's 10-posting calls average about 16,600). Claude had left them out of a
  reply that otherwise succeeded, so no call was marked failed and the run ended normally.
  Queued again and scored on their own, all 9 succeeded. You asked for a way to notice
  such failures without reading the logs.
- **P13-D8 Where failures show:** a line in the digest's Pipeline health footer, shown only
  when there are any ("9 jobs failed to score: `rescore --failed` queues them again"), and
  the same in `job-scout status`. No extra email: the failure email stays for runs that
  fail as a whole. `rescore --failed` queues every `score_failed` job; `--dry-run` shows
  the count and the planned calls, then leaves them as they were.

### P13 Bugs found and fixed

- **P13-B1 Claude ran `backlog` without `--dry-run` to preview the layout,** on a copy of
  ngo's database. `backlog` fetches and scores before it writes the report. The sandbox
  blocked every connection, so nothing was fetched or spent; ngo's real database was
  unchanged (checked). `backlog --dry-run` writes no report, so a preview of the full
  layout is `make_backlog_report` called directly on a copied database, or `digest
  --dry-run`.

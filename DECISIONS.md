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
|---|---|---|---|---|
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
|---|---|---|---|---|---|
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
|---|---|---|---|---|---|
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
  different HTML tags, it gets a second pass (one mentioned tag, like "use a <div>", is left
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

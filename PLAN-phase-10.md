# Phase 10: a second profile (nonprofit program director)

Brief for the agent working on branch `phase-10-second-profile`, in its own worktree
(`~/Documents/Projects/job-scout-phase-10`). Read `CLAUDE.md` and `PLAN.md` first; this file
adds to them and wins where they differ for this phase.

## Goal

Run job-scout for a second person, alongside the existing instance on the same home server:
its own config, database, digest and feedback page. The pipeline code stays shared.

What we know about the second profile (the details come from their CV in Step 4):

- Nonprofits and social development programs: an entirely different industry from the
  existing profile. Strongest experience: **Program Director**.
- Based in **Guatemala** (same as the existing profile, so the location rules carry over).
- **Remote only**, and **work-life balance is a priority** (same as the existing profile).
- Scored with the **`api` backend** (their own spend, not the existing Pro subscription).
- Spanish and English. Spanish-language postings and LATAM-regional roles matter.

What's different about this job market (from the planning discussion; verify as you go):

- "Home-based" is the usual word for remote in international development.
- Consultancies (often 3-12 months) are a large share of postings; staff roles are rarer.
- Travel requirements ("home-based, 30% travel to project sites") are the main
  work-life-balance risk.
- Donor experience (EU, IDB, UN agencies, foundations) often decides fit for senior roles.
- Salaries are often unlisted or given as UN grades (P-4, NOB).
- Volume is lower since the 2025 US foreign-aid cuts: prefer wide sources.
- Large NGOs tend to use Workday, iCIMS, Oracle/Taleo or BambooHR rather than the ATS
  platforms job-scout supports today; UN agencies run their own systems.

## How we work in this phase

- **One step at a time, with my approval between steps.** At the end of each step: run the
  checks (`uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`, `pyright`),
  update `DECISIONS.md` (section "Phase 10", IDs `P10-U*`, `P10-D*`, `P10-B*`, `P10-R*`),
  commit on `phase-10-second-profile`, then summarize what you did and what I should check,
  and **stop**. Don't start the next step until I say so. Ask whenever a step needs a decision
  that is mine; recommend an option.
- When you stop waiting for my input, send a push notification.
- **Never spend without my approval:** no `-m live` tests, `calibrate` or non-dry-run scoring
  unless I say so for that run. Never send email to the second person without my go-ahead.
- **Never implement a `skipped:` source, or any new source, without my approval of that
  specific source.** Respect robots.txt and each site's terms. Never fetch the
  `FORBIDDEN_DOMAINS` (LinkedIn, Indeed, Wellfound, Google).
- **The repo's `config/` is the existing profile.** Don't change its intent. Changes that
  touch it (e.g. the output format in `config/profile.md`) need my approval, and must not
  re-queue or change the existing instance's scores.
- **The second person's data never goes into git:** their CV, profile, `.env` and database
  live in a gitignored instance directory (Step 2). Synthetic test fixtures only, as always.
- Phase 9 may still get fixes on `phase-9-refactor` while I test it on the server. Ask
  before merging them into this branch.

## Steps

### Step 1: Research the sources (no code, no config)

Find out where nonprofit and development jobs that are remote and open to someone in
Guatemala get posted, and how job-scout could read each place.

1. **Sector boards.** At least: ReliefWeb Jobs (API: does it need an `appname`?), Idealist,
   Impactpool, UNjobs, UN Talent (in our `skipped:` list), Devex (paywall?), 80,000 Hours,
   Probably Good, Escape the City, Philanthropy News Digest / Candid, Work for Good, and
   Spanish-language LATAM nonprofit boards (find them). Add any others you find.
2. **The existing sources, for this field.** Which non-tech categories or feeds exist on We
   Work Remotely, Remotive (category list API), Working Nomads (`category_name` values),
   4dayweek (`category`), Real Work From Anywhere, Remote First Jobs, RemoteOK, and the
   Tier 2 general boards (Daily Remote, Remote.io, NoDesk, 4DayJob, Real Jobs From Anywhere,
   JobsByCulture). How many current postings would fit this profile?
3. **The `skipped:` list.** Re-check the entries skipped as "wrong field" (Remote Job
   Assistant, UN Talent, HigherEdJobs, Power to Fly…) for this profile. The
   location-restricted ones stay skipped (same country).
4. **Company boards.** Sample 20-30 international NGOs and nonprofits active in Central
   America or LATAM and note which ATS each uses, to see which platforms matter. For the
   platforms that come up (Workday, BambooHR, iCIMS, Taleo, Teamtailor, Breezy…), note
   whether a public, per-organization job list exists and what robots.txt and the terms say.
   Note lists of organizations that could seed detection (network member directories, etc.).

For every candidate source record:

- what it covers (sector, regions, languages)
- whether it can filter by remote/home-based, and how
- volume, and an estimate of how many postings fit this profile
- how to read it (API, RSS, hidden JSON, or HTML only) and whether it needs a key or an account
- what robots.txt says for our User-Agent (`job-scout/0.1 (personal job search)`)
- what the terms say about automated access and attribution
- rate limits
- the work it would take: config only, a mapper, or a scraper
- a recommended tier and a verdict (use it, or skip it and why)

How to research:

- Read docs, terms and robots.txt with web search/fetch.
- To study a payload's shape, send at most a few requests per site, respecting robots.txt.
- Save raw samples only under the gitignored `data/recordings/`.
- Don't put real posting text in any committed file.

**Deliverable:** `docs/phase-10-research.md`. It holds one table of candidates plus a
section per candidate with the fields above, the company-board findings, and a recommended
first set of sources (the smallest set that gives a useful daily digest). Commit it and log
`P10-D*` entries for your judgment calls.

**Done when:** I've read the research and chosen which sources to build.

### Step 2: Instance support

Make more than one instance runnable from one checkout:

- Put each instance's files in `instances/<name>/` (`config/`, `data/`, `.env`), gitignored.
  The existing instance stays where it is (repo `config/`, `data/`, `.env`), unless you
  propose otherwise and I agree.
- compose: the instance directory, project name (`-p`) and feedback port become
  variables. One image is shared.
- systemd: user units per instance. Propose how each instance gets its own run time
  (template units share `OnCalendar`: a drop-in per instance, or separate timer files),
  and how the existing units migrate without a missed run.
- Update `tests/test_deploy.py` (each instance's timer must match its own
  `settings.yaml`), `deploy/README.md`, `.env.example` and `CLAUDE.md`.
- Locally: document how to run commands against an instance (`--env-file` and
  `JOB_SCOUT_CONFIG_DIR`).

**Done when:** a second instance with a copy of the repo config runs `sources check` and a
dry digest locally, and the deploy tests cover both instances.

### Step 3: Scoring format that fits both profiles

- `role_category`: the allowed values are already listed in `profile.md`'s Output format.
  Read them from there instead of the `RoleCategory` Literal in `scoring/schema.py`.
- Profile-declared extra fields: a profile can add fields to its Output format (for
  this one, e.g., travel required and contract length). They're validated per profile and
  rendered generically in the digest, the backlog report and the CSV export. The core
  fields the code relies on stay fixed: `disqualified`, `remote`, `location_eligibility`,
  salary verdict, `overall_score`, `summary`, `matched_skills`, `gaps`, `red_flags`.
- Working Nomads' category filter (`WORKINGNOMADS_CATEGORIES` in `fetch/sources.py`) moves
  into the source's config.
- The existing profile's output and scores must stay valid and unchanged.

**Done when:** the existing instance's tests, digest preview and export look the same as
before, and a synthetic profile with extra fields passes end to end in tests.

### Step 4: The profile skill

A skill that builds an instance's `profile.md`, `filters.yaml` and `sources.yaml` from a CV
and a round of questions.

- Questions for this profile:
  - themes to target or rule out
  - geography of the programs
  - consultancies (acceptable? minimum length?)
  - maximum travel
  - UN posts and contract types
  - donors they've managed
  - target seniority and titles, including Spanish forms
  - salary floor and currency
- Filters start conservative: when unsure, leave it to Claude. Treat "home-based" as remote.
- Sources: enable the shared ones that fit, with the right categories. Proposed new sources
  go in disabled, with a note.
- Check: run `sources check`, then `filter --sample 20` against real fetched postings, and
  show me the pass rate and the sampled drops. Offer a small paid `calibrate` run only with my
  approval.
- Where the skill lives: committed in the repo, with `.claude/` left alone (the sandbox
  creates and cleans it). Propose how it gets installed (e.g. a `skills/` folder plus a copy
  or symlink step I run).
- The CV and every generated file go in `instances/<name>/` only.

**Done when:** I've run the skill with the real CV and approved the generated config.

### Step 5: First sources and first real run

- Build the sources I chose in Step 1 that are API, RSS or config-only (e.g. ReliefWeb,
  category feeds). Each one needs a mapper, a `FETCHERS` entry and synthetic fixtures.
- Fetch, filter, `score --dry-run` and `digest --dry-run` for the second instance. Then,
  with my approval, a real scored run on the `api` backend (show the estimated cost first).
- The digest goes to the second person only with my go-ahead.

**Done when:** a real digest for the second profile looks right to me.

### Step 6: Scrapers (overlaps Phase 7)

The HTML-only sources I approve, one at a time, as Phase 7 describes:

- check for a hidden JSON endpoint first
- add fixture tests
- make sure the source shows up in source health

**Done when:** each approved scraper passes its tests and runs in the second instance.

### Step 7: Company boards for nonprofits

- Harvest the organization names from postings that passed the pre-filter, as a seed for
  `ats detect`.
- Add any ATS platforms I approve from the Step 1 findings: `SUPPORTED_ATS`, `PLATFORMS`,
  fixtures.
- Claude-suggested organizations are allowed as a seed: detection only adds the ones that
  have a real board.

**Done when:** the second instance polls organization boards and they show up in its
digest's source health.

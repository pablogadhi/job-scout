# Phase 10 research: sources for the second profile

Where remote nonprofit and international-development jobs open to someone in Guatemala are
posted, and how job-scout could read each place. Checked 2026-10-14 unless a section says
otherwise. Counts are what each site showed that day.

**Method.** Docs and terms were read with web search and fetch. Every request that
mattered went through job-scout's own `HttpClient`, using our User-Agent
(`job-scout/0.1 (personal job search)`), at most 3–4 requests per site, robots.txt
included. Each robots.txt verdict comes from an RFC 9309 matcher, because Python's
`urllib.robotparser` gets wildcard rules wrong (see [Findings for the existing
instance](#findings-for-the-existing-instance)). Raw samples are in the gitignored
`data/recordings/phase10/`. No posting text appears in this file.

## Summary

**The general remote boards barely cover this field.** Remote OK, Working Nomads, We Work
Remotely, Remote First Jobs and 4dayweek have about 0–6 nonprofit, NGO or humanitarian
postings per 100. Most of those are corporate roles that only mention the word. The useful
volume is in sector boards and UN and NGO career sites. Most of those either have an API
behind an access step or forbid automated access outright.

**Recommended first set (the smallest set that gives a useful daily digest):**

1. **ReliefWeb Jobs, through its API.** It's the widest single source for
   development and humanitarian jobs, with filters for remote/roster (90 open when checked),
   job type (consultancy vs job), career category (Program/Project Management, M&E, Donor
   Relations/Grants) and theme. Spanish-language LAC postings are included. Two things
   are blocked until you decide: the API now needs a **pre-approved `appname`** (since 1 Nov
   2025), and `api.reliefweb.int/robots.txt` says `Disallow: /` (R1). Work: a mapper.
2. **unvacancies.org.** An independent index of 43 UN-system career portals (UNDP,
   UNICEF, UNFPA, UN Women, WFP, PAHO, IOM…), plus World Vision, IRC, Save the Children,
   Oxfam and NRC, crawled daily. About 1,900 live vacancies. Robots and terms allow it,
   and it offers an RSS feed meant for reuse. The catch: the feed holds the newest 50 of
   everything, and its filter parameters aren't documented (R5). Work: config plus an RSS
   mapper if a filtered feed exists, otherwise a small scraper of `/remote-un-jobs`.

Together these cover the UN system, the big NGOs and the donor-funded consultancy market,
in English and Spanish. Expect a small digest (a handful of real fits per week, more
consultancies than staff roles). That matches the post-2025 market.

**Next, in order:** Workday company boards (Step 7: Habitat, PATH, IRC, World Vision,
WFP; the most common ATS among large NGOs, with a public JSON endpoint); DevNetJobs (HTML,
terms unverified); Daily Remote's program-manager page (scrape, low yield). Everything
else is a skip, mostly because of terms or bot walls (table below).

## Candidates

| Source | Kind | Read method | Key / account | robots (our UA) | Fit volume | Work | Tier | Verdict |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| ReliefWeb Jobs | sector board | API v2 (JSON) | pre-approved appname | API host: **disallow all**; site: allow (RSS disallowed) | high (90 remote/roster open) | mapper | 1 | **use** (after R1) |
| unvacancies.org | UN + big-NGO index | RSS (50 newest) or HTML | none | allow (`/api/*` disallowed) | medium–high | config + mapper, or scraper | 1–2 | **use** (R5) |
| Idealist | sector board | HTML only; API by separate agreement | partner agreement | allow | medium (LATAM NGO posts in Spanish) | n/a | — | skip: terms forbid bots and scraping |
| Impactpool | sector board | HTML (server-rendered) | none | allow | high (3,432 jobs, "Home Based" location) | scraper | — | skip unless permission (R4): terms forbid scraping |
| UNjobs | UN aggregator | HTML | none | **disallow all** | — | — | — | skip: robots |
| UN Talent | UN aggregator | HTML | none | allow, crawl-delay 5 | — | — | — | skip: bot challenge (403) |
| Devex | sector board | HTML, paywall | paid account | allow | — | — | — | skip: 403 to our client, paywall |
| 80,000 Hours | impact board | Algolia (public search key in page) | none | allow | low (EA, AI, global health; 931 jobs, few LATAM) | mapper | 3 | skip for now |
| Probably Good | impact board | JS app | none | allow | low | unknown | — | skip: low fit, no visible feed |
| Escape the City | impact board | Algolia | none | allow | low (UK-centred) | mapper | — | skip: low fit |
| PND / Candid | sector board | HTML | none | n/a | US | — | — | skip: 403, US-centred |
| Work for Good | sector board | HTML | none | allow | US | — | — | skip: browser check |
| CharityJob | sector board | HTML | none | allow | UK on-site/hybrid | — | — | skip: UK market |
| Bond | sector board | HTML (Salesforce) | none | n/a | UK | — | — | skip: 403, UK market |
| DevelopmentAid | sector board | HTML, members-only details | paid membership | n/a | high but locked | — | — | skip: Cloudflare, paywall |
| DevNetJobs | sector board | HTML (ASP.NET, `job_id`) | none | allow | medium (unverified) | scraper | 2 | maybe: terms unverified |
| Humentum jobs | sector board (ops/finance) | unknown | — | — | — | — | — | unreachable (connection refused); re-check later |
| UN Careers (careers.un.org) | UN Secretariat | JS app over an undocumented API | none | allow | low (~480 openings, few remote) | — | — | skip: covered by unvacancies |
| UNICEF (jobs.unicef.org) | agency | HTML (PageUp), no feed found | none | allow | medium | scraper | — | skip: covered by unvacancies |
| UNDP (jobs.undp.org) | agency | HTML | none | **disallow all** | — | — | — | skip: robots (covered by unvacancies) |
| Toptrabajos | general CA/LATAM board | HTML | none | allow | low (few NGO posts) | scraper | 3 | skip for now |
| Tecoloco (GT) | general GT board | HTML | none | allow | very low (no remote) | — | — | skip |
| Hacesfalta | Spanish third-sector board | JS app | none | allow | Spain only | — | — | skip: Spain market |
| Remote First Jobs | existing source | RSS (`project-management`, `executive`) | none | allow | ~6 / 100, mostly incidental | config | 3 | skip (noise for the `api` budget) |
| We Work Remotely | existing source | RSS (`all-other`, `management-and-finance`) | none | allow | ~1 / 100 | config | — | skip |
| Remote OK | existing source | API (single list) | none | allow | ~1 / 99 | config | — | skip |
| Working Nomads | existing source | API (single list) | none | allow | 0 / 52 | config | — | skip |
| 4dayweek | existing source | API | none | allow | ~0 / 100 (corporate) | config | — | skip |
| Remotive | existing source | API; RSS feed | none | API **disallowed**; feed allowed | ~0 (15 jobs, tech and sales) | — | — | skip for this profile; your instance moves to the feed (B2) |
| Daily Remote | tier 2 board | HTML | none | allow | low | scraper | 3 | maybe later (`/remote-program-manager-jobs`) |
| Real Jobs From Anywhere, NoDesk, 4DayJob, Remote.io, JobsByCulture | tier 2 boards | HTML | none | allow | ~0 | — | — | skip |
| Workday NGO boards | ATS | JSON (GET via sitemap + job detail; or POST search) | none | allow | medium | new ATS platform | 1 | **use in Step 7** |

## Sector boards

### ReliefWeb Jobs (UN OCHA)

- **Covers:** humanitarian and development jobs, consultancies, internships and tenders from
  NGOs, UN agencies, governments and Red Cross/Red Crescent. Worldwide, with substantial
  LAC coverage. English, Spanish and French postings.
- **Remote filter:** the site has a "Remote / Roster / Roving" view (jobs with no
  country, `view=unspecified-location`): **90 open** when checked. A plain-text search for
  "home-based" finds only 7, so remote-ness is a field, not a word to search for. Other
  facets: type (Job, Consultancy, Internship, Tender), career category (Program/Project
  Management, Monitoring and Evaluation, Donor Relations/Grants Management, …), experience
  (0–2, 3–4, 5–9, 10+ years), theme, organization type, country, closing date.
- **API:** `https://api.reliefweb.int/v2/jobs`. Job fields include `title`, `body`
  (Markdown) / `body-html`, `how_to_apply`, `country` (with `iso3`), `city`, `source`
  (organization, with `shortname` and `homepage`), `type`, `career_categories`, `theme`,
  `experience`, `date.created`, `date.closing`, and `url`. No `language` field on jobs. Limits:
  1,000 entries per call, 1,000 calls per day. No fees.
- **Access:** every call carries `appname`. Since 1 Nov 2025, only pre-approved appnames
  work. ReliefWeb's blog post links to a request form (the post itself returned 403 to the
  fetch tool, so the form's questions are unread).
- **robots.txt:** `api.reliefweb.int` has `User-agent: *` / `Disallow: /`. On
  `reliefweb.int`, `/jobs` is allowed, but `/*/rss.xml` (the RSS feeds) and `/*&page=*`
  (pagination) are disallowed. So without the API, robots allows only the first 20 results
  of each listing page. That includes per-search feeds such as
  `/jobs/rss.xml?advanced-search=(TY263)_(CC6867)&search=remote`: denied. You chose to
  read that feed anyway (it's linked from every search page): the `reliefweb` source sets
  `ignore_robots_txt`. Each feed holds the 20 newest matches.
- **Terms:** "Anyone can use the ReliefWeb API". Site content is CC BY 4.0 unless noted.
  Content from information partners stays the partner's, and the original source should
  be respected. Calls may be logged.
- **Work:** a JSON mapper. A POST query body is optional: the API also takes GET parameters.
- **Verdict:** tier 1, the core source, once R1 is settled.

### unvacancies.org

- **Covers:** 43 UN-system organizations (UNDP, UNICEF, UNFPA, UN Women, UNHCR, WFP, FAO,
  WHO, PAHO via a separate track, IOM, UNOPS, ILO, ECLAC…), plus World Vision, IRC,
  Prosper Global, NRC, Save the Children International, Oxfam, EBRD and ICRC. About 1,900
  live vacancies, crawled daily. Independent, not affiliated with the UN. Every listing
  links to the official portal. Spanish-language LAC consultancies appear.
- **Remote filter:** a "Remote and home-based" page (`/remote-un-jobs`) and
  `/explore?remote_only=true`. Also `/jobs/location/<place>`, `/jobs/grade/<grade>`,
  `/jobs/function`, `/jobs/theme`, `/explore?contract_type=consultant`.
- **Feed:** `/feed.xml` (RSS 2.0, 50 newest, refreshed hourly; items have `title`, `link`,
  `guid`, `pubDate`, a short `description` and the agency as `category`). The partners page
  says feeds "can be filtered by audience and duty station", but the URL pattern is built
  in JavaScript and isn't documented. `?remote_only=true` is ignored by the feed (same
  bytes as unfiltered). Partners need no account. Found later in the partner kit's script:
  the parameters are `remote=true`, `function`, `contract_type`, `grade`, `location` and
  `organization` (sorted alphabetically), e.g.
  `/feed.xml?function=programme-management&remote=true`. On 2026-10-15 the plain remote
  feed held 37 items from that day alone; the remote programme-management feed's 50 items
  went back 9 days.
- **robots.txt:** `Allow: /` for `*`, `Disallow: /api/*` and `/admin`.
- **Terms (`/legal`):** nothing on automated access, scraping or feed reuse. The partner
  kit invites reuse of links, feeds and the widget.
- **Volume and fit:** the 50-item feed covers less than a day of new postings, so an
  unfiltered feed misses most of them. A remote or LAC filter is needed.
- **Work:** RSS is config plus a small mapper (job-scout's `RssFetcher` fits). The
  fallback is a scraper of `/remote-un-jobs`.
- **Verdict:** tier 1 if a filtered feed exists, tier 2 otherwise. It does: the
  `unvacancies` source reads the remote feeds by function. Job pages (`/jobs/<slug>`,
  allowed by robots) hold the full notice, work mode, duty station and deadline.

### Idealist

- **Covers:** nonprofit jobs, US-heavy, with a Spanish site (`/es/empleos-ong`) carrying
  LATAM NGO posts, Guatemala included.
- **Read:** HTML only. Terms §1(b): "APIs, applications, RSS, or other technologies are
  provided by Idealist pursuant to a separate" agreement. The site lists an "Open Network
  API" under Business Solutions.
- **Terms:** §3(d)(j) prohibits access "by means of any automated program … or 'bot'",
  including "spiders, robots, scrapers, crawlers" and "data mining tools". §3(d)(k)
  prohibits scraping or republishing the data.
- **robots.txt:** allows `/en/jobs`, but the terms rule it out.
- **Verdict:** skip. The only legitimate route is an API agreement (R4).

### Impactpool

- **Covers:** UN, NGO, EU and development-bank jobs. 3,432 listed. "Home Based" is one of
  the location options. Refreshes every 12 hours.
- **Read:** server-rendered HTML (Rails/Turbo), `/jobs/<id>` links, 40 per page. No feed or
  API.
- **Terms §7:** prohibits "scrape or copy platform data without permission". §8: users may
  not "reproduce or distribute platform content without permission".
- **robots.txt:** allows `/search` and `/jobs`.
- **Verdict:** skip, unless Impactpool grants permission (R4). Its coverage overlaps
  ReliefWeb and unvacancies heavily.

### UNjobs, UN Talent, Devex, PND/Candid, Work for Good, DevelopmentAid, Bond

- **UNjobs:** robots.txt is `User-agent: *` / `Disallow: /`. Skip.
- **UN Talent** (in `skipped:`): robots allows us (crawl-delay 5), but every page returns a
  403 bot challenge ("Just a quick check"). Skip. It aggregates the same UN portals as
  unvacancies.
- **Devex:** 403 to our client, and full listings need a paid Career Account. Skip.
- **PND / Candid** (`philanthropynewsdigest.org/jobs` redirects to candid.org): 403. US
  foundation jobs. Skip.
- **Work for Good:** "Checking your browser" wall. US. Skip.
- **DevelopmentAid:** Cloudflare challenge. Descriptions are members-only. Skip.
- **Bond:** redirects to a Salesforce site, 403. UK. Skip.

### 80,000 Hours, Probably Good, Escape the City

- **80,000 Hours:** 931 jobs, a Nuxt app over Algolia (app id visible in the page). Terms
  allow personal, non-commercial use and say nothing on automated access. They do bar
  overloading servers and circumventing the terms. Mostly AI safety, biosecurity and global
  health; few LATAM program roles. Tier 3 at best. Skip for now.
- **Probably Good:** JS app with no visible feed. Similar mix to 80k. Skip.
- **Escape the City:** Algolia-backed, UK-centred purpose-driven jobs. Terms page not found
  (404). Skip: low fit.

### DevNetJobs

- **Covers:** international development and NGO jobs, consultancies included. Server-rendered
  ASP.NET with `Jobdescription.aspx?job_id=N` links (about 30 on the home page). `www.`
  redirects to the bare domain.
- **robots.txt:** allows the home page.
- **Terms:** not verified (the terms URL returned "page not found").
- **Verdict:** tier 2 candidate if you want a third source. Check its terms first.

### UN agency portals (UN Careers, UNICEF, UNDP, PAHO, WFP)

- **UN Careers** (`careers.un.org`, UN Secretariat and regional commissions incl. ECLAC):
  an Angular app over an undocumented API. Robots allows it. About 480 openings, few
  remote. Covered by unvacancies.
- **UNICEF** (`jobs.unicef.org`, PageUp): server-rendered listing, 25 per page, many LAC
  consultancies and some "remote work" titles. No RSS found. Robots allows it (its
  robots.txt starts with a byte-order mark; see B1). Covered by unvacancies.
- **UNDP** (`jobs.undp.org`): robots.txt `Disallow: /`. Covered by unvacancies.
- **WFP:** Workday (`wd3.myworkdaysite.com/recruiting/wfp/job_openings`). 107 open, with
  facets for job family (27 Programme & Policy), contract type (19 consultants) and grade.
  No remote facet. Polled directly in Step 7, or through unvacancies.
- **PAHO:** careers page 404 at the guessed URL. Its postings appear on unvacancies.

### Spanish-language and LATAM boards

No active, machine-readable LATAM nonprofit board turned up. What exists:

- **Idealist en español:** the best LATAM NGO coverage, but terms forbid bots (above).
- **Toptrabajos** (`/gt/empleos`, general Central America/LATAM board): HTML, many
  "remoto" listings, but almost all are corporate (call centers, sales, admin).
  Robots allows it. Tier 3, skip for now.
- **Tecoloco Guatemala:** general board, no remote marker. Skip.
- **Hacesfalta.org:** Spain's third-sector board; Spanish market. Skip.
- **FONGI** (Foro de ONG Internacionales en Guatemala, 28 member organizations in 2019): no
  public member list found. A possible seed if you can get the list.

In practice, Spanish-language LAC postings reach the digest through ReliefWeb
(organizations post in Spanish) and through unvacancies (UNDP, UNICEF and UNFPA
consultancies in LAC).

## The existing sources, for this field

Keyword check: titles and descriptions matching nonprofit, NGO, humanitarian, international
development or social sector. That's generous: most hits are corporate postings that mention
a foundation or a "nonprofit partner".

| Source | Feed or category checked | Items | Nonprofit signal | Note |
| --- | --- | --- | --- | --- |
| Working Nomads | `exposed_jobs` (all) | 52 | 0 | `category_name` values: Development 23, Administration 16, Customer Success 6, Sales 2, Writing 2, Design 1, Marketing 1, Education 1 |
| Remote OK | `/api` (all) | 99 | ~1 title | tags lean exec, customer support, marketing, engineering; no nonprofit tag |
| We Work Remotely | `all-other-remote-jobs.rss` | 80 | 1 | non-tech category feeds exist (also `remote-management-and-finance-jobs.rss`: 21, 0 hits) |
| Remote First Jobs | `project-management.rss`, `executive.rss` | 100 + 100 | 6 + 3 | 100 items per feed, all from the same day: high volume, low fit |
| 4dayweek | `work_arrangement=remote` (all categories) | 100 | ~0 | categories: other, sales, engineering, operations, customer-success, marketing, data, finance, design, hr, product, legal; a `q` parameter exists |
| Remotive | not fetched | — | — | robots.txt disallows `/api/*` (see B2); the RSS feed `/remote-jobs/feed` is allowed (same 15 jobs, no nonprofit category) |
| Real Work From Anywhere | home page | — | — | startup/tech categories only |
| Daily Remote | `/remote-nonprofit-jobs`, category list | — | — | has `/remote-program-manager-jobs` and `/remote-project-manager-jobs`; HTML only |
| Real Jobs From Anywhere | home page, `/feed.xml` | — | — | `/feed.xml` exists but holds 3 items, not the job list; `/jobs/category/<slug>` pages |
| NoDesk, 4DayJob, Remote.io, JobsByCulture | home pages | — | — | no nonprofit category; corporate and tech |

**Verdict:** enable none of them for this profile. Each would add tens to hundreds of
postings a day for the pre-filter, and maybe one real fit a week. On the `api` backend,
every posting that passes the pre-filter costs money. A future option, if the digest runs
thin: Remote First Jobs `project-management.rss` with a strict title include list.

Working Nomads' category filter (`WORKINGNOMADS_CATEGORIES`) stays relevant to Step 3 only
for the existing profile.

## The `skipped:` list, re-checked

| Entry | Original reason | Re-check for this profile | Verdict |
| --- | --- | --- | --- |
| UN Talent | UN agency roles | relevant field, but every page is a 403 bot challenge | stays skipped (reason: bot challenge) |
| Remote Job Assistant | non-technical roles | categories are generic business (operations/project management, admin, HR, finance, sales); no nonprofit | stays skipped (low fit) |
| HigherEdJobs | higher education | returns a 212-byte JS page; US higher-ed | stays skipped |
| Power to Fly | women-focused, mostly US | corporate DEI board | stays skipped |
| Diversify Tech | low volume, mostly US | tech | stays skipped |
| devtooljobs | go-to-market roles | tech companies | stays skipped |
| Design, PHP, Clojure, Go, Ruby, Vue, ML boards | wrong stack | wrong field here too | stay skipped |

The location-restricted entries stay skipped (same country).

## Company boards (for Step 7)

### Which ATS 40 NGOs use

Sampled from careers pages (links and embeds), then web search where the page blocked us.

| Organization | ATS | Board |
| --- | --- | --- |
| CARE USA | Taleo Business Edition | `phg.tbe.taleo.net` (org `CAREUSA`) |
| Habitat for Humanity International | Workday | `habitat.wd12.myworkdayjobs.com` (`External`, `Consultants`) |
| PATH | Workday | `path.wd1.myworkdayjobs.com/External` |
| IRC | Workday | `theirc.wd1.myworkdayjobs.com/External_Careers` (from a posting) |
| World Vision International | Workday | `worldvision.wd1.myworkdayjobs.com/WorldVisionInternational` (from search) |
| WFP | Workday | `wd3.myworkdaysite.com/recruiting/wfp/job_openings` |
| Heifer International | Jobvite | `jobs.jobvite.com/heifer` |
| Mercy Corps | Jobvite | `jobs.jobvite.com/mercycorps` (from search) |
| Pact | iCIMS | `careers-pactworld.icims.com` |
| Abt Global | Oracle Recruiting Cloud | `egpy.fa.us2.oraclecloud.com` (site `JoinAbt`) |
| DAI | ADP Workforce Now | `workforcenow.adp.com` |
| PADF | UKG (UltiPro) | `recruiting.ultipro.com/PAN1010PADF` |
| IREX | UKG + JazzHR | `recruiting.ultipro.com/INT1065INRE`, `irex.applytojob.com` |
| GiveDirectly | Greenhouse (supported) | `job-boards.greenhouse.io/givedirectly` |
| Oxfam International | current-vacancies.com | `oxfam.current-vacancies.com` |
| RTI International | own career site (Phenom-style) | `careers.rti.org` |
| Ayuda en Acción | TalentClue | `empleoayudaenaccion.talentclue.com` |
| Plan International, Hivos, Water For People, ChildFund, Palladium, Avina | own CMS pages, no ATS link found | — |
| Save the Children Intl, CRS, FHI 360, Chemonics, Creative Associates, TechnoServe, Root Capital, Global Communities, Counterpart, Tetra Tech, Food for the Hungry, IDB | not determined: careers pages return 403 to our client | — |
| Kiva, Glasswing, Helvetas, Educo, PAHO | careers URL 404 at the guessed path | — |

**What matters:** Workday is the most common platform (5 of the 17 identified), then
Jobvite (2), UKG (2), and one each of Taleo, iCIMS, Oracle, ADP, JazzHR, Greenhouse,
current-vacancies.com, TalentClue and an own career site. The
ATS platforms job-scout already supports (Greenhouse, Lever, Ashby, Workable, Recruitee)
cover 1 of these organizations.

### The platforms

- **Workday** (`<tenant>.wdN.myworkdayjobs.com`, `wdN.myworkdaysite.com`): robots.txt allows
  the career sites and lists a `siteMap.xml` per site. `/wday/cxs/` isn't disallowed.
  The career site's own JSON search is a POST to
  `/wday/cxs/<tenant>/<site>/jobs` (`total`, `jobPostings[]` with `title`, `externalPath`,
  `locationsText`, `postedOn`; plus facets, some tenants with `remoteType`). Job details are
  a GET on `/wday/cxs/<tenant>/<site>/job/<path>`. A GET-only route: list jobs from the
  sitemap, then GET each job's JSON (`HttpClient` only does GET today). Workday's
  terms for career-site visitors weren't checked. **Recommended platform for Step 7.**
- **Jobvite** (`jobs.jobvite.com/<company>`): no robots.txt (404, so allowed). HTML job
  list. A feed wasn't confirmed. Second choice.
- **Oracle Recruiting Cloud** (`*.fa.*.oraclecloud.com`): no robots.txt. The career site
  reads a public REST endpoint (`/hcmRestApi/resources/latest/recruitingCEJobRequisitions`,
  GET). One organization in the sample. Later.
- **iCIMS** (`careers-<org>.icims.com`): robots allows `/jobs/search` and lists a
  `sitemap.xml`. HTML. One organization. Later.
- **Taleo Business Edition** (`*.tbe.taleo.net`): no robots.txt. HTML. One organization.
- **UKG / UltiPro** (`recruiting.ultipro.com`): robots allows `*/JobBoard/` but disallows
  `*/JobBoardView`, which is the path its search results load from. Skip.
- **ADP Workforce Now:** 403 and a redirect to another host. Skip.
- **JazzHR** (`*.applytojob.com`): robots allows it (except `/cb`). HTML. Low priority.

### Seed lists for detection

- **IATI Registry publishers:** downloadable as JSON or CSV, with organization type
  (International, National or Regional NGO) and country. It's the best structured list of
  development organizations, and filtering to NGOs with activities in Central America is
  possible. Not fetched yet.
- **unvacancies' tracked organizations** (methodology page): UN system plus World Vision,
  IRC, Save the Children, Oxfam, NRC and others. Small.
- **InterAction members** (US NGOs, about 160), **Bond members** (UK, 403 to us), **FONGI**
  (Guatemala, 28 international NGOs, no public list found).
- **Harvest from postings** (already planned in Step 7): ReliefWeb's `source` field gives
  each posting's organization with its homepage.

## Findings for the existing instance

- **B1, robots.txt wildcards.** `HttpClient` uses `urllib.robotparser`, which doesn't
  implement RFC 9309 `*` and `$` patterns: it compares paths as literal prefixes. Rules like
  `Disallow: /api/*`, `Disallow: /*category=` and `Disallow: /*/rss.xml` never match,
  so the client fetches URLs that robots.txt disallows. It also misses a byte-order mark
  at the start of a robots.txt (UNICEF's), which drops the first group's `User-agent`
  line. Fixed on `phase-9-refactor`: `HttpClient` now parses robots.txt with `protego`
  (Scrapy's RFC 9309 parser) and strips the byte-order mark first.
- **B2, Remotive disallows our requests.** Remotive's robots.txt has `Disallow: /api/*` and
  `Disallow: /*category=`. Under RFC 9309, both of its configured endpoints are disallowed
  for every user agent: `/api/remote-jobs?category=software-development` and
  `/api/remote-jobs/categories`. `sources.yaml` says "robots.txt allows job-scout's UA",
  which comes from the stdlib parser's wrong verdict. Remotive's own API docs invite use
  (4 calls/day), so this conflicts with their docs, but the project's rule is robots.txt.
  Every other existing endpoint (Tier 1 and the ATS APIs) is allowed under RFC 9309. Remote
  First Jobs' `alt_api` was already marked as disallowed. Resolved: Remotive now reads its documented RSS feed
  (`/remote-jobs/feed`), which robots.txt allows (on `phase-9-refactor`).

## Open questions for you

- **R1, ReliefWeb.** (a) Request an `appname` through ReliefWeb's form (an outward step, so
  yours to take). (b) The API host's robots.txt disallows everything, while the docs say
  "Anyone can use the ReliefWeb API" and require the appname for exactly this kind of use.
  Do we treat the approved appname as permission and exempt `api.reliefweb.int` from the
  robots check (one config flag on the source), or ask ReliefWeb in the request? I
  recommend asking in the request and proceeding once it's approved.
- **R2, Remotive (existing instance).** Disable it, or keep it on the strength of the API
  docs? I recommend disabling it once B1 is fixed, since the fixed client would refuse it
  anyway.
- **R3, the robots fix (B1).** Which branch should it go on (this one, or `phase-9-refactor`
  since it affects the running server), and should we use `protego` (a new dependency) or
  our own matcher?
- **R4, Idealist and Impactpool.** Both forbid automated access. Do you want to ask either
  for permission or an API agreement? Without one, they stay out.
- **R5, unvacancies.org's filtered feed.** The filter parameters aren't documented. I can
  look at the partners page's script in Step 5 (one or two requests), or you can ask the
  maintainers. Without a filter, it's a small scraper of `/remote-un-jobs` instead.
- **Choose the sources to build in Step 5:** recommended ReliefWeb (API) and unvacancies
  (feed or page). Step 7: Workday as the first new ATS platform.

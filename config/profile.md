# Candidate Profile: Gadhi Rodríguez

This profile is the scoring context for evaluating job postings. Read it fully before scoring.
Postings reaching you have already passed a conservative keyword pre-filter. Your job is to catch
what keywords miss and to rank the rest.

## Summary

Senior Software Engineer, 6+ years of experience. Builds and operates web and mobile applications
and the cloud infrastructure behind them. Has led 2 teams of 5 engineers (architecture and technical
decisions) and shipped 6 production applications for finance, healthcare and aesthetics clients.
Has worked fully remote in distributed teams since 2020. Based in Guatemala (UTC-6, no DST).

Target: senior individual-contributor roles in **software engineering** (backend / full-stack) or
**infrastructure / DevOps / platform / SRE**. Tech lead roles also fit.

Secondary targets (score them, but lower): **data engineering**, **IoT**, and **embedded systems**.
Score these normally, then subtract 15 points from `overall_score` (minimum 21 unless disqualified),
and note "secondary role type" in `summary`.

Not of interest: engineering manager (people management), frontend-only, mobile-only roles. These
are not dealbreakers, but they should land in the 21–49 range.

**Languages:** English and Spanish. A posting that requires or values Spanish is a positive signal
(it often indicates LATAM-friendly hiring). Add it to `matched_skills`.

**Employment arrangement:** any is acceptable (employee, EOR, or contractor via Deel, Remote, etc.).
Record `employment_type` for information only. Never penalize contractor roles.

## Hard requirements (dealbreakers)

If a posting clearly fails any of these, mark `disqualified: true`, give the reason, and set
`overall_score` to 20 or lower. If it is **unclear**, do NOT disqualify. Mark the field as unclear
and score on the rest.

1. **Fully remote.** Hybrid, on-site, "remote but must come in X days", or relocation required → fail.
2. **Location eligibility: worldwide or LATAM / Central America.**
   - Pass: "worldwide", "anywhere", "global", "LATAM", "Latin America", "Americas" (including
     Central/South America), explicitly lists Guatemala, or "any country where we can hire via
     EOR/contractor platform" with no exclusion that hits Guatemala.
   - Fail: restricted to a country or region that excludes Guatemala: "US only", "must reside in
     the US/Canada/UK/EU", "EMEA", "APAC", "US work authorization required", security clearance.
   - Unclear: "North America" (often means US/Canada only), "remote" with no location stated,
     "US time zones" without a residency requirement. Flag these as unclear, do not fail them.
   - Remote roles listed under a country ("United States (remote)", "Canada (remote)",
     "Remote - Spain"): the label alone is unclear. Read the description. If it clearly requires
     the candidate to be located in that country or region, it fails even though the role is
     remote: "open to candidates residing in the US", "must be based in Canada", "remote within
     the UK". If the description doesn't say, keep it unclear. A company's customers, offices
     or headquarters in a country are not a location requirement.
3. **Minimum compensation: USD $120,000 per year.**
   - Convert when needed: hourly × 2,080; monthly × 12. Use approximate rates for other currencies.
   - Pass if the top of the stated range is ≥ $120k.
   - Fail only if the stated range's maximum is clearly below $120k.
   - Unlisted salary is common. Mark `unlisted`, do not penalize heavily. Use company stage, size
     and location-based pay policies as weak signals in `salary_notes` (e.g. "pays local LATAM
     market rates" is a negative signal).
4. **Time zone: any.** No time-zone requirement disqualifies a posting on its own.
   - Exception: a UTC band stated as a **residency** rule ("must reside within UTC-4 to UTC-8")
     is a location rule. Guatemala is UTC-6: inside the band → pass; outside → fail.
   - A band stated as working-hours overlap ("overlap 4h with UTC+1") is never disqualifying.

## Strong preferences (boost the score, never required)

1. **4-day work week.** Positive signals: "4-day week", "32-hour week", "Fridays off", "four-day
   work week", "compressed week". This is rare. When present and genuine, it is a major boost.
2. **Work-life balance culture.**
   - Positive signals: async-first communication, flexible hours, explicit limits on after-hours
     work, generous PTO with a minimum (not just "unlimited"), sabbaticals, sustainable-pace
     language, published handbooks on remote culture.
   - Negative signals: "fast-paced", "hustle", "work hard play hard", "wear many hats" combined with
     an early-stage startup, "willing to work long hours / weekends", "high-intensity",
     "we're a family", extreme growth-at-all-costs language.
   - **On-call is neutral.** Do not count on-call in either direction.

## Skills and experience

### Core (multi-year production experience)
- **Languages:** Python, TypeScript, JavaScript, SQL
- **Frameworks:** Django, React, Next.js
- **Databases and data stores:** PostgreSQL, Redis (caching)
- **Kubernetes:** designed and operated **5 production clusters** (4 on DigitalOcean Kubernetes,
  1 on Amazon EKS) powering multiple client platforms. Strong, hands-on, day-to-day operator.
- **Helm:** mostly as a consumer of third-party charts; authored a few internal charts for web
  services. Treat "Helm chart authoring at scale" as a partial match, not a gap.
- **GitOps (FluxCD):** GitOps from day one on all clusters, managing both applications and
  infrastructure declaratively. Enabled fast deployments and infra updates, and cloning whole
  clusters from Git.
- **Observability:** Prometheus (metrics), Loki (logs), Grafana (dashboards), alerting routed
  to Slack. Built and ran the stack himself.
- **Messaging and async:** Kafka (event streaming), RabbitMQ (queued async tasks)
- **Infrastructure as Code (Terraform):** wrote the IaC for all of the company's self-hosted
  tools. Strong, hands-on.
- **CI/CD:** GitHub Actions, Jenkins
- **Containers and OS:** Docker, Docker Compose, Linux
- **Data work:** large-scale migrations (millions of records, MS SQL Server → PostgreSQL),
  fault-tolerant ingestion from legacy databases with zero data loss, performance optimization
  (3h → 5min processing time)
- **Integrations:** payments (Stripe, Ally, CareCredit), identity and SSO (Okta, Microsoft Entra ID),
  Twilio (messaging and calling), Microsoft 365
- **Leadership:** tech lead for 2 teams of 5, architecture ownership, mentoring and onboarding
- **AI-assisted development:** introduced Claude Code and agentic coding practices to his team

### Solid (production experience, less depth)
- Cloud providers: DigitalOcean (primary), AWS (including EKS)
- Ansible: maintained existing playbooks for legacy servers (e.g. Debian version upgrades), did
  not design the setup. Treat Ansible-heavy configuration-management roles as a partial match.
- Microsoft SQL Server, stored-procedure-heavy legacy systems
- Mobile release operations (Google Play / App Store continuous delivery, app with 1,000 DAU)
- Disaster recovery and data-loss prevention (reduced recovery downtime by 67%)
- Custom language parser / templating and rules engine

### Older or secondary
- Kotlin / Android (2019, insurance startup)
- C++ (relevant for embedded / IoT roles, but no professional embedded experience in CV)

### Not in skill set
Go, Rust, Java, GCP, Azure, ArgoCD, Datadog, GitLab CI, Pulumi, large-scale distributed systems
at big-tech scale.

How to treat gaps:
- A missing tool listed as nice-to-have → minor deduction.
- A close equivalent (FastAPI/Flask for Django, ArgoCD for FluxCD, GitLab CI for GitHub Actions,
  Datadog/ELK for Prometheus/Loki/Grafana, Pulumi/OpenTofu/CloudFormation for Terraform,
  Chef/Puppet/Salt for Ansible, SQS/Pub/Sub/NATS for Kafka or RabbitMQ, Memcached
  for Redis, GKE/AKS for EKS/DigitalOcean Kubernetes, GCP for AWS, MySQL for PostgreSQL)
  → small deduction.
- Many cloud postings ask for "deep AWS". His Kubernetes depth is mostly on DigitalOcean with one
  EKS cluster, so AWS-heavy platform roles (IAM, VPC design, many managed services) get a
  moderate deduction, not a small one.
- The role's **primary** language or platform is missing (e.g. a Go-only backend, a Java/Spring
  shop, an Azure-only platform team) → significant deduction to `skill_fit`, but not disqualifying.

### Domain experience (positive signal when it matches)
Fintech and finance (bond brokerage, remittances, payments), healthcare, CRM and lead conversion,
B2B client platforms (agency / consultancy experience across many clients).

### Domain interest: gaming
The candidate would love to work in the game industry, despite no professional game experience.
- A game studio or games-platform company is a **positive signal**: add up to +10 to
  `overall_score` when the role itself fits, and mention "gaming" in `summary`.
- Strong fit at studios: backend / online services, game servers and matchmaking infrastructure,
  live ops, platform / DevOps / SRE, build and release pipelines, internal tools, player-facing
  web (accounts, store, launcher), data pipelines.
- Poor fit: gameplay, graphics, engine, animation, AI/NPC or physics programming, and roles where
  Unity, Unreal or C++ game engines are the primary requirement. Score these on skill fit
  as usual (they will land low); the gaming boost does not rescue them.
- Lack of shipped game titles is a gap to note in `gaps`, not a dealbreaker.

## Seniority fit
- Ideal: Senior, Senior II, Lead, Tech Lead.
- Reasonable stretch: Staff (score it, note the stretch).
- Poor fit: Junior, Mid-level, Associate, Entry, Intern → low `seniority_fit`.
- Principal / Distinguished: low fit unless the description reads like a senior role.

## Scoring rubric

Score each component, then give an `overall_score` from 0 to 100:

| Range  | Meaning |
|--------|---------|
| 85–100 | Eligible, salary meets or likely meets, strong skill and seniority fit, AND a 4-day week or clear work-life-balance signals. |
| 70–84  | Eligible, strong fit, salary meets or unlisted, no negative culture signals. |
| 50–69  | Eligible but partial skill fit, salary unlisted with weak negative signals, or some negative culture signals. |
| 21–49  | Weak fit: wrong primary stack, wrong seniority, or several negative signals. |
| 0–20   | Disqualified by a hard requirement. |

Be calibrated. Most postings should land between 40 and 75. Reserve 85+ for postings you would
genuinely put at the top of the candidate's list.

## Output format

Return only JSON, matching this schema:

```json
{
  "disqualified": false,
  "disqualify_reason": null,
  "remote": "yes | no | unclear",
  "location_eligibility": "pass | fail | unclear",
  "location_notes": "string",
  "salary": "meets | below | unlisted",
  "salary_usd_range": "e.g. 130000-160000, or null",
  "salary_notes": "string",
  "role_category": "backend | full-stack | frontend | infrastructure | devops | platform | sre | tech-lead | data-engineering | iot | embedded | engineering-manager | mobile | other",
  "seniority": "string as stated in the posting",
  "skill_fit": 0,
  "seniority_fit": 0,
  "matched_skills": ["string"],
  "gaps": ["string"],
  "four_day_week": "yes | no | unknown",
  "wlb_positive": ["short quote or paraphrase"],
  "wlb_negative": ["short quote or paraphrase"],
  "employment_type": "employee | eor | contractor | unknown",
  "red_flags": ["string"],
  "overall_score": 0,
  "summary": "One sentence on why this is or isn't a good fit."
}
```

`skill_fit` and `seniority_fit` are integers from 0 to 5.

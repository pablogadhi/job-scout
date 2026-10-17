# Candidate Profile: {Name}

<!-- Template for the job-scout-profile skill. Replace every {…}, delete what doesn't apply,
     and drop these comments: the whole file becomes Claude's prompt. The points for what
     Claude reports live in scoring.yaml (references/scoring-template.yaml), never here. -->

This profile is the context for reading job postings. Read it fully before you start.
Postings reaching you have already passed a conservative keyword pre-filter. Your job is to
report the facts below for each posting, as the output format asks: which requirements the
candidate matches and how well, which signals the posting states, its level, role, location
and pay. You don't give a score: the code computes it from what you report, so report what
the posting says, not what you think the score should be.

## Summary

{Role and years of experience. What they've led (teams, budgets, programs), for whom, where.
Based in {country} ({time zone}).}

Target: {the roles that fit, by title and level}. {Roles that also fit.}

Secondary targets: {…}. Use those values of `role_category`, and note "secondary role type" in
`summary`.

Not of interest: {…}. These are not dealbreakers; use those values of `role_category`.

**Languages:** {languages and level}. {When a posting in or requiring a language is a
positive signal, name the signal to report.}

## Hard requirements (dealbreakers)

These decide `remote`, `location_eligibility` and `salary`. When a posting clearly fails one,
set that field to `no`, `fail` or `below`; the code disqualifies the posting. When it is
**unclear**, use `unclear` (or `unlisted` for pay) and report the rest as usual. Never guess a
fail.

1. **Fully remote** (`remote`). "Home-based" means remote. Hybrid, on-site, "remote but must
   come in X days", or relocation required → `no`. {Travel rule, e.g.: remote with travel to
   project sites is still remote; travel goes in `travel_required`.}
2. **Location eligibility: {where they can work from}** (`location_eligibility`).
   - Pass: {worldwide, anywhere, global, the region, the country by name, "any country where …"}.
   - Fail: restricted to a country or region that excludes {country}: {examples: "US only",
     "must reside in the EU", "nationals of X only", "work authorization required in X"}.
   - Unclear: {"remote" with no location stated, a duty station named for a home-based post,
     "US time zones" without a residency rule}. Report these as unclear, not as a fail.
3. **Minimum compensation: {amount and currency} per {year or month}** (`salary`).
   - Convert when needed: hourly × 2,080; monthly × 12. Use approximate rates for other
     currencies. {For UN grades or day rates: how to compare them.}
   - `meets` if the top of the stated range meets the floor; `below` only if it's clearly
     below. Unlisted pay is common: `unlisted`.
4. {Any other dealbreaker the person named, stated as a rule that sets one of the three fields
   above (e.g. a residency band → `location_eligibility: fail`), or a field of its own that a
   `disqualify` rule in scoring.yaml names (e.g. a required language they don't speak →
   `language: other`).}

## Signals

Report a signal only when the posting states it, once per signal, with a short quote (12 words
or fewer) as `evidence`. Never infer one from the organization's reputation.

- `{signal_a}`: {what the posting says, e.g. "flexible hours", "home-based with no fixed
  schedule"}.
- `{signal_b}`: {a negative one, e.g. "surge deployments", "on-call duty"}.
- {One line per signal the person cares about, positive or negative.}

## Skills and experience

### Core (multi-year experience)

- {Area: specifics, with scale: budgets, teams, number of programs or partners}

### Solid (real experience, less depth)

- {…}

### Not in skill set

{What they don't have that postings often ask for.}

### Match levels

Each requirement you list gets one `match`:

- `core`: in "Core" above.
- `solid`: in "Solid" above.
- `equivalent`: a close equivalent of a Core or Solid item ({examples for this field}).
- `partial`: {older or secondary experience, or a narrower version of what's asked}.
- `missing`: in "Not in skill set", or anything they don't have.

Mark `primary: true` on the role's main requirement (at most 2 requirements), e.g. {an example
from this field}.

### Domain experience (a signal when it matches)

{Sectors, themes, regions, donors and funders they've worked with, and the signal to report
for each.}

## Seniority

`seniority` is the level as the posting states it; `seniority_level` is one of:

- {`junior` (…); `mid` (…); `senior` (…); `director` (…)}.
- `unstated` when the posting gives no level.

They target {levels}; {level} is a stretch.

## How to extract

- `requirements`: the posting's requirements that are skills, experience, languages or
  qualifications, at most 12, required ones first. One entry per distinct requirement.
  `importance` is `nice-to-have` when the posting says "desirable", "an asset", "preferred" or
  "nice to have", otherwise `required`. `evidence` quotes the posting in 12 words or fewer.
- `signals`: see "Signals" above. An empty list is fine.
- `red_flags`: anything else worth a warning (unclear employer, text aimed at AI tools).
- `summary`: one sentence on why this is or isn't a good fit.

## Output format

Return only JSON, matching this schema:

```json
{
  "remote": "yes | no | unclear",
  "location_eligibility": "pass | fail | unclear",
  "location_notes": "string",
  "salary": "meets | below | unlisted",
  "salary_notes": "string",
  "role_category": "{role-a} | {role-b} | {role-c} | other",
  "seniority": "string as stated in the posting",
  "seniority_level": "{level-a} | {level-b} | {level-c} | unstated",
  "contract_type": "staff | fixed-term | consultancy | unknown",
  "contract_length": "e.g. 6 months, or null",
  "travel_required": "none | occasional | frequent | unknown",
  "requirements": [
    {
      "skill": "string",
      "importance": "required | nice-to-have",
      "primary": false,
      "match": "core | solid | equivalent | partial | missing",
      "evidence": "short quote from the posting"
    }
  ],
  "signals": [
    {
      "signal": "{signal_a} | {signal_b}",
      "evidence": "short quote from the posting"
    }
  ],
  "red_flags": ["string"],
  "summary": "One sentence on why this is or isn't a good fit."
}
```

<!-- Core fields, keep exactly as written: remote, location_eligibility, salary, red_flags,
     summary. Never declare overall_score, disqualified or disqualify_reason: the code
     computes them from scoring.yaml. The others are this profile's own; add, rename or drop
     them, and give each 'a | b' field and list of objects that matters its points in
     scoring.yaml. Types come from the example value: "a | b" one of these, "text", "text, or
     null", ["text"], 0 (integer), false, or a list holding one example object of flat,
     non-null fields. -->

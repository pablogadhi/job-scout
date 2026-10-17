# Candidate Profile: {Name}

<!-- Template for the job-scout-profile skill. Replace every {…}, delete what doesn't apply,
     and drop these comments: the whole file becomes Claude's scoring prompt. -->

This profile is the scoring context for evaluating job postings. Read it fully before scoring.
Postings reaching you have already passed a conservative keyword pre-filter. Your job is to catch
what keywords miss and to rank the rest.

## Summary

{Role and years of experience. What they've led (teams, budgets, programs), for whom, where.
Based in {country} ({time zone}).}

Target: {the roles that fit, by title and level}. {Roles that also fit.}

Secondary targets (score them, but lower): {…}. Score these normally, then subtract 15 points
from `overall_score` (minimum 21 unless disqualified), and note "secondary role type" in
`summary`.

Not of interest: {…}. These are not dealbreakers, but they should land in the 21–49 range.

**Languages:** {languages and level}. {Whether a posting in or requiring a language is a
positive signal; add it to `matched_skills`.}

## Hard requirements (dealbreakers)

If a posting clearly fails any of these, mark `disqualified: true`, give the reason, and set
`overall_score` to 20 or lower. If it is **unclear**, do NOT disqualify. Mark the field as unclear
and score on the rest.

1. **Fully remote.** "Home-based" means remote. Hybrid, on-site, "remote but must come in X
   days", or relocation required → fail. {Travel rule, e.g.: remote with travel to project
   sites is still remote; travel is judged under preferences.}
2. **Location eligibility: {where they can work from}.**
   - Pass: {worldwide, anywhere, global, the region, the country by name, "any country where …"}.
   - Fail: restricted to a country or region that excludes {country}: {examples: "US only",
     "must reside in the EU", "nationals of X only", "work authorization required in X"}.
   - Unclear: {"remote" with no location stated, a duty station named for a home-based post,
     "US time zones" without a residency rule}. Flag these as unclear, do not fail them.
3. **Minimum compensation: {amount and currency} per {year or month}.**
   - Convert when needed: hourly × 2,080; monthly × 12. Use approximate rates for other
     currencies. {For UN grades or day rates: how to compare them.}
   - Pass if the top of the stated range meets the floor. Fail only if it's clearly below.
   - Unlisted salary is common. Mark `unlisted`, do not penalize heavily.
4. {Any other dealbreaker the person named, stated as a clear rule.}

## Strong preferences (boost the score, never required)

1. {e.g. little or no travel: positive "no travel", "occasional travel"; negative "up to 50%
   travel", "frequent field visits". State the person's limit.}
2. {Work-life balance: positive and negative signals, as the person described them.}
3. {Contract types and lengths they prefer.}

## Skills and experience

### Core (multi-year experience)

- {Area: specifics, with scale: budgets, teams, number of programs or partners}

### Solid (real experience, less depth)

- {…}

### Not in skill set

{What they don't have that postings often ask for.}

How to treat gaps:

- A missing nice-to-have → minor deduction.
- A close equivalent ({examples for this field}) → small deduction.
- The role's **primary** requirement is missing → significant deduction, not disqualifying.

### Domain experience (positive signal when it matches)

{Sectors, themes, regions, donors and funders they've worked with.}

## Seniority fit

- Ideal: {titles and levels}.
- Reasonable stretch: {…} (score it, note the stretch).
- Poor fit: {…} → low score.

## Scoring rubric

Give an `overall_score` from 0 to 100:

| Range | Meaning |
| --- | --- |
| 85–100 | Eligible, pay meets or likely meets, strong fit, and clear signals for their top preferences. |
| 70–84 | Eligible, strong fit, pay meets or unlisted, no negative signals. |
| 50–69 | Eligible but partial fit, or some negative signals. |
| 21–49 | Weak fit: wrong role, wrong level, or several negative signals. |
| 0–20 | Disqualified by a hard requirement. |

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
  "salary_notes": "string",
  "role_category": "{role-a} | {role-b} | {role-c} | other",
  "seniority": "string as stated in the posting",
  "contract_type": "staff | fixed-term | consultancy | unknown",
  "contract_length": "e.g. 6 months, or null",
  "travel_required": "none | occasional | frequent | unknown",
  "matched_skills": ["string"],
  "gaps": ["string"],
  "red_flags": ["string"],
  "overall_score": 0,
  "summary": "One sentence on why this is or isn't a good fit."
}
```

<!-- Core fields, keep exactly as written: disqualified, disqualify_reason, remote,
     location_eligibility, salary, matched_skills, gaps, red_flags, overall_score, summary.
     The others are this profile's own; add, rename or drop them. Types come from the example
     value: "a | b" one of these, "text", "text, or null", ["text"], 0 (integer), false. -->

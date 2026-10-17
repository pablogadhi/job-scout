# Writing an instance's filters.yaml

The pre-filter runs before any Claude call and only **drops**. Rules run in order (title,
remote, location, salary); the first one that fails decides. Patterns are case-insensitive
Python regexes. They never read descriptions, only:

| Rule | Reads |
| --- | --- |
| titles | the title |
| remote | the title, the location field, the source's remote/workplace field |
| location | the title, the location field |
| salary | the parsed salary, only when it's in `salary.currency` (others go to Claude) |

What each section does:

- `titles.include_any`: a title must match at least one, or it's dropped. This is the rule
  that drops the most, so make it broad: every title the person could plausibly hold, in
  each language they apply in.
- `titles.exclude_any`: dropped if any matches. Only clear non-fits (junior, intern,
  volunteer, assistant for a director-level person, unrelated trades).
- `remote.exclude_any`: dropped if any matches. Only explicit on-site or hybrid wording.
  "Home-based" is remote: never exclude it, and don't exclude "field" or "travel" here
  (travel is a preference, judged by Claude).
- `location.allow_any`: a match passes the location rule outright, even if an exclude also
  matches. Only places that are clearly fine.
- `location.exclude_any`: dropped only when it's explicit that the applicant must be
  somewhere that excludes the person's country ("US only", "must reside in the EU",
  "nationals only"). A bare place ("USA", "Remote - Spain", a UN duty station) is unclear and
  goes to Claude.
- `salary`: dropped only when a range is stated in this currency and its maximum is below
  `min_annual`. Unlisted salaries, other currencies and UN grades go to Claude.
- `dedupe.key`: keep `[company_normalized, title_normalized]`.

When unsure, leave a posting to Claude: a false drop is never seen again, a false pass only
costs a scoring call. After writing the file, check it with `filter --sample 20` and read
every sampled drop.

## Example: a program director in Guatemala

Adjust the places and titles to the person. Copy `location.exclude_any` from the repo's
`config/filters.yaml` when the person's country is the same (it's long and tested); the
version here is shorter.

```yaml
titles:
  include_any:
    - '\b(program(me)?|project|portfolio|country|regional|operations) (director|manager|lead|leader|coordinator|officer|specialist|advisor|adviser)\b'
    - '\b(director|head|chief) of (programs?|programmes?|operations|projects)\b'
    - '\b(deputy )?chief of party\b'
    - '\b(team leader|technical (advisor|adviser|lead|specialist))\b'
    - '\b(monitoring|evaluation|m&e|mel|meal)\b'
    - '\b(grants?|partnerships?|resource mobili[sz]ation) (director|manager|lead|specialist|advisor|adviser)\b'
    - '\bconsult(ant|ancy)\b'
    # Spanish
    - '\b(director|directora|gerente|coordinador|coordinadora|especialista|asesor|asesora|oficial|responsable|jefe|jefa)\b.*\b(programa|programas|proyecto|proyectos|pa[ií]s|regional|operaciones)\b'
    - '\bconsultor(a|[ií]a)?\b'
  exclude_any:
    - '\b(intern(ship)?|pasant[ií]a|volunteer|voluntari[oa]|trainee|junior|entry[- ]level)\b'
    - '\b(assistant|asistente|auxiliar)\b'
    - '\b(driver|chofer|security guard|guardia|cleaner)\b'

remote:
  exclude_any:
    - '\bhybrid\b'
    - '\bh[ií]brido\b'
    - '\bon[- ]?site\b'
    - '\bpresencial\b'
    - '\bin[- ]office\b'
    - '\brelocation (is )?required\b'

location:
  allow_any:
    - '\b(worldwide|anywhere|global(ly)?|international)\b'
    - '\b(latam|latin america|am[eé]rica latina|latinoam[eé]rica|central america|centroam[eé]rica)\b'
    - '\bamericas\b'
    - '\bguatemala\b'
  exclude_any:
    - '\b((?-i:US)|usa|united states|uk|eu|europe|canada)[\s-]+((based|applicants?|residents?|citizens?|candidates?)\s+)?only\b'
    - '\bmust (be located|be based|reside|live) in (the )?((?-i:US)|usa|united states|uk|eu|europe|canada)(?![a-z])'
  on_unclear: pass_to_llm

salary:
  currency: USD
  min_annual: 48000   # the person's floor, from the interview
  hours_per_year: 2080
  months_per_year: 12
  rule: drop_if_max_below_min
  on_unlisted: pass_to_llm

dedupe:
  key: [company_normalized, title_normalized]
```

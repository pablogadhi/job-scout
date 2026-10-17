# Interview questions

Ask only what the CV doesn't answer, one question at a time. Each question says where the
answer goes. Recommend an option when there's a sensible default.

## Everyone

| Question | Goes to |
| --- | --- |
| Where does the person live, and in which time zone? Any countries they can't work for (sanctions, tax)? | profile: location eligibility; filters: `location` |
| Remote only, or would hybrid/on-site somewhere count? Is "home-based with travel" remote enough? | profile: hard requirement 1; filters: `remote` |
| Target titles and seniority, in each language they'd apply in (e.g. "Program Director" / "Director(a) de Programa")? Titles they've outgrown? | profile: summary, seniority fit; filters: `titles` |
| Salary floor: amount, currency, per year or month? Gross? Would a lower salary ever be acceptable (e.g. a great mission)? | profile: hard requirement 3; filters: `salary` |
| Employment types: staff, fixed-term, consultancy, contractor via a platform? | profile: hard requirements or preferences; output field `contract_type` |
| Languages and level; is a posting in Spanish (or requiring it) a plus? | profile: summary, matched skills |
| What does work-life balance mean to them in practice (hours, travel, on-call, weekends)? | profile: strong preferences; output fields |
| Dream employers, or ones to avoid? | sources: `watchlist`; profile: red flags |
| Anything that's an instant no that the CV wouldn't show? | profile: hard requirements |

## Nonprofit and international development

| Question | Goes to |
| --- | --- |
| Themes to target or rule out (e.g. education, health, gender, migration, climate, governance, humanitarian response)? | profile: domain experience, red flags |
| Geography of the programs: Guatemala only, Central America, LATAM, global? Does the program's country matter if the job is home-based? | profile: preferences |
| Consultancies: acceptable? Minimum length (e.g. 3 months)? Day rates as well as salaries? | profile: hard requirements or preferences; output field `contract_length` |
| Maximum travel: none, occasional, up to N% / N weeks a year? Is frequent field travel a dealbreaker? | profile: hard requirement or strong preference; output field `travel_required` |
| UN posts: which contract types are fine (staff P/NO grades, IPSA/NPSA, individual contractor, UNV)? Any grade floor? | profile: preferences, salary notes |
| Donors and funders they've managed or reported to (USAID, EU, IDB, UN agencies, foundations)? | profile: skills; output field `donors` |
| Program size and scope they've run (budget, staff, number of partners)? | profile: seniority fit |
| Organization types: INGOs, local NGOs, UN agencies, foundations, consultancy firms (Chemonics, DAI…), government? | profile: preferences |

## Sources

| Question | Goes to |
| --- | --- |
| Boards they already use and trust? | sources: proposed (disabled) entries for the operator |
| Should postings from the general remote boards (tech-heavy) be fetched at all? | sources: `enabled` |

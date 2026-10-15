"""Generate the synthetic HTTP fixtures in tests/fixtures/http/.

All fixture data in this project is synthetic: companies, people, titles, ids and text are
invented. Only the payload *structure* mirrors each source (field names, nesting, quirks),
so the mappers are exercised exactly as against the live APIs. Never commit recorded
responses; `fetch --record` writes to the gitignored data/recordings/ for studying shapes.

Run after changing this file:  uv run python tests/fixtures/make_http_fixtures.py
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import httpx

from job_scout.config import load_config

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent / "http"

JSON = "application/json; charset=utf-8"
RSS = "application/rss+xml; charset=utf-8"


def mojibake(text: str) -> str:
    """UTF-8 read as Latin-1, the way Remote OK serves its descriptions."""
    return text.encode("utf-8").decode("latin-1")


def cdata(text: str) -> str:
    return f"<![CDATA[{text}]]>"


def rss(title: str, items: list[str], namespaces: str = "") -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<rss version="2.0"{namespaces}>\n<channel>\n<title>{escape(title)}</title>\n'
        "<link>https://example.invalid/</link>\n<description>Synthetic fixture feed</description>\n"
        + "\n".join(items)
        + "\n</channel>\n</rss>\n"
    ).encode()


def item(**fields: str) -> str:
    """<item> with fields in order; values are inserted verbatim (escape or cdata first)."""
    body = "\n".join(
        f"  <{tag}>{value}</{tag.split()[0]}>" if not tag.endswith("/") else f"  <{tag[:-1]}/>"
        for tag, value in fields.items()
    )
    return f"<item>\n{body}\n</item>"


# --- sources -----------------------------------------------------------------------------


def fourdayweek(endpoint: str) -> dict[str, Any]:
    def job(**overrides: Any) -> dict[str, Any]:
        base = {
            "category": "engineering",
            "role": "Software Engineer",
            "level": "senior",
            "contract_type": "permanent",
            "schedule_type": "4_day_week",
            "hours_per_week_min": 32,
            "hours_per_week_max": 32,
            "work_arrangement": "remote",
            "locations": [],
            "timezones": [],
            "skills": [{"name": "Communication", "slug": "communication"}],
            "stack": [{"name": "Python", "slug": "python"}],
            "tools": [{"name": "Kubernetes", "slug": "kubernetes"}],
            "salary_min": None,
            "salary_max": None,
            "salary_currency": None,
            "salary_period": None,
        }
        return {**base, **overrides}

    northwind = {
        "id": "co-northwind",
        "slug": "northwind-cloud",
        "name": "Northwind Cloud",
        "url": "https://4dayweek.io/company/northwind-cloud",
        "website": "https://northwind.example",
        "country": "Germany",
        "employees": 120,
        "schedule_type": "4_day_week",
        "hours_full_time": 32,
        "remote_level": "remote",
        "hires_worldwide": False,
    }
    page1 = [
        job(
            id="4dw-0001",
            slug="senior-platform-engineer-at-northwind-cloud-0001",
            title="Senior Platform Engineer",
            description="Run our Kubernetes platform with FluxCD and Terraform. 32-hour week at full pay.",
            url="https://4dayweek.io/job/senior-platform-engineer-at-northwind-cloud-0001",
            locations=[
                {
                    "country": "Germany",
                    "continent": "Europe",
                    "work_arrangement": "remote",
                    "is_primary": True,
                }
            ],
            timezones=["CET"],
            salary_min=14_000_000,  # cents: $140,000
            salary_max=17_000_000,
            salary_currency="USD",
            salary_period="year",
            posted_at="2026-10-10T16:00:00Z",
            company=northwind,
        ),
        job(
            id="4dw-0002",
            slug="backend-engineer-python-at-tidewater-health-0002",
            title="Backend Engineer (Python)",
            description="Hybrid role, three days a week in the Austin office.",
            url="https://4dayweek.io/job/backend-engineer-python-at-tidewater-health-0002",
            schedule_type="9_day_fortnight",
            hours_per_week_min=40,
            hours_per_week_max=40,
            locations=[
                {
                    "city": "Austin",
                    "state": "Texas",
                    "country": "United States",
                    "continent": "North America",
                    "work_arrangement": "hybrid",
                }
            ],
            posted_at="2026-10-09T12:00:00Z",
            company={
                **northwind,
                "id": "co-tidewater",
                "slug": "tidewater-health",
                "name": "Tidewater Health",
                "remote_level": "hybrid",
            },
        ),
    ]
    page2 = [
        # Same job as 4dw-0001 posted for another country: links as a duplicate.
        job(
            id="4dw-0003",
            slug="senior-platform-engineer-at-northwind-cloud-0003",
            title="Senior Platform Engineer",
            description="Run our Kubernetes platform with FluxCD and Terraform. 32-hour week at full pay.",
            url="https://4dayweek.io/job/senior-platform-engineer-at-northwind-cloud-0003",
            locations=[
                {"country": "Mexico", "continent": "North America", "work_arrangement": "remote"}
            ],
            timezones=["UTC-6"],
            posted_at="2026-10-10T16:05:00Z",
            company=northwind,
        ),
        job(
            id="4dw-0004",
            slug="staff-software-engineer-at-lumen-ledger-0004",
            title="Staff Software Engineer",
            description="Async-first fintech team. Monthly contractor rate.",
            url="https://4dayweek.io/job/staff-software-engineer-at-lumen-ledger-0004",
            salary_min=900_000,  # cents: $9,000 per month
            salary_max=1_100_000,
            salary_currency="USD",
            salary_period="month",
            posted_at="2026-10-08T09:30:00Z",
            company={
                **northwind,
                "id": "co-lumen",
                "slug": "lumen-ledger",
                "name": "Lumen Ledger",
                "hires_worldwide": True,
            },
        ),
    ]
    page_url = lambda n: str(httpx.URL(endpoint).copy_merge_params({"page": n}))  # noqa: E731
    return {
        page_url(1): (JSON, {"data": page1, "page": 1, "limit": 100, "total": 4, "has_more": True}),
        page_url(2): (
            JSON,
            {"data": page2, "page": 2, "limit": 100, "total": 4, "has_more": False},
        ),
    }


def remoteok(endpoint: str) -> dict[str, Any]:
    def job(job_id: str, slug: str, **fields: Any) -> dict[str, Any]:
        url = f"https://remoteOK.com/remote-jobs/{slug}-{job_id}"
        return {
            "slug": f"{slug}-{job_id}",
            "id": job_id,
            "company_logo": "",
            "logo": "",
            "url": url,
            "apply_url": url,
            **fields,
        }

    return {
        endpoint: (
            JSON,
            [
                {"last_updated": 1791800000, "legal": "API Terms of Service: synthetic fixture."},
                job(
                    "900001",
                    "remote-senior-django-developer-quetzal-labs",
                    epoch=1791700000,
                    date="2026-10-11T08:00:00+00:00",
                    company="Quetzal Labs",
                    position="Senior Django Developer",
                    tags=["python", "django", "backend"],
                    description=mojibake(
                        "<p>\U0001f680 About Quetzal Labs</p><p>We build payment APIs.</p>"
                    ),
                    location="Worldwide",
                    salary_min=130000,
                    salary_max=160000,
                ),
                job(
                    "900002",
                    "remote-marketing-manager-brightpath",
                    epoch=1791600000,
                    date="2026-10-10T04:00:00+00:00",
                    company="Brightpath",
                    position="Marketing Manager",
                    tags=["marketing"],
                    description="<p>Lead our campaigns.</p>",
                    location="United States",
                    salary_min=0,  # Remote OK's "unlisted"
                    salary_max=0,
                ),
                # Also on We Work Remotely: links as a cross-source duplicate.
                job(
                    "900003",
                    "remote-senior-backend-engineer-harbor-co",
                    epoch=1791500000,
                    date="2026-10-09T00:00:00+00:00",
                    company="Harbor & Co",
                    position="Senior Backend Engineer",
                    tags=["python", "postgres"],
                    description="<p>Backend for a logistics platform.</p>",
                    location="Latin America",
                    salary_min=0,
                    salary_max=0,
                ),
            ],
        )
    }


def remotive(endpoint: str) -> dict[str, Any]:
    def job(job_id: int, slug: str, title: str, company: str, **fields: str) -> str:
        url = f"https://remotive.com/remote-jobs/{slug}-{job_id}"
        return item(
            title=escape(title),
            jobId=str(job_id),
            company=escape(company),
            location=escape(fields["location"]),
            type=fields["type"],
            **{'guid isPermaLink="false"': url},
            link=url,
            pubDate=fields["date"],
            **{"dc:creator": escape(company)},
            category=escape(fields["category"]),
            description=cdata(fields["body"]),
        )

    ns = ' xmlns:dc="http://purl.org/dc/elements/1.1/"'
    return {
        endpoint: (
            RSS,
            rss(
                "Remotive Remote Jobs RSS Feed",
                [
                    job(
                        7000001,
                        "software-development/senior-full-stack-engineer",
                        "Senior Full Stack Engineer",
                        "Cobalt Systems",
                        location="Americas",
                        type="full_time",
                        date="Thu, 08 Oct 2026 21:47:32 GMT",
                        category="Software Development",
                        body="<p>Own features end to end.</p><p>Salary: $120,000 - $150,000</p>",
                    ),
                    job(
                        7000002,
                        "sales/inside-sales-representative",
                        "Inside Sales Representative",
                        "Brightpath",
                        location="USA Only",
                        type="contract",
                        date="Wed, 07 Oct 2026 10:00:00 GMT",
                        category="Sales",
                        body="<p>Close deals.</p>",
                    ),
                ],
                ns,
            ),
        )
    }


def workingnomads(endpoint: str) -> dict[str, Any]:
    return {
        endpoint: (
            JSON,
            [
                {
                    "url": "https://www.workingnomads.com/job/go/5000001/",
                    "title": "Senior Backend Developer (Python)",
                    "description": "<p>Join a distributed team.</p><p>Salary: $90k - $110k</p>",
                    "company_name": "Proxima Talent",
                    "category_name": "Development",
                    "tags": "python,django,postgresql",
                    "location": "Time zone: UTC-6 (+/- 3 hours)",
                    "pub_date": "2026-10-08T16:54:14-04:00",
                },
                {
                    "url": "https://www.workingnomads.com/job/go/5000002/",
                    "title": "Customer Support Agent",
                    "description": "<p>Answer tickets.</p>",
                    "company_name": "Brightpath",
                    "category_name": "Customer Success",
                    "tags": "support",
                    "location": "United States",
                    "pub_date": "2026-10-08T10:00:00-04:00",
                },
            ],
        )
    }


def weworkremotely(endpoints: list[str]) -> dict[str, Any]:
    ns = ' xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:media="http://search.yahoo.com/mrss"'

    def job(
        slug: str, title: str, region: str, countries: str, skills: str, body: str, date: str
    ) -> str:
        url = f"https://weworkremotely.com/remote-jobs/{slug}"
        return item(
            **{
                'media:content url="https://example.invalid/logo.png" type="image/png"/': "",
                "title": escape(title),
                "region": escape(region),
                "country": escape(countries),
                "skills": escape(skills),
                "category": "Back-End Programming",
                "type": "Full-Time",
                "description": escape(body),
                "pubDate": date,
                "guid": url,
                "link": url,
            }
        )

    harbor = job(
        "harbor-co-senior-back-end-engineer",
        "Harbor & Co: Senior Back-End Engineer",
        "Anywhere in the World",
        "\U0001f1ec\U0001f1f9 Guatemala, \U0001f1f2\U0001f1fd Mexico",
        "Python, PostgreSQL, and Kubernetes",
        "<p><strong>Headquarters:</strong> Remote</p><p>Backend for a logistics platform.</p>",
        "Fri, 09 Oct 2026 10:00:00 +0000",
    )
    cobalt = job(
        "cobalt-systems-full-stack-developer",
        "Cobalt Systems: Full-Stack Developer",
        "USA Only",
        "\U0001f1fa\U0001f1f8 United States",
        "React and Node.js",
        "<p>US-based team.</p><p>Salary: $100,000 - $115,000 per year</p>",
        "Thu, 08 Oct 2026 09:00:00 +0000",
    )
    orbit = job(
        "orbit-analytics-site-reliability-engineer",
        "Orbit Analytics: Site Reliability Engineer",
        "Latin America Only",
        "\U0001f1e7\U0001f1f7 Brazil, \U0001f1ec\U0001f1f9 Guatemala",
        "Terraform, AWS",
        "<p>On-call rotation, async culture.</p>",
        "Wed, 07 Oct 2026 08:00:00 +0000",
    )
    backend, fullstack, devops = endpoints
    return {
        backend: (RSS, rss("We Work Remotely: Back-End Programming Jobs", [harbor], ns)),
        # Harbor & Co is listed in two categories: same guid, stored once.
        fullstack: (
            RSS,
            rss("We Work Remotely: Full-Stack Programming Jobs", [harbor, cobalt], ns),
        ),
        devops: (RSS, rss("We Work Remotely: DevOps and Sysadmin Jobs", [orbit], ns)),
    }


def realworkfromanywhere(endpoints: list[str]) -> dict[str, Any]:
    def job(slug: str, title: str, company: str, body: str) -> str:
        url = f"https://www.realworkfromanywhere.com/jobs/{slug}"
        return item(
            title=cdata(f"{title} at {company}"),
            description=cdata(body),
            link=url,
            guid=url,
            pubDate="Fri, 09 Oct 2026 00:00:00 GMT",
            category=cdata("Backend"),
            author=cdata(company),
        )

    lattice = job(
        "python-software-engineer-remote-work-lattice-forge-1001",
        "Python Software Engineer (remote work)",
        "Lattice Forge",
        "<p>Work from anywhere.</p>",
    )
    harbor = job(
        "senior-backend-engineer-harbor-co-1002",
        "Senior Backend Engineer",
        "Harbor & Co",
        "<p>Backend for a logistics platform.</p>",
    )
    # A bare "&" outside CDATA makes the feed malformed XML, as the live feed was;
    # feedparser flags it (bozo) but still returns the entries.
    broken = item(
        title="Platform Engineer at Lattice Forge",
        description="R&D platform team",
        link="https://www.realworkfromanywhere.com/jobs/platform-engineer-lattice-forge-1003",
        guid="https://www.realworkfromanywhere.com/jobs/platform-engineer-lattice-forge-1003",
        pubDate="Thu, 08 Oct 2026 00:00:00 GMT",
    )
    backend, fullstack, software, devops = endpoints
    return {
        backend: (RSS, rss("Remote Backend Jobs - Real Work From Anywhere", [lattice, harbor])),
        fullstack: (RSS, rss("Remote Fullstack Jobs - Real Work From Anywhere", [])),
        software: (RSS, rss("Remote Software Developer Jobs - Real Work From Anywhere", [lattice])),
        devops: (RSS, rss("Remote DevOps Jobs - Real Work From Anywhere", [broken])),
    }


def remotefirstjobs(endpoints: list[str]) -> dict[str, Any]:
    def job(slug: str, company_slug: str, title: str, body: str) -> str:
        return item(
            title=escape(title),
            link=f"https://remotefirstjobs.com/companies/{company_slug}/jobs/{slug}",
            description=escape(body),
            **{'guid isPermaLink="true"': slug},
            pubDate="Mon, 12 Oct 2026 11:36:23 +0000",
        )

    claims = job(
        "claims-representative-800001",
        "seaside-insurance",
        "Claims Representative at Seaside Insurance",
        "<p>Pay Information: $74,200 - $92,700 per year</p>",
    )
    devops = job(
        "senior-devops-engineer-800002",
        "orbit-analytics",
        "Senior DevOps Engineer at Orbit Analytics",
        # Upstream typo in the first salary line ($95,00); the second line is correct.
        "<p>Salary Range: $95,00–$115,000 annually</p>"
        "<p>The base salary range for this position is $95,000–$115,000 annually.</p>",
    )
    python = job(
        "senior-python-developer-800003",
        "quetzal-labs",
        "Senior Python Developer at Quetzal Labs",
        "<p>Django and PostgreSQL.</p>",
    )
    software, devops_url, python_url = endpoints
    return {
        software: (RSS, rss("Remote Software Development Jobs RSS", [claims, devops])),
        devops_url: (RSS, rss("Remote DevOps Jobs RSS", [devops])),
        python_url: (RSS, rss("Remote Python Jobs RSS", [python])),
    }


def hn(endpoints: list[str]) -> dict[str, Any]:
    search_url, item_template = endpoints
    story_id = 49000001

    def comment(
        cid: int, text: str | None, author: str | None = "synthetic_user", children=()
    ) -> dict:
        return {
            "author": author,
            "children": list(children),
            "created_at": "2026-10-01T15:10:00.000Z",
            "created_at_i": 1790867400,
            "id": cid,
            "options": [],
            "parent_id": story_id,
            "points": None,
            "story_id": story_id,
            "text": text,
            "title": None,
            "type": "comment",
            "url": None,
        }

    reply = comment(49000099, "<p>Is this open to contractors?</p>")
    children = [
        comment(
            49000010,
            "Quetzal Labs | Senior Platform Engineer | REMOTE (Americas) | Full-time | $150k-$190k"
            "<p>We run Kubernetes on bare metal.</p><p>Apply: jobs@quetzal.example</p>",
            children=[reply],
        ),
        comment(
            49000011,
            "Lumen Ledger (Remote, LATAM) — Senior Backend Engineer — Python / Postgres"
            "<p>Payments infrastructure.</p>",
        ),
        comment(49000012, None, author=None),  # deleted
        comment(
            49000013,
            "Brightpath | Staff Engineer | ONSITE | San Francisco, CA<p>Five days in office.</p>",
        ),
        comment(
            49000014,
            "Cobalt Systems (raised $20M Series A) | Backend Engineer | REMOTE (US) | $140k-$170k"
            "<p>US work authorization required.</p>",
        ),
    ]

    def hit(object_id: str, title: str, created: str) -> dict:
        return {
            "objectID": object_id,
            "title": title,
            "created_at": created,
            "author": "whoishiring",
        }

    search = {
        "hits": [
            hit(
                "49000002", "Ask HN: Who wants to be hired? (October 2026)", "2026-10-01T15:00:00Z"
            ),
            hit(str(story_id), "Ask HN: Who is hiring? (October 2026)", "2026-10-01T15:00:00Z"),
            hit("48000001", "Ask HN: Who is hiring? (September 2026)", "2026-09-01T15:00:00Z"),
        ],
        "hitsPerPage": 3,
        "nbHits": 3,
        "page": 0,
    }
    story = {
        **comment(story_id, None),
        "title": "Ask HN: Who is hiring? (October 2026)",
        "type": "story",
        "children": children,
    }
    return {
        search_url: (JSON, search),
        item_template.format(story_id=story_id): (JSON, story),
    }


def pyjobs(endpoint: str) -> dict[str, Any]:
    def job(slug: str, title: str, summary: str) -> str:
        return item(
            title=cdata(title),
            link=f"https://www.pyjobs.com/job/{slug}",
            description=cdata(summary),
            pubDate="Mon, 12 Oct 2026 16:15:20 +0000",
        )

    ns = ' xmlns:atom="http://www.w3.org/2005/Atom"'
    return {
        endpoint: (
            RSS,
            rss(
                "PyJobs",
                [
                    job(
                        "senior-python-engineer-Qz12Ab34",
                        "Senior Python Engineer",
                        "Senior Python Engineer at Quetzal Labs. Geo remote · Full-time · "
                        "USD 140,000–170,000 per year.",
                    ),
                    job(
                        "data-engineer-intern-Bp56Cd78",
                        "Data Engineer Intern",
                        "Data Engineer Intern at Brightpath GmbH. Office · Berlin · "
                        "Internship · EUR 1,500–1,500 per month.",
                    ),
                    job(
                        "backend-engineer-rd-Ob90Ef12",
                        "Backend Engineer (R&amp;D)",  # the live feed escapes inside CDATA
                        "Backend Engineer (R&amp;D) at Orbit Analytics. Hybrid · Madrid · Full-time.",
                    ),
                ],
                ns,
            ),
        )
    }


def typescriptjobs(endpoint: str) -> dict[str, Any]:
    def job(slug: str, title: str, body: str) -> str:
        url = f"https://typescriptjobs.net/jobs/{slug}"
        return item(
            title=escape(title),
            link=url,
            **{'guid isPermaLink="true"': url},
            pubDate="Mon, 12 Oct 2026 17:21:00 GMT",
            description=escape(body),
        )

    return {
        endpoint: (
            RSS,
            rss(
                "TypeScript Jobs",
                [
                    job(
                        "senior-typescript-engineer-at-cobalt-systems",
                        "Senior TypeScript Engineer at Cobalt Systems",
                        "Next.js and Node.",
                    ),
                    job(
                        "mid-level-product-engineer-at-lattice-forge-inc",
                        "Mid-Level Product Engineer at Lattice Forge Inc.",
                        "Product work.",
                    ),
                ],
            ),
        )
    }


def remotegamejobs(endpoint: str) -> dict[str, Any]:
    def job(slug: str, title: str, body: str) -> str:
        url = f"https://remotegamejobs.com/jobs/{slug}"
        return item(
            title=escape(title),
            link=url,
            pubDate="Fri, 09 Oct 2026 17:44:38 +0000",
            guid=url,
            description=escape(body),
        )

    return {
        endpoint: (
            RSS,
            rss(
                "Remote Game Jobs",
                [
                    job(
                        "pixel-harbor-studios-senior-backend-engineer-online-services-remote-job",
                        "Pixel Harbor Studios is hiring Senior Backend Engineer - Online Services (Remote Job)",
                        "Matchmaking and live ops. Must reside within UTC-4 to UTC-8.",
                    ),
                    job(
                        "pixel-harbor-studios-environment-artist-remote-job",
                        "Pixel Harbor Studios is hiring Environment Artist (Remote Job)",
                        "Job Description",
                    ),
                ],
            ),
        )
    }


def findjobit(endpoint: str) -> dict[str, Any]:
    def job(oid: str, title: str, category: str, region: str, body: str) -> str:
        url = f"https://findjobit.com/jobs/{oid}"
        return item(
            title=escape(title),
            link=url,
            guid=url,
            description=escape(body),
            category=escape(category),
            region=escape(region),
            type="Tiempo Completo",
            pubDate="Fri, 09 Oct 2026 18:23:18 GMT",
        )

    ns = ' xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:media="http://search.yahoo.com/mrss/"'
    return {
        endpoint: (
            RSS,
            rss(
                "Findjobit",
                [
                    job(
                        "f00000000000000000000001",
                        "Quetzal Labs busca un Senior Backend Engineer",
                        "Ingeniero Backend",
                        "Guatemala,México,Colombia",
                        "<p>Python y Django.</p>",
                    ),
                    job(
                        "f00000000000000000000002",
                        "Brightpath busca una Diseñadora UX",
                        "Diseñador UX/UI",
                        "Argentina",
                        "<p>Diseño de producto.</p>",
                    ),
                ],
                ns,
            ),
        )
    }


# --- remoteintech seed + company ATS boards ----------------------------------------------

SEED_COMPANIES = {
    # slug: frontmatter (kept: worldwide/americas/americas-europe and fully-remote/remote-first)
    "quetzal-labs": {
        "title": "Quetzal Labs",
        "website": "https://quetzal-labs.example/",
        "careers_url": "https://boards.greenhouse.io/quetzallabs",
        "region": "worldwide",
        "remote_policy": "fully-remote",
    },
    "brightpath": {
        "title": "Brightpath",
        "website": "https://brightpath.example/",
        "careers_url": "https://jobs.lever.co/brightpath",
        "region": "americas",
        "remote_policy": "remote-first",
    },
    "nimbus-works": {
        "title": "Nimbus Works",
        "website": "https://nimbusworks.example/",
        "careers_url": "https://nimbusworks.example/careers",
        "region": "americas-europe",
        "remote_policy": "fully-remote",
    },
    "tikal-soft": {
        "title": "Tikal Soft",
        "website": "https://www.tikalsoft.example/",
        "region": "worldwide",
        "remote_policy": "fully-remote",
    },
    "laguna-data": {
        "title": "Laguna Data",
        "website": "https://lagunadata.example/",
        "careers_url": "https://lagunadata.recruitee.com/",
        "region": "worldwide",
        "remote_policy": "fully-remote",
    },
    "euro-widgets": {  # filtered out: region
        "title": "Euro Widgets",
        "website": "https://eurowidgets.example/",
        "careers_url": "https://jobs.lever.co/eurowidgets",
        "region": "europe",
        "remote_policy": "fully-remote",
    },
    "deskbound": {  # filtered out: remote policy
        "title": "Deskbound",
        "website": "https://deskbound.example/",
        "region": "americas",
        "remote_policy": "hybrid",
    },
}


def seed_tarball() -> bytes:
    """The remote-jobs repo as GitHub's tarball endpoint serves it (a top-level folder)."""
    import gzip
    import io
    import tarfile

    files = {"README.md": "# remote-jobs (synthetic)\n"}
    for slug, fields in SEED_COMPANIES.items():
        front = "\n".join(f"{k}: {v}" for k, v in fields.items())
        files[f"src/companies/{slug}.md"] = (
            f"---\nslug: {slug}\n{front}\naddedAt: 2026-01-01\n---\n\n## Company blurb\n\n"
            f"{fields['title']} is a synthetic company.\n"
        )
    files["src/companies/broken.md"] = "No frontmatter here.\n"
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(f"remoteintech-remote-jobs-0000000/{name}")
            info.size, info.mtime = len(data), 1_760_000_000
            tar.addfile(info, io.BytesIO(data))
    return gzip.compress(raw.getvalue(), mtime=0)


def remoteintech(endpoint: str, platforms: dict[str, str]) -> dict[str, Any]:
    def board(ats: str, token: str) -> str:
        return platforms[ats].replace("{" + platforms[ats].split("{")[1].split("}")[0] + "}", token)

    greenhouse = {
        "jobs": [
            {
                "absolute_url": "https://job-boards.greenhouse.io/quetzallabs/jobs/4000001",
                "data_compliance": [{"type": "gdpr", "requires_consent": False}],
                "internal_job_id": 3000001,
                "location": {"name": "Remote - Americas"},
                "metadata": [],
                "id": 4000001,
                "updated_at": "2026-10-08T10:00:00-04:00",
                "requisition_id": "Q-101",
                "title": "Senior Backend Engineer (Python)",
                "company_name": "Quetzal Labs",
                "first_published": "2026-10-07T09:00:00-04:00",
                "language": "en",
                "content": (
                    "&lt;p&gt;Build payment APIs with Django and PostgreSQL.&lt;/p&gt;"
                    "&lt;p&gt;Salary: $140,000 - $170,000 USD per year&lt;/p&gt;"
                ),
                "departments": [
                    {"id": 1, "name": "Engineering", "child_ids": [], "parent_id": None}
                ],
                "offices": [
                    {
                        "id": 2,
                        "name": "Remote - LATAM",
                        "location": None,
                        "child_ids": [],
                        "parent_id": None,
                    }
                ],
            },
            {
                "absolute_url": "https://job-boards.greenhouse.io/quetzallabs/jobs/4000002",
                "location": {"name": "Guatemala City"},
                "metadata": [],
                "id": 4000002,
                "updated_at": "2026-10-06T10:00:00-04:00",
                "title": "Office Manager",
                "company_name": "Quetzal Labs",
                "first_published": None,
                "content": "&lt;p&gt;Run the office.&lt;/p&gt;",
                "departments": [],
                "offices": [],
            },
        ],
        "meta": {"total": 2},
    }
    lever = [
        {
            "additional": "<div>Benefits.</div>",
            "additionalPlain": "Benefits: home office budget.",
            "categories": {
                "commitment": "Full-time",
                "department": "Engineering",
                "location": "Remote - LATAM",
                "team": "Platform",
                "allLocations": ["Remote - LATAM", "Remote - US"],
            },
            "createdAt": 1791900000000,
            "descriptionPlain": "Brightpath runs a Kubernetes platform for clinics.",
            "description": "<div>Brightpath runs a Kubernetes platform for clinics.</div>",
            "id": "a1b2c3d4-0000-4000-8000-000000000001",
            "lists": [
                {
                    "text": "What you'll do",
                    "content": "<li>Operate clusters</li><li>Write Terraform</li>",
                }
            ],
            "text": "Senior Platform Engineer",
            "country": "MX",
            "workplaceType": "remote",
            "hostedUrl": "https://jobs.lever.co/brightpath/a1b2c3d4-0000-4000-8000-000000000001",
            "applyUrl": "https://jobs.lever.co/brightpath/a1b2c3d4-0000-4000-8000-000000000001/apply",
            "salaryRange": {
                "currency": "USD",
                "interval": "per-year-salary",
                "min": 130000,
                "max": 160000,
            },
        },
        {
            "categories": {
                "commitment": "Full-time",
                "location": "Bogotá",
                "allLocations": ["Bogotá"],
            },
            "createdAt": 1791800000000,
            "descriptionPlain": "Help our customers.",
            "id": "a1b2c3d4-0000-4000-8000-000000000002",
            "lists": [],
            "text": "Customer Success Lead",
            "workplaceType": "hybrid",
            "hostedUrl": "https://jobs.lever.co/brightpath/a1b2c3d4-0000-4000-8000-000000000002",
            "applyUrl": "https://jobs.lever.co/brightpath/a1b2c3d4-0000-4000-8000-000000000002/apply",
        },
    ]
    ashby = {
        "jobs": [
            {
                "id": "b0000000-0000-4000-8000-000000000001",
                "title": "Staff Software Engineer, Data",
                "department": "Engineering",
                "team": "Data",
                "employmentType": "FullTime",
                "location": "Remote",
                "secondaryLocations": [{"location": "Mexico"}],
                "publishedAt": "2026-10-05T15:00:00.000+00:00",
                "isListed": True,
                "isRemote": True,
                "workplaceType": "Remote",
                "address": {"postalAddress": {"addressCountry": "Mexico"}},
                "jobUrl": "https://jobs.ashbyhq.com/nimbusworks/b0000000-0000-4000-8000-000000000001",
                "applyUrl": "https://jobs.ashbyhq.com/nimbusworks/b0000000-0000-4000-8000-000000000001/application",
                "descriptionPlain": "Own the data pipeline. Python, Kafka, PostgreSQL.",
                "descriptionHtml": "<p>Own the data pipeline.</p>",
                "shouldDisplayCompensationOnJobPostings": True,
                "compensation": {
                    "compensationTierSummary": "$125K - $150K",
                    "scrapeableCompensationSalarySummary": "$125K - $150K",
                    "compensationTiers": [],
                    "summaryComponents": [
                        {
                            "compensationType": "Salary",
                            "interval": "1 YEAR",
                            "currencyCode": "USD",
                            "minValue": 125000,
                            "maxValue": 150000,
                        }
                    ],
                },
            },
            {
                "id": "b0000000-0000-4000-8000-000000000002",
                "title": "Unlisted Role",
                "location": "Remote",
                "isListed": False,
                "jobUrl": "https://jobs.ashbyhq.com/nimbusworks/b0000000-0000-4000-8000-000000000002",
            },
        ],
        "apiVersion": "1",
    }
    workable = {
        "name": "Tikal Soft",
        "description": "Synthetic software studio.",
        "jobs": [
            {
                "title": "Senior Full-Stack Developer",
                "shortcode": "TS00000001",
                "code": "",
                "employment_type": "Full-time",
                "telecommuting": True,
                "department": "Engineering",
                "url": "https://apply.workable.com/j/TS00000001",
                "shortlink": "https://apply.workable.com/j/TS00000001",
                "application_url": "https://apply.workable.com/j/TS00000001/apply",
                "published_on": "2026-10-04",
                "created_at": "2026-10-04",
                "country": "Guatemala",
                "city": "Guatemala City",
                "state": "Guatemala",
                "locations": [
                    {
                        "country": "Guatemala",
                        "countryCode": "GT",
                        "city": "Guatemala City",
                        "region": "Guatemala",
                        "hidden": False,
                    }
                ],
                "description": "<p>TypeScript, React and Django. Fully remote across the Americas.</p>",
            }
        ],
    }
    recruitee = {
        "offers": [
            {
                "id": 9000001,
                "guid": "ab1cd",
                "slug": "senior-devops-engineer",
                "title": "Senior DevOps Engineer",
                "status": "published",
                "company_name": "Laguna Data",
                "department": "Infrastructure",
                "employment_type_code": "fulltime_permanent",
                "remote": True,
                "hybrid": False,
                "on_site": False,
                "location": "Remote",
                "locations": [{"id": 1, "name": "Remote", "country": None, "city": None}],
                "salary": {"min": "9000", "max": "11000", "period": "month", "currency": "USD"},
                "description": "<p>Run our FluxCD and Prometheus stack.</p>",
                "requirements": "<ul><li>Kubernetes</li><li>Terraform</li></ul>",
                "published_at": "2026-10-06 12:00:00 UTC",
                "created_at": "2026-10-06 11:00:00 UTC",
                "careers_url": "https://careers.lagunadata.example/o/senior-devops-engineer",
                "careers_apply_url": "https://careers.lagunadata.example/o/senior-devops-engineer/c/new",
            }
        ]
    }
    return {
        endpoint: ("application/x-gzip", seed_tarball()),
        board("greenhouse", "quetzallabs"): (JSON, greenhouse),
        board("lever", "brightpath"): (JSON, lever),
        board("ashby", "nimbusworks"): (JSON, ashby),
        board("workable", "tikalsoft"): (JSON, workable),
        board("recruitee", "lagunadata"): (JSON, recruitee),
    }


def main() -> None:
    sources = load_config(ROOT / "config").sources
    endpoints = {s.id: s.endpoints for s in sources.sources}
    responses = {
        "4dayweek": fourdayweek(endpoints["4dayweek"][0]),
        "remoteok": remoteok(endpoints["remoteok"][0]),
        "remotive": remotive(endpoints["remotive"][0]),
        "workingnomads": workingnomads(endpoints["workingnomads"][0]),
        "weworkremotely": weworkremotely(endpoints["weworkremotely"]),
        "realworkfromanywhere": realworkfromanywhere(endpoints["realworkfromanywhere"]),
        "remotefirstjobs": remotefirstjobs(endpoints["remotefirstjobs"]),
        "hn_whoishiring": hn(endpoints["hn_whoishiring"]),
        "pyjobs": pyjobs(endpoints["pyjobs"][0]),
        "typescriptjobs": typescriptjobs(endpoints["typescriptjobs"][0]),
        "remotegamejobs": remotegamejobs(endpoints["remotegamejobs"][0]),
        "findjobit": findjobit(endpoints["findjobit"][0]),
        "remoteintech": remoteintech(
            endpoints["remoteintech"][0], {k: v.api for k, v in sources.ats_platforms.items()}
        ),
    }
    shutil.rmtree(OUT, ignore_errors=True)
    for source_id, by_url in responses.items():
        target = OUT / source_id
        target.mkdir(parents=True)
        manifest = {}
        for n, (url, (content_type, body)) in enumerate(by_url.items(), start=1):
            is_json = "json" in content_type
            ext = "json" if is_json else "tar.gz" if "gzip" in content_type else "xml"
            name = f"{n:02d}.{ext}"
            data = (
                (json.dumps(body, indent=1, ensure_ascii=False) + "\n").encode()
                if is_json
                else body
            )
            (target / name).write_bytes(data)
            manifest[url] = {"file": name, "status": 200, "content_type": content_type}
        (target / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"wrote {sum(len(v) for v in responses.values())} responses to {OUT}")


if __name__ == "__main__":
    main()

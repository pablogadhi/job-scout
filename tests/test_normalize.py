import pytest

from job_scout.fetch.base import labelled_salary
from job_scout.fetch.normalize import (
    Salary,
    dedupe_key,
    fix_mojibake,
    html_to_text,
    normalize_company,
    normalize_title,
    parse_salary,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("$120k - $150k", Salary(120_000, 150_000, "USD", "year")),
        ("$120-150k", Salary(120_000, 150_000, "USD", "year")),
        ("USD 100,000 - 120,000", Salary(100_000, 120_000, "USD", "year")),
        ("150000 USD per year", Salary(150_000, 150_000, "USD", "year")),
        ("$170k to $230k Base + Equity", Salary(170_000, 230_000, "USD", "year")),
        ("EUR 2,000–2,000 per month.", Salary(2_000, 2_000, "EUR", "month")),
        ("€75k–80k", Salary(75_000, 80_000, "EUR", "year")),
        ("$60/hr", Salary(60, 60, "USD", "hour")),
        ("$85 - 100 / hour", Salary(85, 100, "USD", "hour")),
        ("R$ 15.000/mês", Salary(15_000, 15_000, "BRL", "month")),
        ("COP 8.000.000 mensual", Salary(8_000_000, 8_000_000, "COP", "month")),
        ("We raised $20M. Pay: $140k-$170k", Salary(140_000, 170_000, "USD", "year")),
    ],
)
def test_parse_salary(text: str, expected: Salary) -> None:
    assert parse_salary(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Series B, 5 years of experience",
        "130000-160000",  # no currency marker
        "salary: $0 - $0",  # hiring.lat-style placeholder
        "$1.5M seed round",
        "raised $20M in Series B",
        "$95 annually",  # implausible yearly figure
        None,
    ],
)
def test_parse_salary_ignores_non_salaries(text: str | None) -> None:
    assert parse_salary(text) is None


def test_labelled_salary_only_reads_salary_lines() -> None:
    text = "We grew revenue to $5M.\nSalary: $130k - $160k\nBenefits"
    assert labelled_salary(text) == Salary(130_000, 160_000, "USD", "year")
    assert labelled_salary("We grew revenue to $150k per month.") is None


def test_labelled_salary_skips_typo_line() -> None:
    text = "Salary Range: $95,00–$115,000 annually"  # upstream typo
    assert labelled_salary(text).max == 115_000


def test_html_to_text_keeps_structure() -> None:
    html = (
        "Acme | SRE | REMOTE<p>Hi <strong>there</strong>,</p>"
        "<ul><li>One</li><li>Two <a href='x'>link</a></li></ul>"
        "<br>End&nbsp;now<script>evil()</script>"
    )
    assert html_to_text(html) == "Acme | SRE | REMOTE\nHi there,\n\n- One\n- Two link\n\nEnd now"
    assert html_to_text("  ") is None


def test_html_to_text_double_escaped() -> None:
    html = (
        "&lt;div class=&quot;intro&quot;&gt;&lt;p&gt;&lt;strong&gt;Acme&lt;/strong&gt; "
        "builds&amp;nbsp;tools.&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Python&lt;/li&gt;&lt;/ul&gt;&lt;/div&gt;"
    )
    assert html_to_text(html) == "Acme builds tools.\n\n- Python"


def test_html_to_text_keeps_a_mentioned_tag() -> None:
    html = "<p>Know when to use a &lt;div&gt; and when not to.</p>"
    assert html_to_text(html) == "Know when to use a <div> and when not to."


def test_fix_mojibake() -> None:
    assert fix_mojibake("ð\u009f\u009a\u0080 Why") == "\U0001f680 Why"
    assert fix_mojibake("Café \U0001f680") == "Café \U0001f680"  # already fine


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Quetzal Inc.", "quetzal"),
        ("brightpath.io", "brightpath"),
        ("Foo Holdings, LLC", "foo holdings"),
        ("The Org GmbH", "the org"),
        ("Lumen Ledger (Remote, LATAM)", "lumen ledger"),
        ("Cobalt Systems (raised $20M Series A)", "cobalt systems"),
        ("(stealth)", "stealth"),
        ("  ", None),
        (None, None),
    ],
)
def test_normalize_company(raw: str | None, expected: str | None) -> None:
    assert normalize_company(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Sr. Back-End Engineer (Remote, LATAM)", "senior backend engineer"),
        ("Senior Backend Engineer - Remote US", "senior backend engineer"),
        ("Senior Full Stack Engineer (.NET)", "senior fullstack engineer net"),
        ("Staff SRE [EMEA]", "staff sre"),
        ("Senior C++ / C# Developer (m/f/d)", "senior c++ c# developer"),
    ],
)
def test_normalize_title(raw: str, expected: str) -> None:
    assert normalize_title(raw) == expected


def test_dedupe_key_is_stable_and_separates_fields() -> None:
    assert dedupe_key(["acme", "sre"]) == dedupe_key(["acme", "sre"])
    assert dedupe_key(["acme s", "re"]) != dedupe_key(["acme", "s re"])

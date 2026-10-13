"""Pure helpers that turn source payload fragments into normalized values. No I/O."""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from selectolax.lexbor import LexborHTMLParser

_BLOCK_TAGS = "p, div, br, ul, ol, h1, h2, h3, h4, h5, h6, tr, table, section, article, pre"


# Markup left over after one pass means the source escaped its HTML twice ("&lt;p&gt;").
# Needs two different tags, or an opening and a closing tag, so a post that mentions "<div>"
# once isn't parsed again.
_LEFTOVER_TAG = re.compile(r"<(/?)(p|div|br|ul|ol|li|strong|em|b|i|h[1-6]|span|a)\b[^<>]*>", re.I)


def looks_double_escaped(text: str | None) -> bool:
    if not text:
        return False
    tags = [(m.group(1), m.group(2).lower()) for m in _LEFTOVER_TAG.finditer(text)]
    names = {name for _, name in tags}
    return len(names) >= 2 or (len(tags) >= 2 and any(closing for closing, _ in tags))


def html_to_text(html: str | None) -> str | None:
    """Strip HTML to readable plain text, keeping paragraph and list structure.

    Double-escaped HTML ("&lt;p&gt;Hi&lt;/p&gt;", sent by some boards) gets a second pass.
    """
    text = _html_to_text_once(html)
    return _html_to_text_once(text) if looks_double_escaped(text) else text


def _html_to_text_once(html: str | None) -> str | None:
    if not html or not html.strip():
        return None
    tree = LexborHTMLParser(html)
    for node in tree.css("script, style, img"):
        node.decompose()
    for node in tree.css("li"):
        node.insert_before("\n- ")
    for node in tree.css(_BLOCK_TAGS):  # text may sit before the first block, unwrapped
        node.insert_before("\n")
        node.insert_after("\n")
    root = tree.body or tree.root
    text = root.text(separator="") if root else ""
    lines = [re.sub(r"[ \t\u00a0]+", " ", line).strip() for line in text.splitlines()]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return text or None


def fix_mojibake(text: str | None) -> str | None:
    """Repair UTF-8 that was decoded as Latin-1 upstream ("ð\\x9f\\x9a\\x80" -> "🚀").

    Only applied when the whole string round-trips, so correct text is left alone.
    """
    if not text:
        return text
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


_COMPANY_SUFFIXES = (
    r"inc|incorporated|llc|l\.l\.c|ltd|limited|gmbh|corp|corporation|co|company|plc|ag|sa|s\.a"
    r"|sas|sl|srl|bv|b\.v|pty|oy|ab|as|lda|ltda|sp z o\.?o"
)
_COMPANY_SUFFIX_RE = re.compile(rf"[\s,]+(?:{_COMPANY_SUFFIXES})\.?$", re.IGNORECASE)
_DOMAIN_TLD_RE = re.compile(r"\.(?:com|io|ai|co|dev|net|org|app|tech)$", re.IGNORECASE)


def normalize_company(company: str | None) -> str | None:
    if not company or not company.strip():
        return None
    # "Acme (Remote, LATAM)", "Acme (raised $20M)": parentheticals aren't part of the name.
    name = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", company).strip() or company.strip()
    name = _DOMAIN_TLD_RE.sub("", name)
    # Suffixes can stack ("Foo Holdings, Inc.") only once in practice; strip twice to be safe.
    for _ in range(2):
        name = _COMPANY_SUFFIX_RE.sub("", name)
    name = re.sub(r"[^\w]+", " ", name.casefold()).strip()
    return name or None


# Parentheticals that only describe location/arrangement, not the role itself.
_TITLE_NOISE = re.compile(
    r"[\(\[][^\)\]]*\b(?:remote|anywhere|worldwide|global|latam|americas|emea|apac|europe"
    r"|us|usa|uk|eu|canada|timezone|time zone|utc|gmt|est|pst|cet|contract|full[- ]?time"
    r"|part[- ]?time|m/f/d|f/m/d|w/m/d|h/f|all genders)\b[^\)\]]*[\)\]]",
    re.IGNORECASE,
)
_TITLE_TRAILING_REMOTE = re.compile(r"\s*[-\u2013|,]\s*(?:fully\s+)?remote\b.*$", re.IGNORECASE)
_TITLE_SYNONYMS = [
    (re.compile(r"\bsr\b\.?"), "senior"),
    (re.compile(r"\bjr\b\.?"), "junior"),
    (re.compile(r"\bfull[\s-]?stack\b"), "fullstack"),
    (re.compile(r"\bback[\s-]?end\b"), "backend"),
    (re.compile(r"\bfront[\s-]?end\b"), "frontend"),
    (re.compile(r"\bdev[\s-]?ops\b"), "devops"),
]


def normalize_title(title: str) -> str:
    text = _TITLE_NOISE.sub(" ", title)
    text = _TITLE_TRAILING_REMOTE.sub("", text).casefold()
    for pattern, replacement in _TITLE_SYNONYMS:
        text = pattern.sub(replacement, text)
    text = re.sub(r"[^\w+#]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def dedupe_key(values: list[str | None]) -> str:
    return hashlib.sha256("\x1f".join(v or "" for v in values).encode()).hexdigest()


@dataclass(frozen=True)
class Salary:
    min: float | None
    max: float | None
    currency: str | None
    period: str | None  # hour | day | week | month | year, or None when unclear


_CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "R$": "BRL", "C$": "CAD", "A$": "AUD"}
_CUR = r"USD|EUR|GBP|CAD|AUD|BRL|MXN|COP|ARS|CLP|PEN|CHF|INR|PLN|SEK|NOK|DKK|R\$|C\$|A\$|[$€£]"
_NUM = r"\d{1,3}(?:[,.\s]\d{3})+|\d+(?:\.\d+)?"
_MULT = r"[kK]|[mM](?![a-zA-Z])"
_SALARY_RE = re.compile(
    rf"(?P<cur1>{_CUR})?\s*(?<![\d,.])(?P<a1>{_NUM})\s*(?P<k1>{_MULT})?"  # not mid-number
    rf"(?:\s*(?:-|\u2013|\u2014|to)\s*(?P<cur2>{_CUR})?\s*(?P<a2>{_NUM})\s*(?P<k2>{_MULT})?)?"
    rf"\s*(?P<cur3>{_CUR})?",
    re.IGNORECASE,
)
_FUNDING_RE = re.compile(
    r"\b(?:raised|raising|funding|funded|seed|series [a-f]|valuation|revenue|arr|investors?)\b"
)
_HARD_CURRENCIES = {"USD", "EUR", "GBP", "CAD", "AUD", "CHF"}
_PERIODS = [
    ("hour", r"/\s*h(?:ou)?r\b|/h\b|per hour|hourly|an hour|p/h"),
    ("day", r"/\s*day\b|per day|daily rate|a day"),
    ("week", r"/\s*w(?:ee)?k\b|per week|weekly"),
    ("month", r"/\s*mo(?:nth)?\b|per month|monthly|a month|/m[eê]s|al mes|mensual"),
    ("year", r"/\s*y(?:ea)?r\b|per year|per annum|p\.?a\.?\b|annual(?:ly)?|yearly|a year|/año"),
]


def _amount(number: str, multiplier: str | None) -> float:
    digits = re.sub(r"[,.\s](?=\d{3}\b)", "", number)
    value = float(digits)
    if multiplier and multiplier.lower() == "k":
        value *= 1_000
    elif multiplier and multiplier.lower() == "m":
        value *= 1_000_000
    return value


def parse_salary(text: str | None) -> Salary | None:
    """Best-effort parse of a free-text salary ("$120k - $150k", "EUR 2,000-3,000 per month").

    Requires a currency marker so that stray numbers ("5 years", "Series B") are ignored.
    Returns None when nothing salary-like is found.
    """
    if not text:
        return None
    for match in _SALARY_RE.finditer(text):
        currency_raw = match["cur1"] or match["cur2"] or match["cur3"]
        if not currency_raw:
            continue
        low = _amount(match["a1"], match["k1"])
        high = _amount(match["a2"], match["k2"]) if match["a2"] else None
        if high is not None and match["k2"] and not match["k1"] and low < 1000:
            low *= 1_000  # "$120-150k"
        if low == 0 and not high:
            continue
        currency = _CURRENCY_SYMBOLS.get(currency_raw, currency_raw.upper())
        context = text[max(0, match.start() - 12) : match.end() + 15].lower()
        if _FUNDING_RE.search(context):
            continue  # "$1.5M seed round", "raised $20M"
        if currency in _HARD_CURRENCIES and max(low, high or 0) > 1_500_000:
            continue
        tail = text[match.end() : match.end() + 40].lower()
        period = next((name for name, pat in _PERIODS if re.search(pat, tail)), None)
        top = max(low, high or 0)
        if period is None and top >= 20_000:
            period = "year"
        if currency in _HARD_CURRENCIES and (
            (period == "year" and top < 1_000) or (period == "hour" and top > 1_000)
        ):
            continue  # implausible, usually a typo upstream ("$95,00-$115,000")
        return Salary(
            min=low, max=high if high is not None else low, currency=currency, period=period
        )
    return None


def struct_time_to_utc(value: time.struct_time | None) -> datetime | None:
    """feedparser's *_parsed fields are UTC struct_times."""
    if value is None:
        return None
    return datetime(*value[:6], tzinfo=UTC)


def iso_to_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)

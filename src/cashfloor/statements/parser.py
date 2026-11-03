"""Extract structured fields from statement text.

Strategy: a generic regex pass that works on most US bank / card / utility
statements, plus optional per-institution overrides (`INSTITUTION_HINTS`)
keyed on text that reliably appears in that institution's PDFs.  Anything
with low confidence is flagged `needs_review` and parked in the archive's
`_review/` folder rather than guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
DATE_PATTERNS = [
    # 01/31/2026, 1/31/26
    re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b"),
    # January 31, 2026 / Jan 31, 2026 / Jan. 31 2026
    re.compile(rf"\b((?:{MONTHS})[a-z]*)\.?\s+(\d{{1,2}}),?\s+(\d{{4}})\b", re.I),
    # 31 Jan 2026
    re.compile(rf"\b(\d{{1,2}})\s+((?:{MONTHS})[a-z]*)\.?\s+(\d{{4}})\b", re.I),
    # 2026-01-31
    re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"),
]

MONEY = r"\$?\s*\(?-?\$?\s*([\d,]+\.\d{2})\)?"

FIELD_PATTERNS: dict[str, list[re.Pattern]] = {
    "due_date": [
        re.compile(r"(?:payment\s+due\s+date|due\s+date|due\s+by|due\s+on|pay\s+by)\s*:?\s*(?P<d>[^\n]{6,32})", re.I),
    ],
    "amount_due": [
        re.compile(rf"(?:new\s+balance|total\s+amount\s+due|amount\s+due|balance\s+due|please\s+pay|total\s+due|statement\s+balance|new\s+balance\s+total)\s*:?\s*{MONEY}", re.I),
    ],
    "minimum_due": [
        re.compile(rf"(?:minimum\s+payment\s+due|minimum\s+payment|minimum\s+due|min(?:imum)?\s+amount\s+due)\s*:?\s*{MONEY}", re.I),
    ],
    "closing_balance": [
        re.compile(rf"(?:ending\s+balance|closing\s+balance|new\s+balance|balance\s+as\s+of)\s*(?:on\s+[A-Za-z0-9/, ]+)?\s*:?\s*{MONEY}", re.I),
    ],
    "statement_date": [
        re.compile(r"(?:statement\s+date|statement\s+closing\s+date|closing\s+date|billing\s+date|bill\s+date|invoice\s+date)\s*:?\s*(?P<d>[^\n]{6,32})", re.I),
    ],
}

PERIOD_PATTERN = re.compile(
    r"(?:statement\s+period|billing\s+period|billing\s+cycle|service\s+period|for\s+the\s+period|period)\s*:?\s*"
    r"(?P<a>[A-Za-z0-9/,.\- ]{6,20}?\d{2,4})\s*(?:-|–|—|to|through|thru)\s*(?P<b>[^\n]{6,32})",
    re.I,
)

LAST4_PATTERNS = [
    re.compile(r"(?:account|acct|card)\s*(?:number|no\.?|#)?\s*(?:ending|ending\s+in)?\s*:?\s*(?:[Xx*•]{2,}[\s\-]?)+(\d{4})\b", re.I),
    re.compile(r"\b(?:[Xx*•]{4}[\s\-]?){2,3}(\d{4})\b"),
    re.compile(r"(?:account|acct)\s*(?:number|no\.?|#)\s*:?\s*(\d{6,17})\b", re.I),
]

# Text that identifies an institution -> canonical folder name.  Extend freely.
INSTITUTION_HINTS: dict[str, str] = {
    "usaa": "USAA",
    "navy federal": "Navy Federal",
    "chase": "Chase",
    "jpmorgan": "Chase",
    "bank of america": "Bank of America",
    "wells fargo": "Wells Fargo",
    "citi": "Citi",
    "capital one": "Capital One",
    "american express": "American Express",
    "discover": "Discover",
    "u.s. bank": "US Bank",
    "us bank": "US Bank",
    "ally": "Ally",
    "schwab": "Schwab",
    "fidelity": "Fidelity",
    "synchrony": "Synchrony",
    "pg&e": "PGE",
    "pacific gas and electric": "PGE",
    "southern california edison": "SCE",
    "comcast": "Comcast",
    "xfinity": "Comcast",
    "at&t": "ATT",
    "verizon": "Verizon",
    "t-mobile": "T-Mobile",
}


@dataclass
class ParsedStatement:
    institution: str | None = None
    last4: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    statement_date: date | None = None
    due_date: date | None = None
    amount_due: Decimal | None = None
    minimum_due: Decimal | None = None
    closing_balance: Decimal | None = None
    confidence: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def best_date(self) -> date | None:
        """The date used for naming: statement date, else period end, else due date."""
        return self.statement_date or self.period_end or self.due_date


def parse_date(s: str) -> date | None:
    s = s.strip().strip(".,:")
    for pat in DATE_PATTERNS:
        m = pat.search(s)
        if not m:
            continue
        g = m.groups()
        try:
            if pat is DATE_PATTERNS[0]:
                mo, d, y = int(g[0]), int(g[1]), int(g[2])
                if y < 100:
                    y += 2000
                return date(y, mo, d)
            if pat is DATE_PATTERNS[1]:
                return datetime.strptime(f"{g[0][:3]} {g[1]} {g[2]}", "%b %d %Y").date()
            if pat is DATE_PATTERNS[2]:
                return datetime.strptime(f"{g[1][:3]} {g[0]} {g[2]}", "%b %d %Y").date()
            return date(int(g[0]), int(g[1]), int(g[2]))
        except ValueError:
            continue
    return None


def parse_money(s: str) -> Decimal | None:
    try:
        return Decimal(s.replace(",", ""))
    except (InvalidOperation, AttributeError):
        return None


def _first(text: str, pats: list[re.Pattern]) -> re.Match | None:
    for p in pats:
        m = p.search(text)
        if m:
            return m
    return None


def detect_institution(text: str, hints: dict[str, str] | None = None) -> str | None:
    """Most-mentioned institution in the first page or so, matching whole
    words only (so "ally" never matches "automatically" and a payment line
    mentioning another bank doesn't outvote the letterhead)."""
    head = text[:4000].lower()
    scores: dict[str, int] = {}
    for needle, name in (hints or INSTITUTION_HINTS).items():
        n = len(re.findall(r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])", head))
        if n:
            # earlier first mention breaks ties (letterhead beats transaction lines)
            scores[name] = scores.get(name, 0) + n * 1000 - head.find(needle) // 10
    return max(scores, key=scores.get) if scores else None


def parse_statement_text(text: str, hints: dict[str, str] | None = None) -> ParsedStatement:
    ps = ParsedStatement()
    flat = re.sub(r"[ \t]+", " ", text)
    ps.institution = detect_institution(flat, hints)

    for pat in LAST4_PATTERNS:
        m = pat.search(flat)
        if m:
            ps.last4 = m.group(1)[-4:]
            break

    m = PERIOD_PATTERN.search(flat)
    if m:
        ps.period_start = parse_date(m.group("a"))
        ps.period_end = parse_date(m.group("b"))

    for fld in ("statement_date", "due_date"):
        m = _first(flat, FIELD_PATTERNS[fld])
        if m:
            setattr(ps, fld, parse_date(m.group("d")))

    for fld in ("amount_due", "minimum_due", "closing_balance"):
        m = _first(flat, FIELD_PATTERNS[fld])
        if m:
            setattr(ps, fld, parse_money(m.group(1)))

    # Utilities / cards often print "New balance" as both due and closing.
    if ps.amount_due is None and ps.closing_balance is not None and ps.due_date:
        ps.amount_due = ps.closing_balance

    score = 0.0
    score += 0.3 if ps.institution else 0
    score += 0.25 if ps.last4 else 0
    score += 0.3 if ps.best_date else 0
    score += 0.15 if (ps.amount_due is not None or ps.closing_balance is not None) else 0
    ps.confidence = round(score, 2)
    if not ps.institution:
        ps.notes.append("institution not recognized")
    if not ps.best_date:
        ps.notes.append("no statement/period/due date found")
    return ps

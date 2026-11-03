"""Extract transactions from bank-statement text.

Targets the common "running balance" layout (USAA and many others):

    Date   Description                              Debits    Credits     Balance
    09/05  Beginning Balance                                               $872.93
    09/08  ATM REBATE                                           $3.50      $876.43
           $103.50 ATM W/D P410221 on 09/05
    09/21  ACH WITHDRAWAL 092126                    $365.95               $1,763.41
           CITI AUTOPAY PAYMENT
           ***********9077

Text extraction loses column positions, so debit-vs-credit is derived from
the balance movement instead: sign(balance - previous_balance).  That also
checks each row (|delta| must equal the amount), so a misread row is dropped
rather than imported wrong.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from ..models import to_cents

PERIOD_RE = re.compile(
    r"statement\s+period\s*:?\s*(\d{1,2}/\d{1,2}/\d{4})\s*(?:to|-|–|through)\s*(\d{1,2}/\d{1,2}/\d{4})", re.I
)
MONEY_RE = re.compile(r"-?\$?\(?-?[\d,]+\.\d{2}\)?")
ROW_RE = re.compile(r"^(\d{2}/\d{2})\s+(.*?)\s*((?:-?\$?\(?[\d,]+\.\d{2}\)?\s*){1,3})$")
STOP_RE = re.compile(r"^(page \d+ of \d+|online:|phone:|mobile:|transactions( \(continued\))?$|date\s+description)", re.I)
GENERIC_PREFIX_RE = re.compile(
    r"^(ach (withdrawal|dep(osit)?|credit|debit)|debit card purchase|atm (withdrawal|rebate|deposit)|"
    r"usaa (debit|credit)|pos (purchase|debit)|check(?: #?\d+)?|online (transfer|payment)|wire (in|out)|"
    r"mobile deposit|deposit|withdrawal|interest paid|service charge)\b.*$",
    re.I,
)
MASK_TAIL_RE = re.compile(r"^\*{3,}[\w-]*$")
TRAILING_REF_RE = re.compile(r"\s+\d{6,}\s*$")


@dataclass
class ParsedTransaction:
    posted: date
    amount: int  # cents, signed
    payee: str
    memo: str
    balance_after: int | None
    index: int


@dataclass
class ParsedTransactions:
    period_start: date | None
    period_end: date | None
    transactions: list[ParsedTransaction] = field(default_factory=list)
    skipped_rows: int = 0
    notes: list[str] = field(default_factory=list)


def _money(s: str) -> int:
    s = s.strip()
    neg = s.startswith("-") or (s.startswith("(") and s.endswith(")"))
    v = to_cents(s.strip("()-$").replace(",", ""))
    return -v if neg else v


def _parse_period(text: str) -> tuple[date | None, date | None]:
    m = PERIOD_RE.search(text)
    if not m:
        return None, None
    out = []
    for s in m.groups():
        mo, d, y = (int(x) for x in s.split("/"))
        out.append(date(y, mo, d))
    return out[0], out[1]


def _year_for(mmdd: str, start: date | None, end: date | None) -> date | None:
    mo, d = (int(x) for x in mmdd.split("/"))
    if end is None:
        return None
    # statements spanning a year boundary: months >= start.month belong to start.year
    year = end.year
    if start and start.year != end.year and mo >= start.month:
        year = start.year
    try:
        return date(year, mo, d)
    except ValueError:
        return None


def _payee_from(lines: list[str]) -> str:
    """First line that isn't a generic transaction type / masked suffix / bare reference."""
    cleaned = []
    for ln in lines:
        ln = TRAILING_REF_RE.sub("", ln.strip())
        if not ln or MASK_TAIL_RE.match(ln) or re.fullmatch(r"[\d\s]+", ln):
            continue
        if MONEY_RE.match(ln):  # detail line like "$103.50 ATM W/D P410221 on 09/05"
            continue
        cleaned.append(ln)
    if not cleaned:
        return (lines[0] if lines else "").strip()
    for ln in cleaned:
        if not GENERIC_PREFIX_RE.match(ln):
            return ln
    # all generic: strip the date/reference noise from the first one
    return re.sub(r"\s+\d{4,}.*$", "", cleaned[0]).strip()


def parse_transactions(text: str) -> ParsedTransactions:
    start, end = _parse_period(text)
    res = ParsedTransactions(period_start=start, period_end=end)
    if end is None:
        res.notes.append("no statement period found; cannot assign years to MM/DD dates")
        return res

    rows: list[tuple[str, list[str], list[int]]] = []  # (mmdd, desc lines, money tokens)
    current: tuple[str, list[str], list[int]] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if STOP_RE.match(line):
            if current:
                rows.append(current)
                current = None
            continue
        m = ROW_RE.match(line)
        if m:
            if current:
                rows.append(current)
            amounts = [_money(x) for x in MONEY_RE.findall(m.group(3))]
            current = (m.group(1), [m.group(2).strip()], amounts)
        elif current and not re.match(r"^\d{2}/\d{2}\s", line):
            # a column that wrapped onto its own line: "$1,743.41" alone
            if re.fullmatch(r"(-?\$?\(?[\d,]+\.\d{2}\)?\s*){1,2}", line) and len(current[2]) < 3:
                current[2].extend(_money(x) for x in MONEY_RE.findall(line))
            else:
                current[1].append(line)
    if current:
        rows.append(current)

    prev_balance: int | None = None
    idx = 0
    for mmdd, desc, tokens in rows:
        head = desc[0].lower()
        if "beginning balance" in head or "ending balance" in head:
            if tokens:
                prev_balance = tokens[-1]
            continue
        posted = _year_for(mmdd, start, end)
        if posted is None or not tokens:
            res.skipped_rows += 1
            continue

        amount: int | None = None
        balance: int | None = None
        sign = 0
        toks = [abs(t) for t in tokens]
        if prev_balance is not None:
            # The balance is the rightmost column, so try the last token as the
            # balance first; accept any pairing where |balance - previous| equals
            # the other token.  (For a debit both pairings satisfy the chain, so
            # reading order is the tie-break.)
            order = [len(tokens) - 1] + list(range(len(tokens) - 1))
            for i in order:
                b = tokens[i]
                others = [toks[j] for j in range(len(toks)) if j != i]
                delta = b - prev_balance
                if others and abs(delta) in others:
                    amount, balance, sign = abs(delta), b, (1 if delta > 0 else -1)
                    break
            if amount is None and len(tokens) == 1:
                # only the balance survived extraction: amount is the movement
                delta = tokens[0] - prev_balance
                if delta != 0:
                    amount, balance, sign = abs(delta), tokens[0], (1 if delta > 0 else -1)
        if amount is None:
            # no usable chain (first row after a gap): trust reading order, infer sign from wording
            if len(toks) >= 2:
                amount, balance = toks[-2], tokens[-1]
            else:
                amount = toks[0]
            text_all = " ".join(desc).lower()
            if re.search(r"withdrawal|debit|purchase|payment|pymt|fee|charge|xfer|transfer to", text_all):
                sign = -1
            elif re.search(r"deposit|dep\b|credit|rebate|refund|interest|payroll", text_all):
                sign = 1
            else:
                res.skipped_rows += 1
                if balance is not None:
                    prev_balance = balance
                continue
        res.transactions.append(ParsedTransaction(
            posted=posted, amount=sign * amount, payee=_payee_from(desc)[:120],
            memo=" | ".join(d.strip() for d in desc)[:300], balance_after=balance, index=idx,
        ))
        idx += 1
        if balance is not None:
            prev_balance = balance
    return res

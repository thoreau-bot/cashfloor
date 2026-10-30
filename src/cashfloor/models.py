"""Plain dataclasses that mirror the SQLite schema.

Money is stored as integer cents everywhere internally to avoid float drift.
Sign convention: positive = money into the account, negative = money out.
For credit cards, `balance` is what you OWE expressed as a negative number
(matches SimpleFIN's convention), so a card with $1,200 charged has
balance = -120000.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum


class AccountType(StrEnum):
    CHECKING = "checking"
    SAVINGS = "savings"
    CREDIT = "credit"
    LOAN = "loan"
    UTILITY = "utility"
    INVESTMENT = "investment"
    OTHER = "other"


class ItemSource(StrEnum):
    MANUAL = "manual"
    DETECTED = "detected"  # from recurring-transaction detection
    DUE_DATE = "due_date"  # derived from a statement / account due date
    TRANSFER = "transfer"


class Frequency(StrEnum):
    ONCE = "once"
    WEEKLY = "weekly"
    BIWEEKLY = "biweekly"
    SEMIMONTHLY = "semimonthly"  # 1st & 15th style
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    YEARLY = "yearly"


def to_cents(amount: float | str | int) -> int:
    from decimal import ROUND_HALF_UP, Decimal

    return int((Decimal(str(amount)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def from_cents(cents: int) -> float:
    return cents / 100.0


@dataclass
class Account:
    id: int | None
    provider: str
    external_id: str
    name: str
    institution: str
    type: AccountType = AccountType.OTHER
    key: str | None = None  # short user-chosen handle, e.g. "main-checking"
    nickname: str | None = None
    currency: str = "USD"
    last4: str | None = None
    balance: int = 0  # cents
    available: int | None = None
    credit_limit: int | None = None
    statement_balance: int | None = None  # cents, cards/loans/utilities
    minimum_payment: int | None = None
    due_date: date | None = None
    in_pool: bool = False
    balance_as_of: datetime | None = None
    last_synced: datetime | None = None
    active: bool = True
    conn_id: str | None = None  # provider connection (SimpleFIN v2)
    extra: dict | None = None  # server-defined extra data, kept verbatim
    sync_error: str | None = None  # last provider error touching this account

    @property
    def display_name(self) -> str:
        return self.nickname or self.name

    @property
    def spendable(self) -> int:
        """Available balance when the provider reports one, else balance."""
        return self.available if self.available is not None else self.balance


@dataclass
class Connection:
    """A login at an institution, as seen by the provider.  One connection
    can carry several accounts.  This is what needs re-auth when it breaks."""

    provider: str
    conn_id: str
    name: str
    org_id: str | None = None
    org_url: str | None = None
    sfin_url: str | None = None
    error_code: str | None = None
    error_msg: str | None = None
    last_seen: datetime | None = None


@dataclass
class Holding:
    account_id: int
    external_id: str
    symbol: str | None
    description: str
    shares: int | None  # thousandths of a share (shares * 1000) to keep integer math
    cost_basis: int | None  # cents
    purchase_price: int | None  # cents
    market_value: int | None  # cents
    currency: str = "USD"
    created: datetime | None = None
    as_of: datetime | None = None


@dataclass
class Transaction:
    id: int | None
    account_id: int
    external_id: str
    posted: date
    amount: int  # cents, signed
    payee: str
    memo: str = ""
    pending: bool = False
    category: str | None = None
    transacted_at: datetime | None = None
    extra: dict | None = None


@dataclass
class ScheduledItem:
    """A future cash movement used by the projection engine."""

    id: int | None
    name: str
    account_id: int  # account the money leaves / enters
    amount: int  # cents, signed from the account's point of view
    frequency: Frequency
    next_date: date
    source: ItemSource = ItemSource.MANUAL
    confidence: float = 1.0
    active: bool = True
    confirmed: bool = True  # detected items start unconfirmed
    end_date: date | None = None
    counter_account_id: int | None = None  # for transfers
    payee_pattern: str | None = None  # normalized payee the detector matched
    notes: str = ""
    # Business-day handling: "none" | "previous" | "next"
    weekend_rule: str = "none"


@dataclass
class Statement:
    id: int | None
    account_id: int | None
    institution: str
    period_start: date | None
    period_end: date | None
    statement_date: date | None
    due_date: date | None
    amount_due: int | None
    minimum_due: int | None
    closing_balance: int | None
    last4: str | None
    source_path: str
    archived_path: str | None
    sha256: str
    parsed_ok: bool = True
    needs_review: bool = False
    imported_at: datetime = field(default_factory=datetime.now)
    title: str | None = None  # provider-supplied document title, e.g. "CHECKING STATEMENT"


@dataclass
class ProjectionDay:
    day: date
    balance: int  # cents, pool total at end of day
    inflows: int
    outflows: int
    events: list[str] = field(default_factory=list)


@dataclass
class ProjectionResult:
    as_of: date
    horizon_days: int
    starting_balance: int
    days: list[ProjectionDay]
    floor: int
    floor_date: date
    safety_buffer: int
    discretionary: int
    unconfirmed_count: int
    warnings: list[str] = field(default_factory=list)  # stale / erroring pool accounts

    def to_dict(self) -> dict:
        return {
            "as_of": self.as_of.isoformat(),
            "horizon_days": self.horizon_days,
            "starting_balance": from_cents(self.starting_balance),
            "floor": from_cents(self.floor),
            "floor_date": self.floor_date.isoformat(),
            "safety_buffer": from_cents(self.safety_buffer),
            "discretionary": from_cents(self.discretionary),
            "unconfirmed_count": self.unconfirmed_count,
            "warnings": self.warnings,
            "days": [
                {
                    "day": d.day.isoformat(),
                    "balance": from_cents(d.balance),
                    "inflows": from_cents(d.inflows),
                    "outflows": from_cents(d.outflows),
                    "events": d.events,
                }
                for d in self.days
            ],
        }

"""Net worth: what each account holds or owes now, and at past month-ends.

A Plaid account's balance is its newest ``balances.jsonl`` line with a current
balance. A past day's is worked back from it: today's balance less the posted
rows after that day, a bank account's the one way and a card's the other (the
checks' ``sign``). Plaid's history starts on the same day for every account of
an Item, so a month-end before the latest Item's first row is left out.

The Apple Card has no Plaid Item. Its balance on a day is the newest statement
ending on or before it plus the Wallet rows billed after that statement, so a
calendar month-end is that month's own statement. Its newest figure is only as
fresh as the newest Wallet export. A Plaid card with statement PDFs whose feed
lacks reward credits (OnePay) is worked forward from its statements the same
way, the PDFs supplying the credits: worked back from today it drifted by the
sum of every redemption since.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from . import apple, check, statements
from .plaid import Store

ASSETS = ("depository", "investment")  # Plaid account types that hold money; the rest owe it
GROUPS = (("depository", "Cash"), ("investment", "Investments"), ("credit", "Credit cards"), ("loan", "Loans"))
APPLE_KEY = "apple"


@dataclass
class Account:
    key: str  # Plaid account_id, or APPLE_KEY
    name: str
    mask: str
    institution: str
    type: str  # Plaid's account type: depository, credit, loan, investment
    subtype: str
    balance: float  # held, or owed on a card or loan: never signed by direction
    as_of: date
    available: float | None = None
    limit: float | None = None
    statement: float | None = None  # the last statement's balance
    minimum: float | None = None
    due: date | None = None
    source: str = "Plaid"

    @property
    def asset(self) -> bool:
        return self.type in ASSETS

    @property
    def net(self) -> float:
        """What it adds to net worth."""
        return self.balance if self.asset else -self.balance

    @property
    def used(self) -> float | None:
        """A card's balance over its limit."""
        return self.balance / self.limit if self.limit else None


@dataclass
class Point:
    day: date
    assets: float
    debts: float
    worked: bool  # some balance was worked back from rows, not read from a snapshot

    @property
    def net(self) -> float:
        return self.assets - self.debts


@dataclass
class NetWorth:
    accounts: list[Account]
    history: list[Point]  # month-ends, oldest first, then today
    first_snapshot: date | None  # the first day a Plaid balance was saved; before it every point is worked back

    @property
    def assets(self) -> float:
        return sum(a.balance for a in self.accounts if a.asset)

    @property
    def debts(self) -> float:
        return sum(a.balance for a in self.accounts if not a.asset)

    @property
    def net(self) -> float:
        return self.assets - self.debts


def _date(s: str | None) -> date | None:
    return date.fromisoformat(s) if s else None


def _month_ends(start: date, today: date, months: int) -> list[date]:
    out = []
    d = today.replace(day=1) - timedelta(days=1)
    while len(out) < months and d >= start:
        out.append(d)
        d = d.replace(day=1) - timedelta(days=1)
    return out[::-1]


def _from_statement(day: date, sts: list[statements.Statement], rows: list[dict], kinds: tuple[str, ...] = ()) -> float | None:
    """A card's balance at the end of ``day``: the newest statement ending on or
    before it, plus the rows billed since, plus the PDFs' own lines of ``kinds``
    (what the feed lacks) dated since. None before its first statement."""
    st = max((s for s in sts if s.end <= day), key=lambda s: s.end, default=None)
    if st is None:
        return None
    rows_since = sum(t["amount"] for t in rows if not t.get("pending") and st.end < check._billed(t) <= day)
    lines_since = sum(ln.amount for s in sts for ln in s.lines if ln.kind in kinds and st.end < ln.date <= day)
    return round(st.new + rows_since + lines_since, 2)


def build(store: Store, sts: list[statements.Statement], apple_rows: list[dict], today: date,
          lacks_rewards: frozenset[str] = frozenset(), months: int = 12) -> NetWorth:
    """``sts`` are the parsed statement PDFs, Apple's and the rest; ``lacks_rewards``
    the masks whose feed has no reward credits (``check.reward_rates``)."""
    items = store.items()
    history: dict[str, list[dict]] = {}
    for line in store.balances():
        if line.get("current") is not None:
            history.setdefault(line["account_id"], []).append(line)
    rows: dict[str, list[dict]] = {}
    starts = []
    for item_id in items:
        txns = list(store.ledger(item_id)["transactions"].values())
        if txns:
            starts.append(min(date.fromisoformat(t["date"]) for t in txns))
        for t in txns:
            if not t.get("pending"):
                rows.setdefault(t["account_id"], []).append(t)

    accounts: list[Account] = []
    for acct_id, snaps in history.items():
        s = snaps[-1]
        item = items.get(s.get("item_id"), {})
        accounts.append(Account(
            acct_id, s.get("name") or "?", s.get("mask") or "", item.get("institution") or "", s.get("type") or "",
            s.get("subtype") or "", round(s["current"], 2), check.local_day(s["at"]), s.get("available"), s.get("limit"),
            s.get("last_statement_balance"), s.get("minimum_payment_amount"), _date(s.get("next_payment_due_date"))))
    apple_sts = sorted((s for s in sts if s.issuer == "apple"), key=lambda s: s.end)
    if apple_sts:
        newest = max([apple_sts[-1].end] + [check._billed(t) for t in apple_rows if not t.get("pending")])
        accounts.append(Account(APPLE_KEY, check.APPLE, "", "Apple", "credit", "credit card",
                                _from_statement(newest, apple_sts, apple_rows), newest, statement=apple_sts[-1].new,
                                source="statement + Wallet export"))
    order = {t: n for n, (t, _) in enumerate(GROUPS)}
    accounts.sort(key=lambda a: (order.get(a.type, len(order)), -a.balance))

    days = {check.local_day(line["at"]) for snaps in history.values() for line in snaps}
    first = min(days) if days else None
    start = max(starts) if starts else today
    if apple_sts:
        start = max(start, apple_sts[0].end)
    points = []
    for d in _month_ends(start, today, months):
        assets = debts = 0.0
        worked = False
        for a in accounts:
            snap = None if a.key == APPLE_KEY else next((x for x in reversed(history[a.key]) if check.local_day(x["at"]) == d), None)
            if a.key == APPLE_KEY:
                bal = _from_statement(d, apple_sts, apple_rows) or 0.0
            elif not snap and a.mask in lacks_rewards and (fwd := _from_statement(d, [s for s in sts if s.mask == a.mask], rows.get(a.key, []), ("reward credit",))) is not None:
                bal = fwd
            else:
                if snap:
                    bal = snap["current"]
                else:
                    worked = True
                    upto = a.as_of
                    moved = sum(t["amount"] for t in rows.get(a.key, []) if d < date.fromisoformat(t["date"]) <= upto)
                    bal = a.balance - check.sign({"type": a.type}) * moved
            if a.asset:
                assets += bal
            else:
                debts += bal
        points.append(Point(d, round(assets, 2), round(debts, 2), worked))
    if accounts:
        points.append(Point(today, round(sum(a.balance for a in accounts if a.asset), 2),
                            round(sum(a.balance for a in accounts if not a.asset), 2), False))
    return NetWorth(accounts, points, first)


def load(root: Path, today: date, sts: list[statements.Statement] | None = None, lacks_rewards: frozenset[str] = frozenset(),
         env: str = "production") -> NetWorth | None:
    """None where there's no Plaid store (opening one would create it).
    ``sts`` are the parsed statement PDFs, when the caller has them already."""
    if not (root / "data" / "plaid" / env).is_dir():
        return None
    if sts is None:
        sts, _ = statements.load_dir(root / "data" / "statements", root / "data" / "apple")
    return build(Store(root, env), sts, apple.load(root / "data" / "apple"), today, lacks_rewards)

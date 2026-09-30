"""Read a Rocket Money transaction export.

The export (Settings → Export transactions, arrives by email) is a CSV with
these columns as of 2026: Date, Original Date, Account Type, Account Name,
Account Number, Institution Name, Name, Custom Name, Amount, Description,
Category, Note, Ignored From, Tax Deductible.

Only Date, Name, Amount and Category are required here; everything else is
optional so a changed export still loads. Amount sign is normalised so that
expenses are positive, using ``[export].expense_sign`` from rules.toml —
run ``penny inspect`` on a real export to confirm which way it goes.
"""

from __future__ import annotations

import csv
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

REQUIRED = ("Date", "Name", "Amount", "Category")
DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y")


@dataclass
class Txn:
    date: date
    name: str
    amount: float  # positive = money out
    category: str
    account: str = ""
    account_number: str = ""
    custom_name: str = ""
    description: str = ""
    ignored_from: str = ""
    family: str | None = field(default=None, compare=False)
    # Set by the Plaid + Apple feed (feed.py); a Rocket Money row keeps the defaults.
    kind: str = "purchase"
    source: str = "rocket_money"
    posted: date | None = None
    transfer: bool = False
    txn_id: str = ""
    channel: str = ""  # Plaid's payment_channel: online, in store, other
    mcc: str = ""  # merchant category code, where Plaid has one
    budget_category: str = ""  # what the money was for (categorize.assign_categories); `category` is the source's own
    account_type: str = ""  # Plaid's account type: credit, depository; Apple Card is credit
    wallet: bool = False  # a payment app's balance account (feed.WALLET_SUBTYPES), which a card can pay through
    paid_by: str = ""  # on a wallet row another account's row paid for: that row's txn_id (feed.pair_wallets)

    @property
    def match_text(self) -> str:
        """Text the categoriser matches against: custom name, name, description."""
        return " | ".join(x for x in (self.custom_name, self.name, self.description) if x).lower()


def _parse_date(s: str) -> date:
    s = s.strip()
    for fmt in DATE_FORMATS:
        try:
            return date(*time.strptime(s, fmt)[:3])
        except ValueError:
            continue
    raise ValueError(f"unrecognised date: {s!r}")


def _parse_amount(s: str) -> float:
    s = s.strip().replace("$", "").replace(",", "")
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    return float(s)


def load(
    path: str | Path,
    *,
    expense_sign: str = "positive",
    drop_ignored: bool = True,
    exclude_categories: list[str] | None = None,
    dedup_across_accounts: bool = True,
) -> list[Txn]:
    """Load the export. ``expense_sign`` is "positive" or "negative" — which sign
    the export uses for money leaving an account. ``exclude_categories`` drops
    Rocket Money categories that are not spend (paychecks, transfers, card
    payments) so they cannot net against a family. ``dedup_across_accounts``
    drops a row whose (date, name, amount, description) twin already appeared
    under a *different* account number — Rocket Money imports a re-issued card
    twice for the overlap (seen 2025-09 → 2025-12 on one Chase card, 305 rows).
    Twins inside one account are kept; two identical purchases in a day happen."""
    excl = {c.lower() for c in (exclude_categories or [])}
    if expense_sign not in ("positive", "negative"):
        raise ValueError("expense_sign must be 'positive' or 'negative'")
    flip = -1.0 if expense_sign == "negative" else 1.0
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        missing = [c for c in REQUIRED if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"export is missing columns {missing}; has {reader.fieldnames}")
        out: list[Txn] = []
        for row in reader:
            ignored = (row.get("Ignored From") or "").strip().lower()
            if drop_ignored and ignored == "everything":
                continue
            if (row.get("Category") or "").strip().lower() in excl:
                continue
            out.append(
                Txn(
                    date=_parse_date(row["Date"]),
                    name=(row.get("Name") or "").strip(),
                    amount=flip * _parse_amount(row["Amount"]),
                    category=(row.get("Category") or "").strip(),
                    account=(row.get("Account Name") or "").strip(),
                    account_number=(row.get("Account Number") or "").strip(),
                    custom_name=(row.get("Custom Name") or "").strip(),
                    description=(row.get("Description") or "").strip(),
                    ignored_from=ignored,
                )
            )
    if dedup_across_accounts:
        seen: dict[tuple, set[str]] = {}
        kept: list[Txn] = []
        for t in out:
            key = (t.date, t.name, round(t.amount, 2), t.description)
            accts = seen.setdefault(key, set())
            if accts and t.account_number not in accts:
                continue
            accts.add(t.account_number)
            kept.append(t)
        out = kept
    out.sort(key=lambda t: t.date)
    return out


def inspect(path: str | Path) -> dict:
    """Facts about an export that decide config: columns, date range, and which
    sign income carries (so expense_sign can be set correctly)."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        cols = list(reader.fieldnames or [])
        rows = list(reader)
    dates = sorted(_parse_date(r["Date"]) for r in rows if r.get("Date"))
    income_like = [
        _parse_amount(r["Amount"])
        for r in rows
        if any(k in (r.get("Category") or "").lower() for k in ("income", "paycheck", "salary", "deposit"))
    ]
    cats: dict[str, int] = {}
    for r in rows:
        c = r.get("Category") or ""
        cats[c] = cats.get(c, 0) + 1
    return {
        "columns": cols,
        "rows": len(rows),
        "first": dates[0] if dates else None,
        "last": dates[-1] if dates else None,
        "income_rows": len(income_like),
        "income_sum": sum(income_like),
        "expense_sign_guess": ("positive" if income_like and sum(income_like) < 0 else "negative" if income_like else "unknown"),
        "categories": sorted(cats.items(), key=lambda kv: -kv[1]),
    }

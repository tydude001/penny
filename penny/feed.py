"""The transaction feed: Plaid's ledgers plus the Apple Card CSVs, as ``Txn``.

This replaces the Rocket Money export (``load.py``) as the board's source.
Each row keeps the fields a budget needs beside the ones the card math reads:

- ``date`` is the transaction date (Plaid's ``authorized_date``, else its
  ``date``); ``posted`` is the posted date. Budgets and the card windows use
  ``date``.
- ``account_number`` is the account's last four digits (``mask``), which is
  what ``rules.toml``'s ``accounts`` restrictions name. Apple Card has none.
- ``category`` is Plaid's detailed ``personal_finance_category``, the one the
  families' ``categories`` fallbacks match. Apple's own categories are left
  empty: Wallet labels nearly every purchase "Other".
- ``kind``: purchase, refund, payment, transfer, income, fee, interest,
  installment or reward credit. Income is only ever into a bank account. ``transfer`` is True for anything that moves
  money between accounts (card payments included), which is never spend.

Plaid files a Chase card payment under ``LOAN_DISBURSEMENTS`` on the card and
``LOAN_PAYMENTS`` on the checking account; both halves are transfers. Money
sent through PayPal, Venmo or Zelle is ``TRANSFER_OUT`` from SoFi: Plaid can't
see what it bought, so it is a transfer here, not spend.

Pending rows are dropped; they count once posted.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from . import apple, csvimport, plaid
from . import categorize as cat
from .load import Txn

TRANSFER_PRIMARY = ("TRANSFER_IN", "TRANSFER_OUT", "LOAN_PAYMENTS", "LOAN_DISBURSEMENTS")
# A card payment Plaid left under OTHER (Chase's autopay, 2026-09).
PAYMENT_NAME = re.compile(r"\bpayment\b.*\bthank", re.IGNORECASE)
APPLE_ACCOUNT = "Apple Card"
OVERRIDES = Path("data") / "overrides.json"
MERCHANTS = Path("data") / "merchants.json"  # the same, keyed by merchant


def _kind(t: dict, pfc: dict, account_type: str = "") -> str:
    primary = pfc.get("primary") or ""
    if PAYMENT_NAME.search(t["name"]):
        return "payment"
    if primary in TRANSFER_PRIMARY:
        return "payment" if primary.startswith("LOAN_") else "transfer"
    if primary == "INCOME":
        # A card isn't paid wages: Plaid has filed merchant refunds on the
        # Chase cards as INCOME_CONTRACTOR.
        return "refund" if account_type == "credit" and t["amount"] < 0 else "income"
    if primary == "BANK_FEES":
        return "fee"
    return "refund" if t["amount"] < 0 else "purchase"


def from_plaid(store: plaid.Store) -> list[Txn]:
    out = []
    for item_id in store.items():
        ledger = store.ledger(item_id)
        accounts = ledger["accounts"]
        for t in ledger["transactions"].values():
            if t.get("pending"):
                continue
            a = accounts.get(t["account_id"], {})
            pfc = t.get("personal_finance_category") or {}
            kind = _kind(t, pfc, a.get("type") or "")
            out.append(Txn(
                date=date.fromisoformat(t.get("authorized_date") or t["date"]),
                name=t["name"],
                amount=round(t["amount"], 2),
                category=pfc.get("detailed") or pfc.get("primary") or "",
                account=a.get("name") or "",
                account_number=a.get("mask") or "",
                description=t.get("merchant_name") or "",
                kind=kind,
                source="plaid",
                posted=date.fromisoformat(t["date"]),
                transfer=kind in ("payment", "transfer"),
                txn_id=t["transaction_id"],
                channel=t.get("payment_channel") or "",
                mcc=t.get("merchant_category_code") or "",
                account_type=a.get("type") or "",
            ))
    return out


def from_apple(apple_dir: Path) -> list[Txn]:
    out = []
    for r in apple.load(apple_dir):
        if r["pending"]:
            continue
        out.append(Txn(
            date=date.fromisoformat(r["date"]),
            name=r["name"],
            amount=r["amount"],
            category="",
            account=APPLE_ACCOUNT,
            description=r.get("merchant_name") or "",
            kind=r["kind"],
            source="apple_csv",
            posted=date.fromisoformat(r["posted"]) if r["posted"] else None,
            transfer=r["kind"] == "payment",
            txn_id=r["transaction_id"],
            account_type="credit",
        ))
    return out


def is_spend(t: Txn) -> bool:
    """Counts toward spend: not a transfer or payment, not income."""
    return not t.transfer and t.kind != "income"


def load(root: Path, env: str = "production", *, spend_only: bool = True) -> list[Txn]:
    """Every posted row from Plaid, the Apple Card CSVs and ``penny import csv``
    (``data/imports/``), oldest first.
    ``spend_only`` drops transfers, card payments and income, as the Rocket
    Money loader's ``exclude_categories`` did."""
    txns = from_plaid(plaid.Store(root, env)) + from_apple(root / "data" / "apple") + csvimport.load(root / csvimport.IMPORTS)
    if spend_only:
        txns = [t for t in txns if is_spend(t)]
    txns.sort(key=lambda t: t.date)
    return txns


def labelled(root: Path, rules: dict, env: str = "production", *, spend_only: bool = True) -> list[Txn]:
    """``load`` with every label on: family, budget category, then the hand
    overrides, which beat both: per merchant in ``data/merchants.json``, then
    per row in ``data/overrides.json``."""
    txns = cat.categorize(load(root, env, spend_only=spend_only), cat.build_families(rules))
    cat.assign_categories(txns, rules)
    cat.apply_merchant_overrides(txns, cat.load_overrides(root / MERCHANTS))
    return cat.apply_overrides(txns, cat.load_overrides(root / OVERRIDES))

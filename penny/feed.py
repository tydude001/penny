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
- ``wallet`` marks a payment app's balance account, and ``paid_by`` a wallet
  row that another account's row paid for (``pair_wallets``).

Plaid files a Chase card payment under ``LOAN_DISBURSEMENTS`` on the card and
``LOAN_PAYMENTS`` on the checking account; both halves are transfers. Money
sent through PayPal, Venmo or Zelle is ``TRANSFER_OUT`` from SoFi: Plaid can't
see what it bought, so it is a transfer here, not spend.

A linked PayPal account does show what it bought, and then a purchase a card
paid for through PayPal is in the feed twice: on the card, and again on
PayPal. ``pair_wallets`` finds the PayPal half and makes it a transfer, so the
card's row is the one that counts. A purchase PayPal paid from its own balance
or from the bank (the bank's half is already a transfer) has no pair and stays
spend.

Pending rows are dropped; they count once posted.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date
from pathlib import Path

from . import apple, csvimport, plaid
from . import categorize as cat
from .load import Txn

TRANSFER_PRIMARY = ("TRANSFER_IN", "TRANSFER_OUT", "LOAN_PAYMENTS", "LOAN_DISBURSEMENTS")
# A card payment Plaid left under OTHER (Chase's autopay, 2026-09).
PAYMENT_NAME = re.compile(r"\bpayment\b.*\bthank", re.IGNORECASE)
APPLE_ACCOUNT = "Apple Card"
# Plaid's subtype for a payment app's balance account. Only the depository
# account is a wallet: PayPal's credit card is a card like any other.
WALLET_SUBTYPES = ("paypal",)
# How far the paying row's date may sit from the wallet row's, in days. A card
# authorises the same day, give or take a time zone; a debit from a bank can
# post most of a week later. A monthly subscription's next charge is 28 days
# off, so the window can't reach it.
PAIR_DAYS_BEFORE = 2
PAIR_DAYS_AFTER = 5
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
                wallet=a.get("type") == "depository" and a.get("subtype") in WALLET_SUBTYPES,
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


def pair_wallets(txns: list[Txn]) -> int:
    """Mark each wallet purchase or refund that another account's row paid
    for, and return how many. The pair is a row of the same kind and the same
    amount to the cent, on an account that isn't a wallet, dated within
    ``PAIR_DAYS_BEFORE``/``PAIR_DAYS_AFTER`` of the wallet's. The wallet row
    becomes a transfer with ``paid_by`` naming its pair; the paying row is
    untouched, because that is the row the card earned on.

    Merchant names can't decide a pair: the wallet says "Valve" where the card
    says "Steam". They only break a tie, ahead of the nearer date. Each paying
    row is used once, so two same-priced purchases on one day pair one to one.
    """
    payers: dict[tuple[str, int], list[Txn]] = defaultdict(list)
    for t in txns:
        if not t.wallet and not t.transfer and t.kind in ("purchase", "refund"):
            payers[(t.kind, round(t.amount * 100))].append(t)
    used: set[int] = set()
    paired = 0
    for w in sorted((t for t in txns if t.wallet and t.kind in ("purchase", "refund")), key=lambda t: t.date):
        best = None
        for o in payers.get((w.kind, round(w.amount * 100)), ()):
            gap = (o.date - w.date).days
            if id(o) in used or not -PAIR_DAYS_BEFORE <= gap <= PAIR_DAYS_AFTER:
                continue
            same = bool(w.description) and w.description.lower() == o.description.lower()
            rank = (not same, abs(gap))
            if best is None or rank < best[0]:
                best = (rank, o)
        if best:
            o = best[1]
            used.add(id(o))
            w.paid_by = o.txn_id or o.account
            w.kind, w.transfer = "transfer", True
            paired += 1
    return paired


def is_spend(t: Txn) -> bool:
    """Counts toward spend: not a transfer or payment, not income."""
    return not t.transfer and t.kind != "income"


def load(root: Path, env: str = "production", *, spend_only: bool = True) -> list[Txn]:
    """Every posted row from Plaid, the Apple Card CSVs and ``penny import csv``
    (``data/imports/``), oldest first.
    ``spend_only`` drops transfers, card payments and income, as the Rocket
    Money loader's ``exclude_categories`` did."""
    txns = from_plaid(plaid.Store(root, env)) + from_apple(root / "data" / "apple") + csvimport.load(root / csvimport.IMPORTS)
    pair_wallets(txns)
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

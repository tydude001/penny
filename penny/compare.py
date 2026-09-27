"""Rocket Money export vs the Plaid + Apple feed, over the same window.

The board switches source only once every difference here is explained
(docs/budget-plan.md § Switching the board to Plaid data). Spend is compared
three ways, each narrowing the last: by family, by account, and by merchant
within one family.
"""

from __future__ import annotations

import re
from collections import defaultdict

from . import model
from .load import Txn

APPLE = "apple"  # Apple Card rows carry no account number; they group under this


def by_family(txns: list[Txn]) -> dict[str, float]:
    """Net per family, negatives kept, as ``by_account`` keeps them: a family
    one source nets below zero must still show against the other."""
    return model.net_by_family(txns)


def by_account(txns: list[Txn]) -> dict[str, float]:
    out: dict[str, float] = defaultdict(float)
    for t in txns:
        key = t.account_number or (APPLE if t.source == "apple_csv" else t.account or "?")
        out[key] += t.amount
    return dict(out)


def merchant_key(t: Txn) -> str:
    """The name's first word, letters only: the two sources spell a merchant
    differently ("CHIPOTLE MEX GR ONLINE" vs "Chipotle Mexican Grill")."""
    words = re.findall(r"[a-z]+", t.name.lower().replace("wal-mart", "walmart"))
    return words[0] if words else "?"


def by_merchant(txns: list[Txn], family: str) -> dict[str, float]:
    out: dict[str, float] = defaultdict(float)
    for t in txns:
        if t.family == family:
            out[merchant_key(t)] += t.amount
    return dict(out)


def diff(a: dict[str, float], b: dict[str, float]) -> list[tuple[str, float, float, float]]:
    """(key, a, b, b − a) for every key, biggest absolute difference first."""
    keys = set(a) | set(b)
    rows = [(k, a.get(k, 0.0), b.get(k, 0.0), b.get(k, 0.0) - a.get(k, 0.0)) for k in keys]
    return sorted(rows, key=lambda r: -abs(r[3]))

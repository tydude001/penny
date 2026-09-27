"""Assign each transaction to a family (amazon, costco_gas, restaurants, …).

Families come from ``[families.*]`` in rules.toml and are tried in file order —
first match wins, so put the specific ones (costco_gas, amazon_pharmacy) before
the general (costco, amazon). A family matches on merchant-text regexes first,
then on the fallbacks: the merchant category code where the family lists
``mccs`` and the row has one (card issuers pay by it), else the source's own
category (Plaid's detailed personal_finance_category, or Rocket Money's
Category). Anything unmatched is
``other``. Membership fee charges are pulled out first into ``membership_fees``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import fsio
from .load import Txn

OTHER = "other"
FEES = "membership_fees"


@dataclass
class Family:
    key: str
    label: str
    patterns: list[re.Pattern]
    categories: list[str]
    membership: str | None = None
    accounts: list[str] | None = None  # restrict to these account numbers
    amounts: list[float] | None = None  # restrict to these exact amounts (fees)
    online_patterns: list[re.Pattern] | None = None  # match only rows the feed marks as bought online
    mccs: list[str] | None = None  # merchant category codes; where a row has one, it decides over categories

    def matches(self, t: Txn) -> bool:
        if self.accounts is not None and t.account_number not in self.accounts:
            return False
        if self.amounts is not None and round(t.amount, 2) not in self.amounts:
            return False
        text = t.match_text
        if any(p.search(text) for p in self.patterns):
            return True
        if t.channel == "online" and any(p.search(text) for p in self.online_patterns or []):
            return True
        if self.mccs is not None and t.mcc:
            return t.mcc in self.mccs
        return t.category.lower() in self.categories


FeeRule = tuple[list[re.Pattern], list[float] | None]


def prorated_upgrades(m: dict) -> list[float]:
    """With ``upgrade_prorated = true``, the charges a mid-year tier upgrade
    can bill: the gap between two tiers' fees for 1-11 twelfths of the year
    (Costco's Gold Star -> Executive, $65 x 7/12 = $37.92 on 2026-09-23)."""
    if not m.get("upgrade_prorated"):
        return []
    fees = sorted({float(t["fee"]) for t in (m.get("tiers") or {}).values()})
    return sorted({round((hi - lo) * n / 12, 2) for i, lo in enumerate(fees) for hi in fees[i + 1:] for n in range(1, 12)})


def fee_amounts(m: dict) -> list[float]:
    """Every amount a fee row can carry: the listed ``fee_amounts`` plus any
    prorated upgrade. Empty when the membership lists none (pattern alone)."""
    listed = [float(a) for a in m.get("fee_amounts", [])]
    return sorted(set(listed + prorated_upgrades(m))) if listed else []


def fee_rules(memberships: dict) -> dict[str, FeeRule]:
    """Per membership: compiled ``fee_patterns`` and, when given, the exact
    ``fee_amounts`` a hit must also have (costco.com sells things too), with
    any prorated upgrade added. The one definition of "this row is a membership
    fee", shared by the categoriser and the report's observed-fee scan."""
    return {
        key: ([re.compile(p, re.IGNORECASE) for p in m.get("fee_patterns", [])], fee_amounts(m) or None)
        for key, m in memberships.items()
    }


def is_fee(t: Txn, rule: FeeRule) -> bool:
    """A fee charge, or its refund: amounts compare by size, so a refunded
    fee lands in membership_fees too and nets against the charge."""
    pats, amts = rule
    if amts is not None and round(abs(t.amount), 2) not in amts:
        return False
    return any(p.search(t.match_text) for p in pats)


class FeeFamily(Family):
    """One rule per membership, or-ed: a fee row is any membership's
    ``fee_patterns`` hit, further restricted by that membership's
    ``fee_amounts`` when given."""

    def __init__(self, memberships: dict):
        super().__init__(FEES, "Membership fees", [], [])
        self.rules = fee_rules(memberships)

    def matches(self, t: Txn) -> bool:
        return any(is_fee(t, r) for r in self.rules.values())


def build_families(rules: dict) -> list[Family]:
    """Families from rules.toml, preceded by a synthetic ``membership_fees``
    family built from every membership's ``fee_patterns`` — so the fee itself
    never counts as store spend or earns a card edge."""
    fams: list[Family] = [FeeFamily(rules.get("memberships", {}))]
    for key, spec in rules.get("families", {}).items():
        fams.append(
            Family(
                key=key,
                label=spec.get("label", key),
                patterns=[re.compile(p, re.IGNORECASE) for p in spec.get("patterns", [])],
                categories=[c.lower() for c in spec.get("categories", [])],
                membership=spec.get("membership"),
                accounts=[str(a) for a in spec["accounts"]] if "accounts" in spec else None,
                amounts=[float(a) for a in spec["amounts"]] if "amounts" in spec else None,
                online_patterns=[re.compile(p, re.IGNORECASE) for p in spec.get("online_patterns", [])],
                mccs=[str(m) for m in spec["mccs"]] if "mccs" in spec else None,
            )
        )
    return fams


def categorize(txns: list[Txn], families: list[Family]) -> list[Txn]:
    for t in txns:
        t.family = next((f.key for f in families if f.matches(t)), OTHER)
    return txns


UNCATEGORISED = "uncategorised"
NOT_SPEND = {"payment": "transfer", "transfer": "transfer", "income": "income"}


@dataclass
class CategoryRule:
    category: str
    patterns: list[re.Pattern]


def category_rules(rules: dict) -> list[CategoryRule]:
    return [CategoryRule(r["category"], [re.compile(p, re.IGNORECASE) for p in r.get("patterns", [])])
            for r in rules.get("categories", {}).get("rules", [])]


def plaid_primaries(rules: dict) -> list[str]:
    """``[categories.plaid]`` keys, longest first, so the most specific prefix wins."""
    return sorted(rules.get("categories", {}).get("plaid", {}), key=len, reverse=True)


def category_of(t: Txn, rules: dict, crules: list[CategoryRule], primaries: list[str] | None = None) -> str:
    """What the money was for, first answer wins: not spend at all (a transfer,
    payment or income); a reward; a ``[[categories.rules]]`` pattern; the
    family's default; Plaid's category, detailed then primary; else
    uncategorised. Families are what a card earns on; this is what a budget
    groups by, so one family can default to a category and a rule override it."""
    spec = rules.get("categories", {})
    if t.kind in NOT_SPEND:
        return NOT_SPEND[t.kind]
    if t.kind == "reward credit":
        return "earned"
    text = t.match_text
    for r in crules:
        if any(p.search(text) for p in r.patterns):
            return r.category
    by_family = spec.get("families", {})
    if t.family and t.family in by_family:
        return by_family[t.family]
    plaid = spec.get("plaid", {})
    if t.category in plaid:
        return plaid[t.category]
    if primaries is None:
        primaries = plaid_primaries(rules)
    primary = next((k for k in primaries if t.category.startswith(k + "_")), None)
    return plaid[primary] if primary else UNCATEGORISED


def assign_categories(txns: list[Txn], rules: dict) -> list[Txn]:
    """Set ``budget_category`` on each row. Run after ``categorize``: a family's
    default category needs the family."""
    crules = category_rules(rules)
    primaries = plaid_primaries(rules)
    for t in txns:
        t.budget_category = category_of(t, rules, crules, primaries)
    return txns


def load_overrides(path) -> dict[str, dict]:
    """``data/overrides.json``: transaction id -> {"family"?, "category"?}, set by
    hand for one row. Personal, like everything in data/."""
    import json
    from pathlib import Path

    p = Path(path)
    return json.loads(p.read_text()) if p.is_file() else {}


def check_override(txns: list[Txn], rules: dict, txn_id: str, family: str | None, category: str | None) -> None:
    """Raise ValueError unless ``txn_id`` is a spend row and ``family`` a known
    family. A new category is allowed: a budget can grow one."""
    if not any(t.txn_id == txn_id for t in txns):
        raise ValueError(f"no spend row with id {txn_id}")
    families = {OTHER, FEES, *rules.get("families", {})}
    if family and family not in families:
        raise ValueError(f"unknown family {family!r}; one of {', '.join(sorted(families))}")
    if category is not None and not re.fullmatch(r"[a-z][a-z &-]{0,39}", category):
        raise ValueError(f"category {category!r}: lower-case words, up to 40 letters")


def save_override(path, txn_id: str, family: str | None = None, category: str | None = None, clear: bool = False) -> dict:
    """Set one row's hand-set family and/or category in ``data/overrides.json``,
    or ``clear`` it back to the rules. Returns what the row now carries."""
    import json
    from pathlib import Path

    p = Path(path)
    ov = load_overrides(p)
    if clear:
        ov.pop(txn_id, None)
        entry = {}
    else:
        entry = ov.setdefault(txn_id, {})
        if family:
            entry["family"] = family
        if category:
            entry["category"] = category
    fsio.write_atomic(p, json.dumps(ov, indent=1, sort_keys=True) + "\n")
    return entry


def merchant_of(t: Txn) -> str:
    """The merchant a row is grouped under: Plaid's merchant name, else the
    bank's text. Merchant overrides are keyed on it, lower-cased."""
    return t.description or t.name


def save_merchant_override(path, merchant: str, family: str | None = None, category: str | None = None, clear: bool = False) -> dict:
    """``save_override`` for every row from ``merchant``, in ``data/merchants.json``."""
    return save_override(path, merchant.lower(), family, category, clear)


def apply_merchant_overrides(txns: list[Txn], overrides: dict[str, dict]) -> list[Txn]:
    """A merchant's hand-set family or category beats the rules; a row's own
    override (``apply_overrides``, run after) beats it."""
    if overrides:
        for t in txns:
            o = overrides.get(merchant_of(t).lower())
            if o and t.kind not in NOT_SPEND and t.kind != "reward credit":
                if "family" in o:
                    t.family = o["family"]
                if "category" in o:
                    t.budget_category = o["category"]
    return txns


def apply_overrides(txns: list[Txn], overrides: dict[str, dict]) -> list[Txn]:
    """A hand-set family or category beats every rule. Run last."""
    for t in txns:
        o = overrides.get(t.txn_id)
        if not o:
            continue
        if "family" in o:
            t.family = o["family"]
        if "category" in o:
            t.budget_category = o["category"]
    return txns


def unmatched(txns: list[Txn], top: int = 40) -> list[tuple[str, float, int]]:
    """Merchants that fell through to ``other``, biggest spend first. This is
    the tuning loop: run it, add patterns to rules.toml, repeat."""
    agg: dict[str, list[float]] = {}
    for t in txns:
        if t.family == OTHER and t.amount > 0:
            agg.setdefault(t.name, []).append(t.amount)
    rows = [(n, sum(a), len(a)) for n, a in agg.items()]
    rows.sort(key=lambda r: -r[1])
    return rows[:top]

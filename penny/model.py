"""The decision math.

For one membership::

    net = card_edge + tier_reward + perks - fee

* ``card_edge`` — extra cash back from the membership's card over the baseline
  card, counted only where the card wins (you'd use the baseline elsewhere):
  Σ_family max(0, rate_card − rate_baseline) × spend_family, over ALL families,
  because a Costco Visa earns its keep on gas and restaurants, not at Costco.
  That is the *card* decision. The *membership* decision only gets the part
  of the edge the membership causes: the card's rates with the membership
  minus what the same card pays without it (``without_membership_rates``;
  the Amazon Visa still pays 3 % at Amazon without Prime). A card with
  ``requires_membership = true`` (Costco Visa) attributes its whole edge; one
  with ``requires_membership = false`` and no without-rates attributes none.
* ``tier_reward`` — the membership's own rebate (Costco Executive 2 %), capped,
  and paid only on the tier's ``reward_families`` when it names a subset of the
  membership's families (Costco's 2 % skips gasoline).
* ``perks`` — hand-estimated value of shipping / delivery / video / price
  advantage from assumptions.toml, at low / base / high.
* ``fee`` — the list fee from rules.toml; the fee actually charged in the
  window is reported beside it, flagged when off by more than max($1, 5 %).

Break-even is the spend on the membership's own families at which net = 0,
holding the current family mix and all other spend fixed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from .categorize import FEES, OTHER, fee_rules, is_fee, prorated_upgrades
from .load import Txn

LEVELS = ("low", "base", "high")


@dataclass
class Window:
    start: date
    end: date
    asked: int = 0  # days the caller asked for; more than ``days`` when the data is shorter

    @property
    def partial(self) -> bool:
        return self.asked > self.days

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def annualize(self) -> float:
        return 365.0 / self.days


def pick_window(txns: list[Txn], *, year: int | None = None, days: int | None = None) -> Window:
    if not txns:
        raise ValueError("no transactions")
    """The window asked for, clamped to the dates the data covers: a year or
    365 days scaled by ×1.00 over nine months of rows would read 25% low."""
    first, last = min(t.date for t in txns), max(t.date for t in txns)
    if year is not None:
        start, end = date(year, 1, 1), date(year, 12, 31)
    else:
        start, end = last - timedelta(days=(days or 365) - 1), last
    asked = (end - start).days + 1
    return Window(max(start, first), min(end, last), asked)


def in_window(txns: list[Txn], w: Window) -> list[Txn]:
    return [t for t in txns if w.start <= t.date <= w.end]


def net_by_family(txns: list[Txn]) -> dict[str, float]:
    """Net amount per family (refunds subtract), negatives kept."""
    out: dict[str, float] = {}
    for t in txns:
        fam = t.family or OTHER
        out[fam] = out.get(fam, 0.0) + t.amount
    return out


def spend_by_family(txns: list[Txn]) -> dict[str, float]:
    """Net spend per family (refunds subtract), expenses only: a family netting
    to zero or below is dropped, since the card math has nothing to earn on."""
    return {k: v for k, v in net_by_family(txns).items() if v > 0}


def annualized(spend: dict[str, float], annualize: float) -> dict[str, float]:
    """A window's spend per family scaled to a year."""
    return {k: v * annualize for k, v in spend.items()}


def card_rate(card: dict, family: str) -> float:
    return float(card.get("rates", {}).get(family, card.get("default_rate", 0.0)))


def card_edge(card: dict, baseline: dict, spend: dict[str, float]) -> tuple[float, dict[str, float]]:
    """Total edge of the card over the baseline, and its per-family breakdown."""
    per: dict[str, float] = {}
    for fam, amt in spend.items():
        d = card_rate(card, fam) - card_rate(baseline, fam)
        if d > 0:
            per[fam] = d * amt
    return sum(per.values()), per


def without_membership(card: dict, baseline: dict) -> dict:
    """The card as it would earn with the membership cancelled."""
    if card.get("requires_membership", True):
        return baseline
    rates = dict(card.get("rates", {}))
    rates.update(card.get("without_membership_rates", {}))
    return {"rates": rates, "default_rate": card.get("default_rate", 0.0)}


def attributable_edge(card: dict, baseline: dict, spend: dict[str, float]) -> tuple[float, dict[str, float]]:
    """The part of the card's edge the membership causes: rate with membership
    minus the better of (same card without it, baseline), where positive."""
    without = without_membership(card, baseline)
    per: dict[str, float] = {}
    for fam, amt in spend.items():
        d = card_rate(card, fam) - max(card_rate(without, fam), card_rate(baseline, fam))
        if d > 0:
            per[fam] = d * amt
    return sum(per.values()), per


def perks(assumptions: dict, membership: str) -> dict[str, float]:
    """Sum of perk estimates at each level for one membership."""
    tot = {lvl: 0.0 for lvl in LEVELS}
    for perk in assumptions.get(membership, {}).values():
        if not isinstance(perk, dict):
            continue
        for lvl in LEVELS:
            tot[lvl] += float(perk.get(lvl, 0.0))
    return tot


@dataclass
class Verdict:
    membership: str
    tier: str
    with_card: bool
    fee: float
    family_spend: float  # annualised spend on the membership's own families
    edge: float  # card over baseline (the card decision)
    edge_detail: dict[str, float]
    attributable: float  # of which the membership causes (the membership decision)
    attributable_detail: dict[str, float]
    reward: float
    perks: dict[str, float]
    net: dict[str, float] = field(default_factory=dict)
    break_even: float | None = None  # family spend at which net(base) = 0

    def __post_init__(self):
        self.net = {lvl: self.attributable + self.reward + self.perks[lvl] - self.fee for lvl in LEVELS}


def evaluate(
    rules: dict,
    assumptions: dict,
    spend_window: dict[str, float],
    annualize: float,
) -> list[Verdict]:
    spend = annualized(spend_window, annualize)
    cards = rules.get("cards", {})
    baseline = cards[rules["held"]["baseline_card"]]
    out: list[Verdict] = []
    for mkey, m in rules.get("memberships", {}).items():
        fams = m.get("families", [])
        fam_spend = sum(spend.get(f, 0.0) for f in fams)
        pk = perks(assumptions, mkey)
        tiers = m.get("tiers") or {"standard": {"fee": m["fee"]}}
        card = cards.get(m.get("card", ""))
        for tkey, tier in tiers.items():
            fee = float(tier["fee"])
            rr = float(tier.get("reward_rate", 0.0))
            cap = float(tier.get("reward_cap", float("inf")))
            # The tier rebate need not pay on every family the membership enables:
            # Costco Executive's 2% excludes gasoline. `reward_families` names the
            # subset that earns it; absent, every family does.
            rfams = [str(f) for f in tier.get("reward_families", fams)]
            reward_spend = sum(spend.get(f, 0.0) for f in rfams)
            reward_share = reward_spend / fam_spend if fam_spend else 0.0
            reward = min(rr * reward_spend, cap)
            for with_card in ((False, True) if card else (False,)):
                edge, detail = card_edge(card, baseline, spend) if with_card else (0.0, {})
                attr, adetail = attributable_edge(card, baseline, spend) if with_card else (0.0, {})
                v = Verdict(mkey, tkey, with_card, fee, fam_spend, edge, detail, attr, adetail, reward, pk)
                # Break-even: scale the membership's own families by x, hold the rest.
                own_edge = sum(adetail.get(f, 0.0) for f in fams)
                other_edge = attr - own_edge
                # $ net per $ of family spend: scaling the own families scales the
                # reward only by the share of them that earns it.
                slope = (own_edge / fam_spend if fam_spend else 0.0) + rr * reward_share
                fixed = other_edge + pk["base"] - fee
                if slope > 0:
                    be = -fixed / slope
                    cap_at = cap / (rr * reward_share) if rr and reward_share else float("inf")
                    if be <= cap_at:
                        v.break_even = max(0.0, be)
                    else:
                        # Past the cap the reward is flat, but the card's edge on
                        # the own families keeps growing: solve on that slope.
                        own_slope = own_edge / fam_spend if fam_spend else 0.0
                        if own_slope > 0:
                            v.break_even = (-fixed - cap) / own_slope
                out.append(v)
    return out


def point_values(rules: dict, assumptions: dict) -> dict[str, float]:
    """The baseline card's value per point at low / base / high, in $/pt.

    Base is the card's own ``default_rate`` (its 1x rate *is* the ¢/pt), so the
    board's ¢/pt Record stays the one place base is set. Low and high come from
    assumptions.toml ``[point_value.<baseline card>]``; absent, both equal base.
    """
    bkey = rules["held"]["baseline_card"]
    base = float(rules["cards"][bkey].get("default_rate", 0.0))
    rng = assumptions.get("point_value", {}).get(bkey, {})
    return {"low": float(rng.get("low", base)), "base": base, "high": float(rng.get("high", base))}


def scaled_card(card: dict, cpp: float) -> dict:
    """The card re-valued at ``cpp`` $/pt: every rate keeps its multiple of the
    default rate (3x dining stays 3x), as the board's ¢/pt Record rescales."""
    d = float(card.get("default_rate", 0.0))
    f = cpp / d if d else 1.0
    out = dict(card)
    out["default_rate"] = d * f
    out["rates"] = {fam: float(r) * f for fam, r in card.get("rates", {}).items()}
    return out


def rules_at(rules: dict, cpp: float) -> dict:
    """The rules with the baseline card valued at ``cpp`` $/pt, and every card
    pooling its points into the baseline re-valued with it."""
    bkey = rules["held"]["baseline_card"]
    cards = dict(rules["cards"])
    cards[bkey] = scaled_card(cards[bkey], cpp)
    return resolve_points({**rules, "cards": cards})


# ---------------------------------------------------------------------------
# Pooled points and rotating quarters (the Freedom Flex)


def validate_rules(rules: dict, assumptions: dict | None = None) -> None:
    """One pass over every cross-reference in rules.toml (and, when given,
    assumptions.toml's ``[card_worth].alternatives``): each family, card and
    membership key named must exist. Raises ValueError listing every miss, so
    a typo fails at load instead of silently earning nothing. A table absent
    from ``rules`` isn't checked against (partial rules in tests)."""
    errs: list[str] = []
    fams = set(rules["families"]) | {OTHER, FEES} if "families" in rules else None
    cards = set(rules["cards"]) if "cards" in rules else None
    mems = set(rules["memberships"]) if "memberships" in rules else None

    def need(known: set[str] | None, what: str, where: str, keys) -> None:
        if known is None:
            return
        for k in [keys] if isinstance(keys, str) else keys:
            if str(k) not in known:
                errs.append(f"{where}: unknown {what} {k!r}")

    held = rules.get("held", {})
    if "baseline_card" in held:
        need(cards, "card", "[held] baseline_card", held["baseline_card"])
    need(cards, "card", "[held] cards", held.get("cards", []))
    need(mems, "membership", "[held] memberships", held.get("memberships", []))
    costco = rules.get("memberships", {}).get("costco", {})
    if "costco_tier" in held and "tiers" in costco:
        need(set(costco["tiers"]), "Costco tier", "[held] costco_tier", held["costco_tier"])
    for fk, f in rules.get("families", {}).items():
        if "membership" in f:
            need(mems, "membership", f"[families.{fk}] membership", f["membership"])
    for ck, c in rules.get("cards", {}).items():
        for tbl in ("rates", "without_membership_rates"):
            need(fams, "family", f"[cards.{ck}] {tbl}", c.get(tbl, {}))
        need(fams, "family", f"[cards.{ck}] points", [f for f in c.get("points", {}) if f != "default"])
        if "points_of" in c:
            need(cards, "card", f"[cards.{ck}] points_of", c["points_of"])
        for i, q in enumerate(c.get("quarters", [])):
            need(fams, "family", f"[cards.{ck}] quarter {i + 1} families", q.get("families", []))
    for mk, m in rules.get("memberships", {}).items():
        need(fams, "family", f"[memberships.{mk}] families", m.get("families", []))
        if m.get("card"):
            need(cards, "card", f"[memberships.{mk}] card", m["card"])
        for tk, t in (m.get("tiers") or {}).items():
            need(fams, "family", f"[memberships.{mk}.tiers.{tk}] reward_families", t.get("reward_families", []))
    for ak, a in rules.get("accounts", {}).items():
        if isinstance(a, dict) and "card" in a:
            need(cards, "card", f"[accounts] {ak!r} card", a["card"])
    need(cards, "card", "[card_credits]", list(rules.get("card_credits", {})))
    need(fams, "family", "[categories.families]", list(rules.get("categories", {}).get("families", {})))
    if assumptions is not None:
        need(cards, "card", "assumptions [card_worth] alternatives", assumptions.get("card_worth", {}).get("alternatives", []))
    if errs:
        raise ValueError("rules.toml: " + "; ".join(errs))


def resolve_points(rules: dict) -> dict:
    """Cards whose points pool into another card (``points_of``) are worth
    that card's ¢/pt: their ``points`` multiples (``default`` plus one per
    family) and each quarter's ``points`` become dollar rates at the pool
    card's current default_rate. Returns new rules; run it again whenever the
    pool card is re-valued, so the two never drift apart. Every loader runs
    the raw rules through here, so it checks them first (``validate_rules``)."""
    validate_rules(rules)
    cards = dict(rules.get("cards", {}))
    for ckey, card in cards.items():
        pool = card.get("points_of")
        if not pool:
            continue
        cpp = float(cards[pool].get("default_rate", 0.0))
        pts = card.get("points", {})
        c = dict(card)
        c["default_rate"] = float(pts.get("default", 1)) * cpp
        c["rates"] = {f: float(m) * cpp for f, m in pts.items() if f != "default"}
        c["quarters"] = [{**q, "rate": float(q["points"]) * cpp} for q in card.get("quarters", [])]
        cards[ckey] = c
    return {**rules, "cards": cards}


def _q(q: dict, key: str) -> date:
    return date.fromisoformat(str(q[key]))


def active_quarter(card: dict, day: date) -> dict | None:
    """The rotating quarter running on ``day``, if any."""
    return next((q for q in card.get("quarters", []) if _q(q, "start") <= day <= _q(q, "end")), None)


def card_on(card: dict, day: date) -> dict:
    """The card as it earns on ``day``: the running quarter's families at its
    rate, ignoring the cap."""
    q = active_quarter(card, day)
    if not q:
        return card
    rates = dict(card.get("rates", {}))
    for f in q["families"]:
        rates[f] = max(card_rate(card, f), float(q["rate"]))
    return {**card, "rates": rates}


def rotating_edge(card: dict, baseline: dict, spend: dict[str, float], day: date) -> tuple[float, dict[str, float]]:
    """What the announced quarters not yet over on ``day`` add, over the better
    of the card's own rate and the baseline's. Each takes a quarter of the
    year's ``spend`` on its families (even spending assumed) up to its cap, and
    a quarter already running counts only its days still to come, and only
    what is left of its cap after the days gone (spent evenly) used their part.
    Quarters are announced one at a time, so this is a floor on a year of the
    card."""
    per: dict[str, float] = {}
    for q in card.get("quarters", []):
        start, end = _q(q, "start"), _q(q, "end")
        if end < day:
            continue
        left = ((end - max(day, start)).days + 1) / ((end - start).days + 1)
        qs = {f: spend.get(f, 0.0) / 4 for f in q["families"] if spend.get(f, 0.0) > 0}
        tot = sum(qs.values())
        if not tot:
            continue
        cap = float(q.get("cap", float("inf")))
        k = min(tot * left, max(0.0, cap - tot * (1 - left))) / tot
        if k <= 0:
            continue  # the cap was used up by the days already gone
        for f, x in qs.items():
            d = float(q["rate"]) - max(card_rate(card, f), card_rate(baseline, f))
            if d > 0:
                per[f] = per.get(f, 0.0) + d * x * k
    return sum(per.values()), per


def card_edge_on(card: dict, baseline: dict, spend: dict[str, float], day: date | None) -> tuple[float, dict[str, float]]:
    """``card_edge`` plus the rotating quarters still to come on ``day``
    (none when ``day`` is None)."""
    e, per = card_edge(card, baseline, spend)
    if day is None:
        return e, per
    r, rper = rotating_edge(card, baseline, spend, day)
    for f, x in rper.items():
        per[f] = per.get(f, 0.0) + x
    return e + r, per


def evaluate_at_points(rules: dict, assumptions: dict, spend_window: dict[str, float], annualize: float) -> dict[str, list[Verdict]]:
    """``evaluate`` with the baseline card valued at each level of ``point_values``."""
    pv = point_values(rules, assumptions)
    return {lvl: evaluate(rules_at(rules, pv[lvl]), assumptions, spend_window, annualize) for lvl in LEVELS}


def card_edges_at_points(rules: dict, assumptions: dict, spend_window: dict[str, float], annualize: float, day: date | None = None) -> dict[str, dict[str, float]]:
    """Every other card's edge over the baseline, at each level of
    ``point_values``, with rotating quarters still to come on ``day``."""
    spend = annualized(spend_window, annualize)
    bkey = rules["held"]["baseline_card"]
    pv = point_values(rules, assumptions)
    at = {lvl: rules_at(rules, pv[lvl])["cards"] for lvl in LEVELS}
    return {
        ckey: {lvl: card_edge_on(at[lvl][ckey], at[lvl][bkey], spend, day)[0] for lvl in LEVELS}
        for ckey in rules.get("cards", {})
        if ckey != bkey
    }


def observed_fees(txns: list[Txn], rules: dict) -> dict[str, list[Txn]]:
    """Membership fee charges seen in the window, by membership. Same rule as
    the ``membership_fees`` family, so a costco.com order never reads as a fee."""
    found: dict[str, list[Txn]] = {}
    for mkey, rule in fee_rules(rules.get("memberships", {})).items():
        hits = [t for t in txns if t.amount > 0 and is_fee(t, rule)]
        if hits:
            found[mkey] = hits
    return found


def fee_mismatches(found: dict[str, list[Txn]], rules: dict) -> list[tuple[str, Txn, float]]:
    """Charges that differ from every list fee by more than max($1, 5 %).

    Returns (membership, charge, nearest list fee). A hit usually means the fee
    in rules.toml is stale — a price rise, or monthly billing the model reads as
    annual.
    """
    out: list[tuple[str, Txn, float]] = []
    for mkey, hits in found.items():
        m = rules.get("memberships", {}).get(mkey, {})
        listed = [float(t["fee"]) for t in (m.get("tiers") or {}).values()] or [float(m["fee"])]
        upgrades = prorated_upgrades(m)  # a mid-year upgrade bills an exact part of a year
        for t in hits:
            if round(t.amount, 2) in upgrades:
                continue
            nearest = min(listed, key=lambda f: abs(t.amount - f))
            if abs(t.amount - nearest) > max(1.0, 0.05 * nearest):
                out.append((mkey, t, nearest))
    return out

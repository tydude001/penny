"""Is it worth it: every membership and card verdict, and the inputs they
still rest on, worked out from rules.toml, assumptions.toml and the spend.

The board and ``penny report`` both render what this module returns; neither
decides anything of its own. ``model`` holds the arithmetic (net, edge, caps,
break-even) and ``cardworth`` the fee-card arithmetic; this is the layer that
picks which verdict a membership is judged by, what each open input could
move it by, and the plain-language verdict.
"""

from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from . import cardworth, model, pairs
from . import categorize as cat
from .fmt import num, pct, signed, usd
from .model import LEVELS
from .todo import DOC_REF, WikiRow, future_dates

WASH = 10.0  # a tier whose best alternative is within this of the held one is settled
# Optional plain-language keys on a perk in assumptions.toml, shown on the board:
# ask (the question), how (how to find the answer), low_if / high_if (what an
# answer at the low / high value means), measured_if with a number `measured`
# (a figure worked out elsewhere, offered as one click). The model ignores them.
PROMPTS = ("ask", "how", "low_if", "high_if", "measured_if")


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


# --------------------------------------------------------------------------
# Picking the verdict a membership is judged by


def held_tier(rules: dict, mkey: str) -> str:
    m = rules["memberships"][mkey]
    tiers = m.get("tiers")
    if not tiers:
        return "standard"
    t = rules.get("held", {}).get(f"{mkey}_tier")
    return t if t in tiers else next(iter(tiers))


def card_held(rules: dict, mkey: str) -> bool:
    c = rules["memberships"][mkey].get("card")
    return bool(c) and c in rules.get("cards", {}) and c in rules.get("held", {}).get("cards", [])


def headline_key(rules: dict, mkey: str, tier: str | None = None, with_card: bool | None = None) -> tuple[str, str, bool]:
    return (mkey, tier or held_tier(rules, mkey), card_held(rules, mkey) if with_card is None else with_card)


def by_key(verdicts: list[model.Verdict]) -> dict[tuple[str, str, bool], model.Verdict]:
    return {(v.membership, v.tier, v.with_card): v for v in verdicts}


# --------------------------------------------------------------------------
# Swing engine: evaluate on mutated copies, difference the base nets


def nets_under(rules: dict, assumptions: dict, spend: dict, annualize: float, mutate) -> dict[tuple[str, str, bool], float]:
    r, a = copy.deepcopy(rules), copy.deepcopy(assumptions)
    mutate(r, a)
    return {k: v.net["base"] for k, v in by_key(model.evaluate(r, a, spend, annualize)).items()}


def swing(rules: dict, assumptions: dict, spend: dict, annualize: float, frm, to, key=None) -> tuple[float, tuple | None]:
    """Base net under ``to`` minus under ``frm``, for ``key``, or for whichever
    verdict moves most when no key is given. Returns (swing, key)."""
    a = nets_under(rules, assumptions, spend, annualize, frm)
    b = nets_under(rules, assumptions, spend, annualize, to)
    if key is not None:
        return b[key] - a[key], key
    if not a:
        return 0.0, None
    k = max(a, key=lambda k: abs(b[k] - a[k]))
    return b[k] - a[k], k


def perk_swing(rules: dict, assumptions: dict, spend: dict, annualize: float, mkey: str, pkey: str, frm: str = "low", to: str = "high") -> float:
    def at(level):
        def mutate(r, a):
            p = a[mkey][pkey]
            p["base"] = p[level]

        return mutate

    return swing(rules, assumptions, spend, annualize, at(frm), at(to), headline_key(rules, mkey))[0]


def rate_multiples(card: dict) -> dict[str, float]:
    """Each rate as a multiple of the default rate (3x dining on the Sapphire).
    On a points card every rate is points × ¢/pt, so all of them rescale."""
    d = float(card.get("default_rate", 0.0))
    return {fam: round(float(r) / d, 4) if d else 1.0 for fam, r in card.get("rates", {}).items()}


def rate_swing(rules: dict, assumptions: dict, spend: dict, annualize: float, step: float = 0.005) -> tuple[float, tuple | None]:
    """Net change per ½¢ on the baseline's ¢/pt, for the verdict it moves most."""
    bkey = rules["held"]["baseline_card"]
    mults = rate_multiples(rules["cards"][bkey])

    def shift(d):
        def mutate(r, a):
            c = r["cards"][bkey]
            c["default_rate"] = float(c.get("default_rate", 0.0)) + d
            for fam, m in mults.items():
                c["rates"][fam] = float(c["rates"][fam]) + d * m

        return mutate

    s, k = swing(rules, assumptions, spend, annualize, shift(-step), shift(step))
    return abs(s) / 2, k


# --------------------------------------------------------------------------
# Decisions


@dataclass
class Decision:
    key: str
    title: str
    membership: str | None
    kind: str  # external | placeholder | choice | defaults | verify | baseline | apply | declined | tier | fee
    swing: float | None  # dollars a year; None when the model can't say
    explain: list[str]
    source: dict  # {"file", "section", "keys"}
    resolver: str | None  # perk | perks | verify | cpp | tier | fee
    settled: bool = False
    due: date | None = None
    sub: str = ""
    data: dict = field(default_factory=dict)


def _labels(rules: dict) -> dict[str, str]:
    out = {k: f.get("label", k) for k, f in rules.get("families", {}).items()}
    out[cat.OTHER] = "everything else"
    return out


def perk_label(rules: dict, mkey: str, pkey: str) -> str:
    fam = rules.get("families", {}).get(pkey, {})
    return fam["label"] if fam.get("membership") == mkey and "label" in fam else pkey.replace("_", " ")


def tier_label(m: dict, tkey: str) -> str:
    return m.get("tiers", {}).get(tkey, {}).get("label") or tkey.replace("_", " ").title()


def rate_text(card: dict, labels: dict[str, str]) -> str:
    by: dict[float, list[str]] = {}
    for fam, r in card.get("rates", {}).items():
        by.setdefault(float(r), []).append(labels.get(fam, fam))
    parts = [f"{pct(r)} {', '.join(names)}" for r, names in sorted(by.items(), reverse=True)]
    parts.append(f"{pct(float(card.get('default_rate', 0.0)))} everything else")
    return " · ".join(parts)


def detail_text(detail: dict[str, float], labels: dict[str, str], top: int = 4) -> str:
    items = sorted(detail.items(), key=lambda kv: -kv[1])[:top]
    return ", ".join(f"{labels.get(f, f)} {usd(x)}" for f, x in items) or "none"


def _perks(assumptions: dict, mkey: str) -> dict[str, dict]:
    return {k: p for k, p in assumptions.get(mkey, {}).items() if isinstance(p, dict) and all(lvl in p for lvl in LEVELS)}


def external_decision(row: WikiRow, today: date, root: Path | None = None) -> Decision:
    """A to-do file row. Docs it names are linked when they exist under ``root``."""
    dates = future_dates(row.detail, today)
    docs = [ref for ref in dict.fromkeys(DOC_REF.findall(row.detail)) if root is None or (root / ref).is_file()]
    title = re.sub(r"^penny\s*[—-]\s*", "", row.item)
    return Decision(
        key=f"wiki:{row.slug}",
        title=title[:1].upper() + title[1:],
        membership=None,
        kind="external",
        swing=None,
        explain=[row.detail],
        source={"file": "to-do file", "section": row.heading, "keys": [row.slug]},
        resolver=None,
        due=dates[0] if dates else None,
        sub=f"to-do {row.slug} · {row.heading}" if row.heading else f"to-do {row.slug}",
        data={"docs": docs, "dates": dates, "heading": row.heading, "detail": row.detail},
    )


def derive(
    rules: dict,
    assumptions: dict,
    spend: dict[str, float],
    annualize: float,
    found: dict | None = None,
    wiki: list[WikiRow] | None = None,
    today: date | None = None,
    root: Path | None = None,
    measures: list[pairs.Measure] | None = None,
) -> list[Decision]:
    """Every unresolved input, in no particular order (``rank`` orders them).
    ``spend`` is the window's spend by family, as ``model.evaluate`` takes it.
    ``measures`` are perks penny worked out from the stores' own captures
    (``pairs``): each is offered as the perk's measured answer."""
    today = today or datetime.now().astimezone().date()
    verdicts = by_key(model.evaluate(rules, assumptions, spend, annualize))
    labels = _labels(rules)
    mems = rules.get("memberships", {})
    cards = rules.get("cards", {})
    out: list[Decision] = [external_decision(r, today, root) for r in wiki or []]

    # Perks
    zero = []
    for mkey in assumptions:
        if mkey not in mems:
            continue
        mlabel = mems[mkey].get("label", mkey)
        net = verdicts[headline_key(rules, mkey)].net["base"]
        for pkey, p in _perks(assumptions, mkey).items():
            lo, base, hi = (float(p[lvl]) for lvl in LEVELS)
            note = str(p.get("note", ""))
            section = f"{mkey}.{pkey}"
            plabel = perk_label(rules, mkey, pkey)
            n_lo, n_hi = net - base + lo, net - base + hi
            data = {"section": section, "membership": mkey, "low": lo, "base": base, "high": hi, "note": note, "net": net, "fee": verdicts[headline_key(rules, mkey)].fee, "label": plabel, "mlabel": mlabel, "flips": n_lo < 0 <= n_hi, "recorded": note.startswith("Recorded ")}
            data.update({k: str(p[k]) for k in PROMPTS if k in p})
            if "measured_if" in p and _is_num(p.get("measured")):
                data["measured"] = float(p["measured"])
            mm = next((m for m in measures or [] if m.perk == section and m.priced), None)
            if mm:
                # Worked out from the captures on every load: it replaces any
                # hand-typed measured / measured_if, which goes stale.
                data.update(measured=float(round(mm.saving)), measured_if="What your receipts show", how=mm.summary(),
                            receipts={"items": len(mm.priced), "last": mm.last.isoformat(), "partial": mm.partial})
            src = {"file": "assumptions.toml", "section": section, "keys": list(LEVELS) + ["note"]}
            explain = [
                f"{mlabel} net, base: {signed(net)}. With {plabel} at its low {signed(n_lo)}, at its high {signed(n_hi)}.",
                f"Note in the file: {note}" if note else "No note in the file.",
            ]
            if "PLACEHOLDER" in note:
                out.append(Decision(f"perk:{section}", f"Replace the {plabel} placeholder", mkey, "placeholder", perk_swing(rules, assumptions, spend, annualize, mkey, pkey), explain, src, "perk", sub=f"{mlabel} · assumptions.toml [{section}] is a placeholder at {num(lo)} / {num(base)} / {num(hi)}", data=data))
            elif base == 0 and hi > 0 and not note.startswith("Recorded "):
                out.append(Decision(f"perk:{section}", f"{mlabel} {plabel}: what is it worth to you?", mkey, "choice", perk_swing(rules, assumptions, spend, annualize, mkey, pkey, "base", "high"), explain, src, "perk", sub=f"{mlabel} · [{section}] is {num(lo)} / {num(base)} / {num(hi)}; base 0 counts it as worth nothing to you", data=data))
            elif lo == base == hi == 0 and not data["recorded"]:
                zero.append(data)
    if zero:
        names = ", ".join(f"{d['mlabel']} {d['label']}" for d in zero)
        out.append(Decision("perks:zero", "Perks still at zero", None, "defaults", None, ["Zero is the honest value unless you would pay for the perk. Fill only what you'd buy."], {"file": "assumptions.toml", "section": ", ".join(d["section"] for d in zero), "keys": list(LEVELS)}, "perks", settled=True, sub=names, data={"perks": zero}))

    # Cards
    bkey = rules["held"]["baseline_card"]
    baseline = cards[bkey]
    blabel = baseline.get("label", bkey)
    held_cards = set(rules.get("held", {}).get("cards", []))
    annual = {k: v * annualize for k, v in spend.items()}
    card_members = {}
    for mkey, m in mems.items():
        if m.get("card") in cards:
            card_members.setdefault(m["card"], []).append(mkey)
    for ckey, card in cards.items():
        clabel = card.get("label", ckey)
        unverified = not card.get("verified", False)
        section = f"cards.{ckey}"
        if ckey == bkey:
            if unverified:
                s, k = rate_swing(rules, assumptions, spend, annualize)
                mults = rate_multiples(card)
                cpp = float(card.get("default_rate", 0.0)) * 100
                mover = verdicts.get(k) if k else None
                multi = ", ".join(f"{labels.get(f, f)} {m:g}x" for f, m in mults.items())
                explain = [
                    f"Valued at {cpp:g}¢ a point now: default_rate {card.get('default_rate')}" + (f"; {multi}." if multi else "."),
                    f"Each ½¢ moves a verdict's net by up to {usd(s)} a year" + (f" ({_verdict_name(rules, mover)})." if mover and s else "."),
                    "Every card edge is measured against this card, so card-worth numbers move more than the membership nets.",
                    f"Rates: {rate_text(card, labels)}.",
                ]
                out.append(Decision(f"cpp:{ckey}", f"Pick the cents per point for {clabel}", None, "baseline", s, explain, {"file": "rules.toml", "section": section, "keys": ["default_rate"] + [f"rates.{f}" for f in mults]}, "cpp", sub=f"the baseline card · rules.toml says {cpp:g}¢ and verified = false", data={"section": section, "cpp": cpp, "mults": mults, "label": clabel}))
            continue
        members = card_members.get(ckey, [])
        rates = f"Rates: {rate_text(card, labels)}."
        if members and ckey not in held_cards and card.get("declined_on"):
            edge = max(verdicts[headline_key(rules, mk, with_card=True)].edge for mk in members)
            out.append(Decision(f"declined:{ckey}", f"Skipped the {clabel}", members[0], "declined", edge, [f"Declined on {card['declined_on']}."], {"file": "rules.toml", "section": section, "keys": ["declined_on"]}, None, settled=True, sub=f"declined {card['declined_on']}", data={"section": section, "on": str(card["declined_on"])}))
        elif members and ckey not in held_cards:
            for mkey in members:
                m = mems[mkey]
                mlabel = m.get("label", mkey)
                w = verdicts[headline_key(rules, mkey, with_card=True)]
                wo = verdicts[headline_key(rules, mkey, with_card=False)]
                explain = [
                    f"{mlabel} net, base: {signed(w.net['base'])} with the card, {signed(wo.net['base'])} without.",
                    f"Edge over {blabel}: {usd(w.edge)} a year — {detail_text(w.edge_detail, labels)}.",
                    rates,
                ]
                if card.get("requires_membership", True):
                    explain.append(f"The card closes if {mlabel} lapses, so its whole edge counts toward {mlabel}.")
                if unverified:
                    explain.append("The rates are recollections: verify them before applying.")
                out.append(Decision(f"apply:{ckey}:{mkey}", f"{clabel}: worth applying?", mkey, "apply", w.edge, explain, {"file": "rules.toml", "section": section, "keys": ["verified"] if unverified else []}, "verify" if unverified else None, sub=f"{mlabel} · edge {usd(w.edge)} · net {signed(w.net['base'])} with it vs {signed(wo.net['base'])} without" + (" · rates unverified" if unverified else ""), data={"section": section, "unverified": unverified}))
        elif unverified:
            if members:
                edge = max(verdicts[headline_key(rules, mk, with_card=True)].edge for mk in members)
                mkey = members[0]
                detail = verdicts[headline_key(rules, mkey, with_card=True)].edge_detail
            else:
                edge, detail = model.card_edge(card, baseline, annual)
                mkey = None
            explain = [rates, f"Edge over {blabel}: {usd(edge)} a year — {detail_text(detail, labels)}. That is what rides on these rates."]
            for mk in members:
                attr = verdicts[headline_key(rules, mk, with_card=True)].attributable
                explain.append(f"Of that, {usd(attr)} needs {mems[mk].get('label', mk)} and counts in its verdict; the rest is the card's own worth.")
            if card.get("without_membership_rates"):
                without = {**card.get("rates", {}), **card["without_membership_rates"]}
                explain.append(f"Without the membership: {rate_text({'rates': without, 'default_rate': card.get('default_rate', 0.0)}, labels)}.")
            sub_m = f"{mems[mkey].get('label', mkey)} · " if mkey else ""
            out.append(Decision(f"verify:{ckey}", f"Confirm the {clabel} rates", mkey, "verify", edge, explain, {"file": "rules.toml", "section": section, "keys": ["verified"]}, "verify", sub=f"{sub_m}rules.toml [{section}] verified = false", data={"section": section}))

    # Tiers
    for mkey, m in mems.items():
        tiers = m.get("tiers") or {}
        if len(tiers) < 2:
            continue
        mlabel = m.get("label", mkey)
        held = held_tier(rules, mkey)
        nets = {t: verdicts[headline_key(rules, mkey, tier=t)].net["base"] for t in tiers}
        alts = {t: nets[t] - nets[held] for t in tiers if t != held}
        best = max(alts, key=alts.get)
        diff = alts[best]
        h, b = tiers[held], tiers[best]
        drr = float(b.get("reward_rate", 0.0)) - float(h.get("reward_rate", 0.0))
        dfee = float(b["fee"]) - float(h["fee"])
        be = dfee / drr if drr else None
        # The reward is paid on reward_families only (Costco: warehouse, not gas),
        # so that is the spend to hold against the break-even.
        own = sum(annual.get(f, 0.0) for f in b.get("reward_families") or m.get("families", []))
        hl, bl = tier_label(m, held), tier_label(m, best)
        explain = [
            f"{bl}: {usd(dfee)} more fee" + (f" against {pct(drr)} of {usd(own)} spend that earns it ({usd(drr * own)})" if drr else "") + f". Net, base: {bl} {signed(nets[best])}, {hl} {signed(nets[held])}.",
        ]
        if be is not None and be > 0:
            explain.append(f"{bl} pays for itself above {usd(be)} a year of {mlabel} spend.")
        settled = diff <= WASH
        # A refundable upgrade (Costco refunds the fee less the reward on a
        # downgrade) cannot lose money, so a wash is a free option, not a no.
        refund = bool(b.get("refundable")) and dfee > 0 and abs(diff) <= WASH
        if refund:
            sub = (f"On the numbers it nets {signed(diff)} against {hl} at {usd(own)} of spend that earns the reward, "
                   f"but the {usd(dfee)} upgrade is refunded, less the reward, on a downgrade: the worst case is $0.")
            explain.append(f"Refundable: if the reward comes in under {usd(dfee)}, downgrading returns the difference, so the year nets $0 rather than {signed(diff)}.")
        elif settled and abs(diff) <= WASH:
            sub = f"A wash at {usd(own)} own spend: {bl} nets {signed(diff)} against {hl}." + (f" Revisit if it passes ~{usd(be)}." if be and be > 0 else "")
        elif settled:
            sub = f"{hl} is ahead by {usd(-diff)}." + (f" {bl} pays off above {usd(be)} own spend." if be and be > 0 else "")
        else:
            sub = f"Switching to {bl} moves net by {signed(diff)} a year"
        tier_nets = {t: nets[t] - nets[held] for t in tiers}
        out.append(Decision(f"tier:{mkey}", f"{mlabel} {bl}: " + ("take it at renewal" if refund else "settled by the numbers" if settled else f"switch from {hl}?"), mkey, "tier", diff, explain, {"file": "rules.toml", "section": "held", "keys": [f"{mkey}_tier"]}, "tier", settled=settled, sub=sub, data={"key": f"{mkey}_tier", "held": held, "best": best, "refund": refund, "nets": tier_nets, "labels": {t: tier_label(m, t) for t in tiers}}))

    # Fee charges off the list fee
    hits: dict[str, list] = {}
    for mkey, t, nearest in model.fee_mismatches(found or {}, rules):
        hits.setdefault(mkey, []).append((t, nearest))
    for mkey, hs in hits.items():
        m = mems[mkey]
        mlabel = m.get("label", mkey)
        t, nearest = max(hs, key=lambda h: h[0].date)
        tiers = m.get("tiers") or {}
        tkey = next((k for k, tr in tiers.items() if float(tr["fee"]) == nearest), None)
        section = f"memberships.{mkey}.tiers.{tkey}" if tkey else f"memberships.{mkey}"
        explain = [f"Charged {usd(x.amount)} on {x.date}; nearest list fee {usd(n)}." for x, n in hs]
        if t.amount < nearest / 5:
            explain.append("A charge this small may be monthly billing read as annual; if so, leave the fee and check fee_patterns instead.")
        out.append(Decision(f"fee:{mkey}", f"{mlabel} fee changed?", mkey, "fee", t.amount - nearest, explain, {"file": "rules.toml", "section": section, "keys": ["fee"]}, "fee", sub=f"{mlabel} · charged {usd(t.amount)}, list fee {usd(nearest)}", data={"section": section, "value": t.amount}))
    return out


def _verdict_name(rules: dict, v: model.Verdict) -> str:
    m = rules["memberships"][v.membership]
    name = m.get("label", v.membership)
    if v.tier != "standard":
        name += f" {tier_label(m, v.tier)}"
    if v.with_card:
        name += f" + {rules['cards'][m['card']].get('label', m['card'])}"
    return name


def rank(decisions: list[Decision]) -> list[Decision]:
    """Dated wiki items first (earliest due), then by dollars moved, settled last."""

    def k(d: Decision):
        group = 2 if d.settled else 0 if d.kind == "external" and d.due else 1
        return (group, d.due or date.max if group == 0 else date.max, -abs(d.swing) if d.swing is not None else math.inf)

    return sorted(decisions, key=k)


# --------------------------------------------------------------------------
# Verdict rows


@dataclass
class VerdictRow:
    title: str
    sub: str
    low: float
    base: float
    high: float
    foot: str
    verdict: str
    tone: str  # good | warn | crit | neutral
    why: str
    membership: str = ""


def _and(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def net_terms(rules: dict, v: model.Verdict) -> list[str]:
    """The non-zero things a verdict's base net adds up before the fee."""
    m = rules["memberships"][v.membership]
    mlabel = m.get("label", v.membership)
    terms = []
    if round(v.attributable):
        clabel = rules["cards"][m["card"]].get("label", m["card"])
        terms.append(f"{usd(v.attributable)} of {clabel} cash back that needs {mlabel}")
    if round(v.reward):
        terms.append(f"{usd(v.reward)} tier reward")
    if round(v.perks["base"]):
        terms.append(f"{usd(v.perks['base'])} of perks at base")
    return terms


def verdict_line(rules: dict, assumptions: dict, v: model.Verdict) -> tuple[str, str, str]:
    """keep / drop / can't tell, and what it hangs on."""
    mkey = v.membership
    lo, base, hi = (v.net[lvl] for lvl in LEVELS)
    perks = _perks(assumptions, mkey)
    terms = net_terms(rules, v)
    against = f"{_and(terms)} against the {usd(v.fee)} fee: {signed(base)}" if terms else f"nothing offsets the {usd(v.fee)} fee: {signed(base)}"
    valued = any(float(p[lvl]) for p in perks.values() for lvl in LEVELS) or any(str(p.get("note", "")).startswith("Recorded ") for p in perks.values())
    if not valued and base < 0:
        return "keep or drop: can't tell", "neutral", f"No perk has a value yet; {against}."
    if round(lo) == round(hi):  # nothing uncertain left to range over
        why = f"{against[0].upper()}{against[1:]}. No input left to guess at."
        return ("keep", "good", why) if lo >= 0 else ("drop", "crit", why)
    if lo >= 0:
        return "keep", "good", f"Positive at low, base and high: {signed(lo)} to {signed(hi)}."
    if hi < 0:
        return "drop", "crit", f"Negative even at high: {signed(hi)}."
    flips = []
    for pkey, p in perks.items():
        pb, pl, ph = float(p["base"]), float(p["low"]), float(p["high"])
        if base - pb + pl < 0 <= base - pb + ph:
            flips.append((ph - pl, pkey, pb, pl, ph))
    if not flips:
        return ("keep" if base >= 0 else "drop") + ", but several estimates decide it together", "warn", f"Net {signed(lo)} to {signed(hi)}; no single perk flips it."
    _, pkey, pb, pl, ph = max(flips)
    label = perk_label(rules, mkey, pkey)
    need = pb - base
    if base >= 0:
        return f"keep, if {label} is worth {usd(need)}+ a year", "warn", f"Net {signed(base)} counts {label} at {usd(pb)}. At its low ({usd(pl)}) the net is {signed(base - pb + pl)}."
    return f"drop, unless {label} is worth {usd(need)}+ a year", "warn", f"{against[0].upper()}{against[1:]}. With {label} at its high ({usd(ph)}) the net is {signed(base - pb + ph)}."


def verdict_rows(rules: dict, assumptions: dict, spend: dict, annualize: float) -> list[VerdictRow]:
    verdicts = by_key(model.evaluate(rules, assumptions, spend, annualize))
    out = []
    for mkey, m in rules.get("memberships", {}).items():
        v = verdicts[headline_key(rules, mkey)]
        sub = [f"fee {usd(v.fee)}", f"own spend {usd(v.family_spend)}"]
        foot = f"break-even {usd(v.break_even)}" if v.break_even is not None else "no break-even"
        card = rules.get("cards", {}).get(m.get("card", ""))
        if card and not v.with_card:
            sub.append("store card not held")
            alt = verdicts[headline_key(rules, mkey, with_card=True)]
            foot = f"{signed(alt.net['base'] - v.net['base'])} with the {card.get('label', m['card'])}"
        verdict, tone, why = verdict_line(rules, assumptions, v)
        out.append(VerdictRow(_verdict_name(rules, v), " · ".join(sub), v.net["low"], v.net["base"], v.net["high"], foot, verdict, tone, why, mkey))
    return out


def net_lines(rules: dict, assumptions: dict, v: model.Verdict) -> list[tuple[str, float, str]]:
    """The sum a verdict's base net adds up, one (label, dollars, mark) a
    term: store-card cash back that needs the membership, the tier reward,
    each perk at base (the zero ones on one line), then the fee."""
    m = rules["memberships"][v.membership]
    mlabel = m.get("label", v.membership)
    out = []
    if round(v.attributable):
        out.append((f"{rules['cards'][m['card']].get('label', m['card'])} cash back that needs {mlabel}", v.attributable, ""))
    if round(v.reward):
        out.append((f"{tier_label(m, v.tier)} reward", v.reward, ""))
    zero = []
    for pkey, p in assumptions.get(v.membership, {}).items():
        if not isinstance(p, dict):
            continue
        label = perk_label(rules, v.membership, pkey)
        base = float(p.get("base", 0.0))
        if not round(base):
            zero.append(label)
            continue
        note = str(p.get("note", ""))
        rec = re.match(r"Recorded (\d{4}-\d{2}-\d{2})", note)
        mark = "placeholder" if "PLACEHOLDER" in note else f"recorded {rec.group(1)}" if rec else ""
        out.append((label[:1].upper() + label[1:], base, mark))
    if zero:
        s = _and(zero)
        out.append((s[:1].upper() + s[1:], 0.0, ""))
    out.append(("Fee", -v.fee, ""))
    return out


# --------------------------------------------------------------------------
# Cards: one row per card, where to swipe, and card worth. Worked out from
# rules.toml, the model and cardworth on every GET; nothing new is stored.


accepts = model.accepts


def usable_spend(rules: dict, card: dict, spend: dict[str, float]) -> dict[str, float]:
    """``spend`` without the families whose stores won't take ``card``."""
    return {f: x for f, x in spend.items() if accepts(rules, f, card)}


def worth_only(rules: dict, assumptions: dict, ckey: str) -> bool:
    """A card that exists only as a card-worth alternative (the flat 2%)."""
    held = set(rules.get("held", {}).get("cards", [])) | {rules["held"]["baseline_card"]}
    named = {m.get("card") for m in rules.get("memberships", {}).values()}
    return ckey in assumptions.get("card_worth", {}).get("alternatives", []) and ckey not in held and ckey not in named


@dataclass
class CardRow:
    key: str
    label: str
    role: str  # baseline | store card | category card | dormant | considered
    held: bool
    edge: float  # over the baseline, a year; 0 for the baseline itself
    detail: dict[str, float]  # the edge by family
    bonus: float  # the part earned on families the card lists a rate for
    default_part: float  # the part its default_rate earns over the baseline's rate
    members: list[str]  # memberships that name it as their card
    attributable: dict[str, float]  # membership → the part of the edge it causes
    rates: str
    without: str
    verified: bool
    verified_on: str
    fee: float | None  # None: no annual_fee key, shown as "no fee"
    badge: str
    tone: str


def worth_margin(worth: list[cardworth.Worth]) -> dict[str, float] | None:
    """The first card-worth alternative over the second, at low / base / high."""
    if len(worth) < 2:
        return None
    return {lvl: worth[0].worth[lvl] - worth[1].worth[lvl] for lvl in LEVELS}


def margin_at(rules: dict, assumptions: dict, spend: dict[str, float], cpp: float) -> float:
    """The base margin with the first alternative valued at ``cpp`` $/pt."""
    ckey = assumptions["card_worth"]["alternatives"][0]
    r = {**rules, "cards": {**rules["cards"], ckey: model.scaled_card(rules["cards"][ckey], cpp)}}
    ws = cardworth.card_worth(r, assumptions, spend)
    return ws[0].worth["base"] - ws[1].worth["base"]


def flip_point(rules: dict, assumptions: dict, spend: dict[str, float], lo: float | None = None) -> float | None:
    """The $/pt inside the ``[point_value]`` range (from ``lo`` instead, when
    given) where the first card-worth alternative stops beating the second,
    benefits at base, or None when it doesn't cross there. ``spend`` is
    annual, on the first alternative's accounts."""
    alts = assumptions.get("card_worth", {}).get("alternatives", [])
    if len(alts) < 2:
        return None
    pv = cardworth.card_point_values(rules, assumptions, alts[0])
    lo, hi = pv["low"] if lo is None else lo, pv["high"]
    f_lo, f_hi = margin_at(rules, assumptions, spend, lo), margin_at(rules, assumptions, spend, hi)
    if lo == hi or (f_lo < 0) == (f_hi < 0):
        return None
    for _ in range(60):
        mid = (lo + hi) / 2
        if (margin_at(rules, assumptions, spend, mid) < 0) == (f_lo < 0):
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def card_rows(rules: dict, assumptions: dict, spend: dict[str, float], annualize: float, worth: list[cardworth.Worth] | None = None, today: date | None = None) -> list[CardRow]:
    """Every card but a card-worth-only one: the baseline, then held cards by
    edge, then considered ones. ``spend`` is the window's, as ``derive`` takes
    it. With ``today``, a card's rotating quarters still to come count too."""
    verdicts = by_key(model.evaluate(rules, assumptions, spend, annualize))
    annual = {k: v * annualize for k, v in spend.items()}
    labels = _labels(rules)
    cards = rules.get("cards", {})
    bkey = rules["held"]["baseline_card"]
    baseline = cards[bkey]
    held = set(rules.get("held", {}).get("cards", [])) | {bkey}
    margin = worth_margin(worth or [])
    mems = rules.get("memberships", {})
    out = []
    for ckey, card in cards.items():
        if worth_only(rules, assumptions, ckey):
            continue
        members = [m for m, mm in mems.items() if mm.get("card") == ckey]
        attr: dict[str, float] = {}
        if ckey == bkey:
            edge, detail = 0.0, {}
        elif members:
            vs = {m: verdicts[headline_key(rules, m, with_card=True)] for m in members}
            top = max(vs.values(), key=lambda v: v.edge)
            edge, detail = top.edge, top.edge_detail
            attr = {m: v.attributable for m, v in vs.items()}
        else:
            edge, detail = model.card_edge_on(card, baseline, usable_spend(rules, card, annual), today)
        rated = set(card.get("rates", {})) | {f for q in card.get("quarters", []) for f in q["families"]}
        bonus = sum(x for f, x in detail.items() if f in rated)
        is_held = ckey in held
        if ckey == bkey:
            role = "baseline"
        elif not is_held:
            role = "considered"
        elif members:
            role = "store card"
        elif round(edge) == 0:
            role = "dormant"
        else:
            role = "category card"
        if role == "baseline":
            badge, tone = (("worth it", "good") if margin["base"] > 0 else ("not worth the fee", "crit")) if margin and worth[0].card == bkey else ("baseline", "neutral")
        elif role == "considered":
            badge, tone = ("worth applying?", "warn") if edge >= WASH else ("skip", "neutral")
        elif role == "dormant":
            badge, tone = "no reason to swipe", "neutral"
        else:
            badge, tone = ("keep", "good") if edge >= WASH else ("little edge", "neutral")
        without = ""
        if card.get("without_membership_rates"):
            without = rate_text({"rates": {**card.get("rates", {}), **card["without_membership_rates"]}, "default_rate": card.get("default_rate", 0.0)}, labels)
        out.append(CardRow(
            ckey, card.get("label", ckey), role, is_held, edge, detail, bonus, edge - bonus, members, attr,
            rate_text(card, labels) + (f" by {card['default_via']}" if card.get("default_via") else ""), without, bool(card.get("verified", False)), str(card.get("verified_on", "")),
            float(card["annual_fee"]) if "annual_fee" in card else None, badge, tone,
        ))
    order = {"baseline": 0, "considered": 2}
    return sorted(out, key=lambda r: (order.get(r.role, 1), -r.edge))


@dataclass
class SwipeRow:
    family: str  # a family key, or cat.OTHER for everything else
    label: str
    spend: float  # a year
    card: str  # "" when no held card is taken there
    via: str  # the card's label, or its default_via when the default rate wins
    rate: float
    gain: float  # a year over swiping the baseline here
    until: str = ""  # set when a rotating quarter's rate wins: its cap and end


def swipe_rows(rules: dict, spend: dict[str, float], annualize: float, today: date | None = None) -> list[SwipeRow]:
    """For each family with spend, the held card paying the most there at the
    baseline's ¢/pt (ties go to the baseline) among those its store takes,
    biggest spend first, and last the everything-else row, where each card's
    default_rate competes. With ``today``, a rotating quarter running then
    counts at its rate (cap aside), and ``gain`` holds what it adds."""
    raw = rules.get("cards", {})
    cards = {k: model.card_on(c, today) for k, c in raw.items()} if today else raw
    bkey = rules["held"]["baseline_card"]
    held = [bkey] + [k for k in rules.get("held", {}).get("cards", []) if k in cards and k != bkey]
    labels = _labels(rules)
    out, other = [], None
    for fam, amt in sorted(spend.items(), key=lambda kv: -kv[1]):
        if amt <= 0 or fam == cat.FEES:  # a membership fee is billed wherever it is billed
            continue
        ok = [k for k in held if accepts(rules, fam, cards[k])]
        if not ok:
            out.append(SwipeRow(fam, labels.get(fam, fam), amt * annualize, "", "no card you hold", 0.0, 0.0))
            continue
        best = max(ok, key=lambda k: (model.card_rate(cards[k], fam), k == bkey))
        c = cards[best]
        rate = model.card_rate(c, fam)
        via = c["default_via"] if fam not in c.get("rates", {}) and c.get("default_via") else c.get("label", best)
        gain = (rate - model.card_rate(cards[bkey], fam)) * amt * annualize
        until = ""
        q = model.active_quarter(raw[best], today) if today else None
        if q and fam in q["families"] and rate > model.card_rate(raw[best], fam):
            gain = model.card_edge_on(raw[best], raw[bkey], {fam: amt * annualize}, today)[0]
            until = f"to ${float(q['cap']):,.0f} a quarter, through {model._q(q, 'end'):%b %-d}"
        row = SwipeRow(fam, labels.get(fam, fam), amt * annualize, best, via, rate, gain, until)
        if fam == cat.OTHER:
            other = row
        else:
            out.append(row)
    return out + ([other] if other else [])

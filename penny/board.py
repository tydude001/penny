"""``penny board``: the verdicts, the inputs they still rest on, and a form
per input that writes the choice back where it belongs.

The decision list is computed, not maintained. Every GET reads the feed
(or the export, with --export), rules.toml, assumptions.toml and, with
--todo-file, the ``penny-`` rows of a Markdown to-do table
(cached on their files' stats, so a change to any of them is read at once), and
derives decisions from what is still unresolved in them: ``verified = false``
cards, placeholder or zeroed perks, tiers, cards named but not held, fee
charges off the list fee. Recording a choice edits the file the decision is
about (``tomledit``, comment-preserving), so the trigger disappears and the
decision closes itself. The board keeps no state; each write is appended to
``data/board-decisions.jsonl`` as an audit line. It never commits.

Stdlib only.
"""

from __future__ import annotations

import calendar
import copy
import ipaddress
import json
import math
import os
import re
import stat
import subprocess
import sys
import threading
import tomllib
import traceback
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from functools import partial
from html import escape, unescape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from . import (
    apple,
    budget,
    cardworth,
    cashflow,
    check,
    costco,
    feed,
    home,
    model,
    networth,
    recurring,
    statements,
    tomledit,
    walmart,
)
from . import categorize as cat
from . import load as ld
from .model import LEVELS
from .report import cents

WASH = 10.0  # a tier whose best alternative is within this of the held one is settled
# Optional plain-language keys on a perk in assumptions.toml, shown on the board:
# ask (the question), how (how to find the answer), low_if / high_if (what an
# answer at the low / high value means), measured_if with a number `measured`
# (a figure worked out elsewhere, offered as one click). The model ignores them.
PROMPTS = ("ask", "how", "low_if", "high_if", "measured_if")
MAX_BODY = 64_000
MAX_UPLOAD = 20_000_000  # a year of Wallet CSV is ~100 KB; a statement PDF well under 1 MB

ISO_DATE = re.compile(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)")
WIKI_ID = re.compile(r"<!--\s*id:(penny-[\w-]+)\s*-->")
DOC_REF = re.compile(r"docs/[\w-]+(?:/[\w-]+)*\.md")
DOC_PATH = re.compile(r"/docs/([\w-]+(?:\.[\w-]+)*\.md)")


# --------------------------------------------------------------------------
# Formatting


def usd(x: float) -> str:
    return f"{'−' if round(x) < 0 else ''}${abs(x):,.0f}"


def signed(x: float) -> str:
    r = round(x)
    return "$0" if r == 0 else f"{'+' if r > 0 else '−'}${abs(x):,.0f}"


def num(x: float) -> str:
    return f"{x:,.0f}" if float(x).is_integer() else f"{x:g}"


def pct(r: float) -> str:
    return f"{r * 100:g}%"


def _toml(p: str | Path) -> dict:
    with open(p, "rb") as f:
        return tomllib.load(f)


# --------------------------------------------------------------------------
# To-do rows (--todo-file): any Markdown table whose rows carry
# ``<!-- id:penny-... -->``. Called "wiki" in keys and groups, after the
# first such file.


@dataclass
class WikiRow:
    slug: str
    item: str
    detail: str
    heading: str = ""


def parse_wiki(text: str) -> list[WikiRow]:
    """Open items rows whose ``<!-- id:slug -->`` starts with ``penny-``."""
    out: list[WikiRow] = []
    heading = ""
    for line in text.splitlines():
        if line.startswith("#"):
            heading = line.lstrip("#").strip()
            continue
        m = WIKI_ID.search(line)
        if not m or not line.lstrip().startswith("|"):
            continue
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", line.strip().strip("|"))]
        if len(cells) < 2:
            continue
        item = WIKI_ID.sub("", cells[0]).replace("**", "").strip()
        out.append(WikiRow(m.group(1), item, cells[1], heading))
    return out


def future_dates(text: str, today: date) -> list[date]:
    out = []
    for s in ISO_DATE.findall(text):
        try:
            d = date.fromisoformat(s)
        except ValueError:
            continue
        if d > today:
            out.append(d)
    return sorted(out)


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
    kind: str  # external | placeholder | choice | defaults | verify | baseline | apply | tier | fee
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
) -> list[Decision]:
    """Every unresolved input, in no particular order (``rank`` orders them).
    ``spend`` is the window's spend by family, as ``model.evaluate`` takes it."""
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
        if members and ckey not in held_cards:
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


def accepts(rules: dict, family: str, card: dict) -> bool:
    """Whether the store behind ``family`` takes ``card``: a family's optional
    ``networks`` list (Costco: Visa only) against the card's ``network``.
    Either key absent means yes."""
    nets = rules.get("families", {}).get(family, {}).get("networks")
    return not nets or "network" not in card or card["network"] in nets


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


# --------------------------------------------------------------------------
# State read on every GET


@dataclass
class Config:
    root: Path
    rules: Path
    assumptions: Path
    wiki: Path | None = None
    export: Path | None = None


WALMART_FAMILY = "walmart_delivery"
WALMART_ONLINE = "walmart_online"  # Purchase History lists every walmart.com order: the capture has these
WALMART_STORE = "walmart_store"  # in-store trips it may not list
COSTCO_FAMILY = "costco"  # the OnePay feed the export undercounts; rules.toml says why


def git_state(cfg: Config) -> dict[str, str] | None:
    paths = [cfg.rules, cfg.assumptions]
    try:
        r = subprocess.run(["git", "status", "--porcelain", "--", *map(str, paths)], cwd=cfg.root, capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    changed = {Path(line[3:].strip()).name for line in r.stdout.splitlines() if line.strip()}
    return {p.name: ("uncommitted change" if p.name in changed else "committed") for p in paths}


@dataclass
class State:
    today: date
    export: Path | None  # the Rocket Money export, when the board was started on one; None: the Plaid + Apple feed
    rules: dict
    assumptions: dict
    window: model.Window
    first: date
    n_txns: int
    spend: dict[str, float]
    found: dict
    decisions: list[Decision]
    verdicts: list[VerdictRow]
    git: dict[str, str] | None
    walmart_measured: walmart.Measured | None = None
    walmart_export: float = 0.0  # what the export alone had for the family, window units
    walmart_dropped: float = 0.0  # other Walmart rows the capture stands in for, window units
    costco_measured: costco.Measured | None = None
    worth: list[cardworth.Worth] = field(default_factory=list)  # [card_worth].alternatives on the first one's spend
    carried: dict[str, dict[str, float]] = field(default_factory=dict)  # annual spend by family on each card's own accounts (and the card-worth spend)
    credit_years: dict[str, list[cardworth.AnnYear]] = field(default_factory=dict)  # [card_credits] cards, as the report prints them
    sync_alert: str | None = None  # a failed or overdue Plaid sync, shown on every page
    walmart_feed_span: float | None = None  # the feed's Walmart rows over the capture's own days, as a cross-check
    txns: list[ld.Txn] = field(default_factory=list)  # every labelled feed row, transfers and income included (cash flow); empty on an export
    root: Path | None = None
    checks: check.Checks | None = None  # the balance checks, as `penny check` runs them
    worth_now: networth.NetWorth | None = None  # every account's balance and month-end net worth; None on an export

    @property
    def noun(self) -> str:
        """What the figures come from, as the pages name it."""
        return "export" if self.export else "feed"


SYNC_STALE = timedelta(hours=36)  # the timer runs daily; a day and a half means it stopped


def sync_alert(root: Path, now: datetime) -> str | None:
    """The daily Plaid sync's last outcome, when it needs attention.

    Feeds fail silently, and the journal is read by nobody, so the board says it.
    No last-sync.json means Plaid was never set up here: nothing to say.
    """
    path = root / "data" / "plaid" / "production" / "last-sync.json"
    try:
        last = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    at = datetime.fromisoformat(last["at"])
    if not last.get("ok"):
        failed = ", ".join(f"{k}: {v}" for k, v in last.get("items", {}).items() if v != "ok")
        return f"Plaid sync failed {at.astimezone().date()} — {failed}"
    if now - at > SYNC_STALE:
        return f"No Plaid sync since {at.astimezone().date()}; check the penny-sync timer"
    return None


# --------------------------------------------------------------------------
# Caches. Each input is keyed on the stat of every file it reads, so a
# changed rules.toml, assumptions.toml, overrides.json, feed file, capture,
# wiki or git index misses and a quiet one hits. ``today`` is in the state's
# key: it moves daily.


def _log(msg: str) -> None:
    """One line to stderr: the penny-board unit's journal."""
    print(msg, file=sys.stderr, flush=True)


def _stat(p: Path | None) -> tuple | None:
    """A file's identity for a cache key; absent is a state of its own."""
    if p is None:
        return None
    try:
        st = p.stat()
    except OSError:
        return (str(p), None)
    return (str(p), st.st_mtime_ns, st.st_size, st.st_ino)


def _scan(d: Path, pattern: str = "*") -> tuple:
    """Every regular file directly in ``d`` matching ``pattern``, as cache-key
    stats. Temporary files (a sync's ``*.tmp``) are skipped, and a file that
    vanishes between listing and stat is skipped, not a 500."""
    out = []
    try:
        entries = list(d.glob(pattern))
    except OSError:
        return ()
    for f in entries:
        if f.suffix == ".tmp":
            continue
        try:
            st = f.stat()
        except FileNotFoundError:
            continue
        if stat.S_ISREG(st.st_mode):
            out.append((str(f), st.st_mtime_ns, st.st_size, st.st_ino))
    return tuple(sorted(out))


def _dirs(d: Path) -> tuple:
    """Every directory under ``d`` with its mtime: a doc added or removed shows."""
    out = []
    for top, _, _ in os.walk(d):
        try:
            out.append((top, os.stat(top).st_mtime_ns))
        except FileNotFoundError:
            continue
    return tuple(sorted(out))


_CACHE_LOCK = threading.Lock()
_CHECKS: dict[Path, tuple[tuple, check.Checks]] = {}


def load_checks(cfg: Config, rules: dict) -> check.Checks:
    """``check.run_all``, rerun only when a file it reads has changed: every
    page shows its warnings, and it parses every statement PDF.

    ``run_all`` reads the Plaid store's top-level files (items, ledgers,
    balances), never ``raw/``, which grows by a file a sync, so the key leaves
    it out. The reward rates it takes from ``rules`` are in the key."""
    data = cfg.root / "data"
    plaid_dir = data / "plaid" / "production"
    if not plaid_dir.is_dir():  # no Plaid store here; opening one would create it
        return check.Checks([], [], [])
    key = (_scan(plaid_dir), _scan(data / "statements"), _scan(data / "apple"), tuple(sorted(check.reward_rates(rules).items())))
    with _CACHE_LOCK:
        hit = _CHECKS.get(cfg.root)
    if hit and hit[0] == key:
        return hit[1]
    c = check.run_all(cfg.root, rules)
    with _CACHE_LOCK:
        _CHECKS[cfg.root] = (key, c)
    return c


_NETWORTH: dict[Path, tuple[tuple, networth.NetWorth | None]] = {}


def load_networth(cfg: Config, rules: dict, c: check.Checks, today: date) -> networth.NetWorth | None:
    """``networth.load`` on the statements the checks parsed, rerun only when
    the Plaid store, the Apple files or the day changes: it reads every ledger."""
    if cfg.export is not None:
        return None
    data = cfg.root / "data"
    lacks = frozenset(check.reward_rates(rules))
    key = (today, _scan(data / "plaid" / "production"), _scan(data / "statements"), _scan(data / "apple"), lacks)
    with _CACHE_LOCK:
        hit = _NETWORTH.get(cfg.root)
    if hit and hit[0] == key:
        return hit[1]
    nw = networth.load(cfg.root, today, c.statements, lacks)
    with _CACHE_LOCK:
        _NETWORTH[cfg.root] = (key, nw)
    return nw


_GIT_DIR: dict[Path, Path | None] = {}
_GIT: dict[tuple, tuple[tuple, dict[str, str] | None]] = {}


def _git_dir(root: Path) -> Path | None:
    if root not in _GIT_DIR:
        try:
            r = subprocess.run(["git", "rev-parse", "--absolute-git-dir"], cwd=root, capture_output=True, text=True, timeout=5, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return None  # not remembered: try again next time
        _GIT_DIR[root] = Path(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None
    return _GIT_DIR[root]


def cached_git_state(cfg: Config) -> dict[str, str] | None:
    """``git_state``, rerun when the files or the repo moved: the index (an
    add), HEAD and its reflog (a commit, checkout or reset)."""
    gd = _git_dir(cfg.root)
    if gd is None:
        return git_state(cfg)
    ck = (cfg.root, cfg.rules, cfg.assumptions)
    rest = (_stat(gd / "HEAD"), _stat(gd / "logs" / "HEAD"), _stat(cfg.rules), _stat(cfg.assumptions))
    key = (_stat(gd / "index"), *rest)
    with _CACHE_LOCK:
        hit = _GIT.get(ck)
    if hit and key in hit[0]:
        return hit[1]
    g = git_state(cfg)
    # git status may refresh the index it read; either stat means the same state.
    with _CACHE_LOCK:
        _GIT[ck] = ((key, (_stat(gd / "index"), *rest)), g)
    return g


def _cfg_key(cfg: Config) -> tuple:
    return (cfg.root, cfg.rules, cfg.assumptions, cfg.wiki, cfg.export)


def _state_key(cfg: Config, today: date) -> tuple:
    """Every file ``_load_state`` reads, by stat, and the day."""
    data = cfg.root / "data"
    key = [today, *(_stat(p) for p in (cfg.rules, home.DEFAULT_RULES, cfg.assumptions, cfg.wiki, cfg.export, cfg.root / feed.OVERRIDES, cfg.root / feed.MERCHANTS))]
    if cfg.export is None:
        key += [_scan(data / "plaid" / "production"), _scan(data / "apple", "*.csv"), _scan(data / "imports", "*.csv")]
    key += [_scan(data, "walmart-orders-*.json"), _scan(data, "costco-receipts-*.json"), _dirs(cfg.root / "docs")]
    return tuple(key)


_STATE: dict[tuple, tuple[tuple, State]] = {}


def base_state(cfg: Config, today: date) -> State:
    """``_load_state``, cached on ``_state_key``. Shared between requests, so
    callers read it or take a copy (``load_state``); none changes it."""
    ck, key = _cfg_key(cfg), _state_key(cfg, today)  # keyed before the read: a change mid-read misses next time
    with _CACHE_LOCK:
        hit = _STATE.get(ck)
    if hit and hit[0] == key:
        return hit[1]
    s = _load_state(cfg, today)
    with _CACHE_LOCK:
        _STATE[ck] = (key, s)
    return s


def load_state(cfg: Config, today: date | None = None) -> State:
    today = today or datetime.now().astimezone().date()
    s = base_state(cfg, today)
    # What moves without one of its files changing is worked out every time.
    c = load_checks(cfg, s.rules)
    nw = load_networth(cfg, s.rules, c, today)
    names = card_names(s.rules)
    if names and nw:
        nw = replace(nw, accounts=[replace(a, name=names.get(a.mask, a.name)) for a in nw.accounts])
    if names:
        c = replace(c, results=[replace(r, account=_named_check(r.account, names)) for r in c.results])
    return replace(s, git=cached_git_state(cfg), sync_alert=sync_alert(cfg.root, datetime.now(UTC)), checks=c, worth_now=nw)


def card_names(rules: dict) -> dict[str, str]:
    """Last four -> the card's label, from rules.toml [accounts]. Plaid names
    every Chase card "CREDIT CARD"; the board shows the card instead."""
    cards = rules.get("cards", {})
    return {str(m): cards[a["card"]]["label"] for m, a in rules.get("accounts", {}).items()
            if isinstance(a, dict) and cards.get(a.get("card"), {}).get("label")}


def _named_check(account: str, names: dict[str, str]) -> str:
    name, _, mask = account.rpartition(" …")
    return f"{names[mask]} …{mask}" if name and mask in names else account


def _load_state(cfg: Config, today: date) -> State:
    # git and sync_alert are left None here: load_state fills them in each time.
    rules, assumptions = model.resolve_points(home.load_rules(cfg.rules)), _toml(cfg.assumptions)
    model.validate_rules(rules, assumptions)
    if cfg.export is None:
        return load_feed_state(cfg, rules, assumptions, today)
    export = cfg.export
    ex = rules.get("export", {})
    txns = ld.load(export, expense_sign=ex.get("expense_sign", "positive"), exclude_categories=ex.get("exclude_categories", []))
    cat.categorize(txns, cat.build_families(rules))
    w = model.pick_window(txns)
    tx = model.in_window(txns, w)
    spend = model.spend_by_family(tx)
    # Rocket Money drops OnePay purchase rows, so the export's Walmart delivery
    # is a floor. Walmart's own order history replaces it when a capture exists.
    measured = walmart.newest_measured(cfg.root / "data") if WALMART_FAMILY in rules.get("families", {}) else None
    walmart_export = spend.get(WALMART_FAMILY, 0.0)
    walmart_dropped = 0.0
    if measured:
        spend[WALMART_FAMILY] = measured.annual / w.annualize
        # The capture, scaled to a year, is the whole year of Walmart orders.
        # Walmart.com rows are orders it lists; store rows before its first
        # order are months the scaling already stands in for. Counting either
        # again double-counts the same shopping.
        before = spend.get(WALMART_ONLINE, 0.0) + spend.get(WALMART_STORE, 0.0)
        spend.pop(WALMART_ONLINE, None)
        store = sum(t.amount for t in tx if t.family == WALMART_STORE and t.date >= measured.first)
        spend.pop(WALMART_STORE, None)
        if store > 0:
            spend[WALMART_STORE] = store
        walmart_dropped = before - max(store, 0.0)
    found = model.observed_fees(tx, rules)
    wiki = parse_wiki(cfg.wiki.read_text()) if cfg.wiki and cfg.wiki.is_file() else []
    decisions = rank(derive(rules, assumptions, spend, w.annualize, found, wiki, today, cfg.root))
    rows = verdict_rows(rules, assumptions, spend, w.annualize)
    receipts = costco.newest_measured(cfg.root / "data") if COSTCO_FAMILY in rules.get("families", {}) else None
    # The card figures, the same way `penny report` works them out: the
    # in-window export (no Walmart capture: card worth is about the Sapphire's
    # own accounts), and credits from a re-read that keeps excluded categories.
    annual = lambda sp: model.annualized(sp, w.annualize)
    alts = assumptions.get("card_worth", {}).get("alternatives", [])
    carried = {k: annual(cardworth.worth_spend(tx, rules, k)) for k, c in rules.get("cards", {}).items() if "accounts" in c or k in alts[:1]}
    worth = cardworth.card_worth(rules, assumptions, carried[alts[0]]) if alts else []
    credit_years = {}
    if rules.get("card_credits"):
        ctx = cardworth.load_for_credits(export, rules)
        for ckey in rules["card_credits"]:
            own = cardworth.card_first_row(ctx, rules, ckey)
            first = max(ctx[0].date, own) if own else ctx[0].date
            credit_years[ckey] = cardworth.anniversary_years(ctx, rules, ckey, first=first, last=ctx[-1].date)
    return State(today, Path(export), rules, assumptions, w, tx[0].date if tx else w.start, len(tx), spend, found, decisions, rows, None, measured, walmart_export, walmart_dropped, receipts, worth, carried, credit_years,
                 None)


def load_feed_state(cfg: Config, rules: dict, assumptions: dict, today: date) -> State:
    """The board on the Plaid + Apple Card feed (M3). Walmart spend is the feed's
    own: it has the OnePay rows the export dropped, and over the capture's days it
    comes within 4% of the order history, under it where Walmart charged less
    than the order total (docs/budget-plan.md § Two-source comparison). The
    capture is shown beside it as a cross-check, never substituted."""
    everything = feed.labelled(cfg.root, rules, spend_only=False)
    names = card_names(rules)
    for t in everything:
        t.account = names.get(t.account_number, t.account) if t.account_number else t.account
    txns = [t for t in everything if feed.is_spend(t)]
    if not txns:
        raise FileNotFoundError(f"no transactions under {cfg.root / 'data'}; run penny import csv FILE, penny plaid sync, or penny demo to see it with sample data")
    w = model.pick_window(txns)
    tx = model.in_window(txns, w)
    spend = model.spend_by_family(tx)
    measured = walmart.newest_measured(cfg.root / "data") if WALMART_FAMILY in rules.get("families", {}) else None
    span = None
    if measured:
        span = sum(t.amount for t in txns if t.family in (WALMART_FAMILY, WALMART_ONLINE, WALMART_STORE)
                   and measured.first <= t.date <= measured.last)
    found = model.observed_fees(tx, rules)
    wiki = parse_wiki(cfg.wiki.read_text()) if cfg.wiki and cfg.wiki.is_file() else []
    decisions = rank(derive(rules, assumptions, spend, w.annualize, found, wiki, today, cfg.root))
    rows = verdict_rows(rules, assumptions, spend, w.annualize)
    receipts = costco.newest_measured(cfg.root / "data") if COSTCO_FAMILY in rules.get("families", {}) else None
    annual = lambda sp: model.annualized(sp, w.annualize)
    alts = assumptions.get("card_worth", {}).get("alternatives", [])
    carried = {k: annual(cardworth.worth_spend(tx, rules, k)) for k, c in rules.get("cards", {}).items() if "accounts" in c or k in alts[:1]}
    worth = cardworth.card_worth(rules, assumptions, carried[alts[0]]) if alts else []
    # Credits from every row, transfers included: a statement credit is real
    # money whatever the feed calls it.
    credit_years = {}
    for ckey in rules.get("card_credits", {}):
        own = cardworth.card_first_row(everything, rules, ckey)
        first = max(everything[0].date, own) if own else everything[0].date
        credit_years[ckey] = cardworth.anniversary_years(everything, rules, ckey, first=first, last=everything[-1].date)
    return State(today, None, rules, assumptions, w, tx[0].date if tx else w.start, len(tx), spend, found, decisions, rows,
                 None, measured, spend.get(WALMART_FAMILY, 0.0), 0.0, receipts, worth, carried, credit_years,
                 None, span, everything, cfg.root)


# --------------------------------------------------------------------------
# Recording: allowlisted, comment-preserving, audited


class Refused(ValueError):
    pass


ALLOWED = (
    "assumptions: [<membership>.<perk>] low, base, high (numbers) and note (text); "
    "rules: [cards.<card>] verified (true only) and verified_on (YYYY-MM-DD); [held] <membership>_tier (one of its tiers); "
    "[cards.<baseline>] default_rate and rates.<family already listed> (0 to 1); "
    "[memberships.<m>] or [memberships.<m>.tiers.<t>] fee (0 or more); "
    "assumptions: [budget] <category already listed> (0 or more)"
)
_PART = re.compile(r"[A-Za-z0-9_-]+")


@dataclass
class Write:
    file: str  # rules | assumptions
    section: str
    key: str
    subkey: str | None
    value: object
    note: str | None


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _iso_date(s: str) -> bool:
    try:
        return date.fromisoformat(s).isoformat() == s
    except ValueError:
        return False


def check_write(w, rules: dict) -> Write:
    """Validate one requested write against the allowlist, or raise Refused."""
    if not isinstance(w, dict):
        raise Refused("each write must be an object {file, section, key, subkey?, value, note?}")
    file = str(w.get("file", "")).removesuffix(".toml")
    section, key, subkey, value, note = w.get("section"), w.get("key"), w.get("subkey"), w.get("value"), w.get("note")
    where = f"{file} [{section}] {key}{'.' + str(subkey) if subkey else ''}"
    if not (isinstance(section, str) and isinstance(key, str) and (subkey is None or isinstance(subkey, str)) and (note is None or isinstance(note, str))):
        raise Refused(f"malformed write: {where}. Allowed: {ALLOWED}")
    parts = section.split(".")
    if not all(_PART.fullmatch(p) for p in parts) or not _PART.fullmatch(key) or (subkey is not None and not _PART.fullmatch(subkey)):
        raise Refused(f"malformed write: {where}. Allowed: {ALLOWED}")
    ok = False
    if file == "assumptions" and section == "budget" and subkey is None:
        ok = _is_num(value) and value >= 0
    elif file == "assumptions" and subkey is None and len(parts) == 2:
        ok = (key in LEVELS and _is_num(value)) or (key == "note" and isinstance(value, str) and len(value) <= 2000)
    elif file == "rules":
        mems = rules.get("memberships", {})
        baseline = rules.get("held", {}).get("baseline_card")
        if len(parts) == 2 and parts[0] == "cards" and key == "verified" and subkey is None:
            ok = value is True
        elif len(parts) == 2 and parts[0] == "cards" and key == "verified_on" and subkey is None:
            ok = isinstance(value, str) and _iso_date(value)
        elif section == "held" and key.endswith("_tier") and subkey is None:
            ok = isinstance(value, str) and value in (mems.get(key[: -len("_tier")], {}).get("tiers") or {})
        elif section == f"cards.{baseline}" and ((key == "default_rate" and subkey is None) or (key == "rates" and subkey in rules.get("cards", {}).get(baseline, {}).get("rates", {}))):
            ok = _is_num(value) and 0 <= value <= 1
        elif key == "fee" and subkey is None and parts[0] == "memberships" and (len(parts) == 2 or (len(parts) == 4 and parts[2] == "tiers")):
            ok = _is_num(value) and value >= 0
    if not ok:
        raise Refused(f"not allowed: {where} = {value!r}. Allowed: {ALLOWED}")
    return Write(file, section, key, subkey, value, note)


def _lookup(doc: dict, w: Write):
    node = doc
    for part in w.section.split(".") + [w.key] + ([w.subkey] if w.subkey else []):
        node = node[part]
    return node


def record(cfg: Config, payload, today: date | None = None, now: datetime | None = None) -> dict:
    """Apply one write or a list of them. All are checked before any file
    changes; each file is replaced once. No-op writes are skipped."""
    today = today or datetime.now().astimezone().date()
    now = now or datetime.now().astimezone()
    items = payload if isinstance(payload, list) else [payload]
    if not items:
        raise Refused("nothing to record")
    paths = {"rules": cfg.rules, "assumptions": cfg.assumptions}
    audit = []
    with tomledit.LOCK:
        writes = [check_write(i, home.load_rules(cfg.rules)) for i in items]
        new_text: dict[str, tuple[str, str]] = {}
        for w in writes:
            path = paths[w.file]
            old, text = new_text.get(w.file) or (path.read_text(),) * 2
            value = w.value
            if w.file == "assumptions" and w.key == "note" and not value.startswith("Recorded "):
                value = f"Recorded {today.isoformat()}: {value.strip()}"
            try:
                # rules.toml is the instance, read over the defaults: the value
                # in force is the merged one, and a key only the defaults have
                # is written into the instance as an override.
                before = _lookup(home.merged_text(text) if w.file == "rules" else tomllib.loads(text), w)
                if before == value and isinstance(before, bool) == isinstance(value, bool):
                    new_text[w.file] = (old, text)
                    continue
                if w.file == "rules":
                    text = tomledit.override(text, w.section, w.key, w.subkey, value)
                else:
                    text = tomledit.set_inline(text, w.section, w.key, w.subkey, value) if w.subkey else tomledit.set_key(text, w.section, w.key, value)
            except (KeyError, TypeError):
                raise Refused(f"{path.name} has no [{w.section}] {w.key}{'.' + w.subkey if w.subkey else ''}; the board never adds a key — edit the file by hand") from None
            except ValueError as e:
                raise Refused(f"{path.name} [{w.section}] {w.key}: {e}") from None
            new_text[w.file] = (old, text)
            audit.append({"when": now.isoformat(timespec="seconds"), "file": path.name, "section": w.section, "key": w.key + (f".{w.subkey}" if w.subkey else ""), "before": before, "after": value, "note": w.note})
        changed = {file: ot for file, ot in new_text.items() if ot[0] != ot[1]}
        # Every file must still be as it was read before any is replaced, so a
        # batch across rules.toml and assumptions.toml lands whole or not at all.
        for file, (old, _) in changed.items():
            if paths[file].read_text() != old:
                raise Refused(f"{paths[file].name} changed on disk while recording; reload and try again")
        written: list[str] = []
        try:
            for file, (old, text) in changed.items():
                tomledit.write_edits(paths[file], partial(_swap, old=old, new=text, name=paths[file].name))
                written.append(file)
        except BaseException:
            # A later file refused or failed: put the earlier ones back. One
            # that can't be (changed again since) stays written, and audited.
            kept = []
            for file in reversed(written):
                old, text = changed[file]
                try:
                    tomledit.write_edits(paths[file], partial(_swap, old=text, new=old, name=paths[file].name))
                except Exception:  # noqa: BLE001 -- any undo failure is logged and the file kept
                    _log(f"board: could not undo {paths[file].name} after a failed batch; it stays as recorded\n{traceback.format_exc()}")
                    kept.append(paths[file].name)
            _audit(cfg, [a for a in audit if a["file"] in kept])
            raise
        _audit(cfg, audit)
    return {"recorded": [{k: a[k] for k in ("file", "section", "key", "after")} for a in audit], "git": cached_git_state(cfg)}


def _swap(current: str, old: str, new: str, name: str) -> str:
    """A ``write_edits`` edit: ``new`` if the file is still ``old``, else refuse."""
    if current != old:
        raise Refused(f"{name} changed on disk while recording; reload and try again")
    return new


def _audit(cfg: Config, lines: list[dict]) -> None:
    """Append to ``data/board-decisions.jsonl``. It runs after the files are
    replaced, so the save already happened: a failure here is logged, not
    reported as a failed save."""
    if not lines:
        return
    try:
        d = cfg.root / "data"
        d.mkdir(exist_ok=True)
        with open(d / "board-decisions.jsonl", "a") as f:
            f.writelines(json.dumps(a, default=str) + "\n" for a in lines)
    except Exception:  # noqa: BLE001 -- the save stands; a failed audit line is logged
        _log(f"board: saved, but the audit line was not written: {lines!r}\n{traceback.format_exc()}")


def spend_rows(cfg: Config, rules: dict, today: date | None = None) -> list[ld.Txn]:
    """The labelled feed's spend rows, from the cached state when the board
    runs on the feed rather than rereading every ledger for one ID."""
    if cfg.export is None:
        try:
            return [t for t in base_state(cfg, today or datetime.now().astimezone().date()).txns if feed.is_spend(t)]
        except FileNotFoundError:  # no feed rows at all
            return []
    return feed.labelled(cfg.root, rules)


def record_override(cfg: Config, payload, now: datetime | None = None, today: date | None = None) -> dict:
    """Set or clear one feed row's hand-set family / category in
    ``data/overrides.json`` (the Transactions page), or with ``scope:
    "merchant"`` every row from that row's merchant, in ``data/merchants.json``.
    Audited like ``record``."""
    now = now or datetime.now().astimezone()
    if not isinstance(payload, dict) or not isinstance(payload.get("id"), str):
        raise Refused("send {id, family?, category?} or {id, clear: true}, with scope: \"merchant\" for every row from its merchant")
    if payload.get("scope", "row") not in ("row", "merchant"):
        raise Refused("scope is row or merchant")
    by_merchant = payload.get("scope") == "merchant"
    tid, family, category, clear = payload["id"], payload.get("family"), payload.get("category"), payload.get("clear") is True
    if not all(x is None or isinstance(x, str) for x in (family, category)):
        raise Refused("family and category are text")
    family, category = (family or "").strip() or None, (category or "").strip().lower() or None
    if not clear and not (family or category):
        raise Refused("nothing to save: pick a family or a category")
    rules = model.resolve_points(home.load_rules(cfg.rules))
    txns = spend_rows(cfg, rules, today)
    try:
        cat.check_override(txns, rules, tid, None if clear else family, None if clear else category)
    except ValueError as e:
        raise Refused(str(e)) from None
    if by_merchant:
        path, key = cfg.root / feed.MERCHANTS, cat.merchant_of(next(t for t in txns if t.txn_id == tid)).lower()
    else:
        path, key = cfg.root / feed.OVERRIDES, tid
    with tomledit.LOCK:
        before = copy.deepcopy(cat.load_overrides(path).get(key, {}))
        after = cat.save_override(path, key, clear=True) if clear else cat.save_override(path, key, family, category)
        if after != before:
            _audit(cfg, [{"when": now.isoformat(timespec="seconds"), "file": path.name, "section": key, "key": "override", "before": before, "after": after, "note": None}])
    out = {"id": tid, "override": after, "changed": after != before}
    return {**out, "merchant": key} if by_merchant else out


# --------------------------------------------------------------------------
# Page
#
# The page asks questions, not "decisions". Every open input is rendered as
# one of five groups:
#   now    a to-do file row with a future date on it
#   ask    only you can answer it, and the answer moves real money
#   check  a fact to compare against card terms
#   quiet  nothing to do: settled, or moves less than WASH a year
#   wiki   open work tracked in the to-do file; nothing on this page writes it
# Each item says what is being asked, what the answer changes, and how to
# find it out. File names and TOML keys sit under "What gets saved".

# Indigo Ledger (docs/design/), dark mode as exported. Only the tokens used.
CSS = """
:root{
  /* Ledger (docs/ui-overhaul-plan.md): paper and deep green; dark follows the phone */
  color-scheme:light;
  --background:#F6F3EC; --foreground:#1C2320; --side:#EFEBE2;
  --card:#FBF9F4; --muted:#ECE7DC; --muted-foreground:#5C645E; --faint:#858B85;
  --primary:#1F5F4A; --primary-foreground:#F6F3EC; --logo-dot:#1F5F4A;
  --border:#E2DCCF; --input:#D3CCBC; --ring:#1F5F4A; --track:#E7E1D4;
  --good:#1F6B4F; --warn:#9A6412; --crit:#A63D2A; --info:#1F5F4A;
  --radius:6px;
  --font-sans:"IBM Plex Sans",-apple-system,BlinkMacSystemFont,system-ui,sans-serif;
  --font-display:"Newsreader",Georgia,serif;
  --font-mono:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,monospace;
  --text-title:500 2.125rem/2.5rem var(--font-display);
  --text-heading:500 1.25rem/1.75rem var(--font-display);
  --text-body:400 0.875rem/1.375rem var(--font-sans);
  --text-caption:400 0.75rem/1rem var(--font-sans);
  --text-label:500 0.8125rem/1.125rem var(--font-sans);
  --text-overline:600 0.6875rem/1rem var(--font-sans);
  --text-figure:500 2.25rem/2.5rem var(--font-display);
}
@media (prefers-color-scheme:dark){:root{
  color-scheme:dark;
  --background:#131614; --foreground:#ECE9E1; --side:#161A17;
  --card:#1A1E1B; --muted:#232925; --muted-foreground:#A9AEA7; --faint:#7F857E;
  --primary:#6FC3A0; --primary-foreground:#0F1512; --logo-dot:#6FC3A0;
  --border:#2A302C; --input:#3A423C; --ring:#6FC3A0; --track:#262C28;
  --good:#6FC3A0; --warn:#E3B25C; --crit:#E58A70; --info:#6FC3A0;
}}
*{box-sizing:border-box}
body{margin:0;background:var(--background);color:var(--foreground);font:var(--text-body);-webkit-text-size-adjust:100%}
h1,h2,h3,p{margin:0}
a{color:var(--foreground);text-decoration-color:var(--faint);text-underline-offset:3px}
code,pre,.mono{font-family:var(--font-mono);font-variant-numeric:tabular-nums}
code{font-size:.9em;background:var(--muted);padding:1px 5px;border-radius:calc(var(--radius) - 2px)}
:focus-visible{outline:2px solid var(--ring);outline-offset:2px}
button{font:inherit;color:inherit;cursor:pointer}

h1{font:var(--text-title);letter-spacing:-.01em}
.lede{color:var(--muted-foreground);margin-top:4px;max-width:60ch}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chip{font:var(--text-caption);padding:5px 9px;border:1px solid var(--input);border-radius:999px;color:var(--muted-foreground);white-space:nowrap}
.chip b{font-family:var(--font-mono);font-weight:500;color:var(--foreground)}

section{margin-bottom:40px}
h2{font:var(--text-overline);letter-spacing:.1em;text-transform:uppercase;color:var(--muted-foreground)}
.empty{color:var(--muted-foreground);padding:14px 16px;border:1px dashed var(--input);border-radius:var(--radius)}

/* badges: dot + muted pill */
.badge{display:inline-flex;align-items:center;gap:6px;font:var(--text-label);font-weight:600;padding:3px 9px;border-radius:999px;white-space:nowrap;color:var(--c);background:color-mix(in srgb,var(--c) 14%,transparent)}
.badge::before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor}
.good{--c:var(--good)} .crit{--c:var(--crit)} .warn{--c:var(--warn)} .neutral{--c:var(--muted-foreground)}

/* now */
.now{border:1px solid var(--input);border-left:3px solid var(--crit);border-radius:var(--radius);background:var(--card);padding:16px 18px;display:grid;grid-template-columns:minmax(0,1fr) auto;gap:10px 24px;margin-top:24px}
.now h3{font:var(--text-heading)}
.now p{color:var(--muted-foreground);margin-top:4px}
.now .days{text-align:right;font:var(--text-figure);font-variant-numeric:tabular-nums;color:var(--crit)}
.now .days small{display:block;font:var(--text-caption);color:var(--muted-foreground)}
@media (max-width:640px){.now{grid-template-columns:minmax(0,1fr)}.now .days{text-align:left}}

/* verdicts */
.vgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:12px}
.vcard{background:var(--card);border:1px solid var(--input);border-radius:var(--radius);padding:16px;display:flex;flex-direction:column;gap:8px}
.vcard .head{display:flex;justify-content:space-between;align-items:flex-start;gap:8px}
.vcard h3{font:var(--text-heading)}
.vcard .sub{font:var(--text-caption);color:var(--faint)}
.vcard .net{font:var(--text-figure);font-variant-numeric:tabular-nums;letter-spacing:-.01em}
.vcard .net small{font:var(--text-caption);color:var(--muted-foreground);margin-left:4px}
.vcard .cond{font:var(--text-label);color:var(--foreground)}
.vcard .why{color:var(--muted-foreground)}
.vcard .hang{margin-top:auto;padding-top:8px;border-top:1px solid var(--border);font:var(--text-label);color:var(--muted-foreground)}
.vcard .hang a{color:var(--foreground);font-weight:600}
.bar{position:relative;height:30px;margin-block:2px}
.bar .axis{position:absolute;left:0;right:0;top:12px;height:1px;background:var(--border)}
.bar .zero{position:absolute;top:4px;height:17px;width:1px;background:var(--faint)}
.bar .range{position:absolute;top:8px;height:9px;border-radius:999px;background:color-mix(in srgb,var(--info) 28%,transparent);border:1px solid color-mix(in srgb,var(--info) 70%,transparent)}
.bar .mark{position:absolute;top:5px;width:3px;height:15px;border-radius:2px;background:var(--info);margin-left:-1.5px}
.bar .lab{position:absolute;top:20px;font:400 10px/10px var(--font-mono);color:var(--faint);white-space:nowrap}
.bar .lab.lo{transform:translateX(-100%);padding-right:3px}
.bar .lab.hi{padding-left:3px}

/* questions */
.qlist{display:flex;flex-direction:column;gap:10px}
.q{background:var(--card);border:1px solid var(--input);border-radius:var(--radius)}
.q.open{border-color:var(--faint)}
.qhead{display:grid;grid-template-columns:28px minmax(0,1fr) auto;gap:6px 12px;width:100%;padding:14px 16px;background:none;border:0;text-align:left;min-height:44px;border-radius:var(--radius)}
.qn{width:28px;height:28px;border-radius:50%;border:1px solid var(--input);display:grid;place-items:center;font:500 .75rem/1 var(--font-mono);color:var(--muted-foreground)}
.qt{display:block;font:var(--text-heading);font-size:1.0625rem;line-height:1.5rem;padding-top:2px}
.qwhy{display:block;color:var(--muted-foreground);margin-top:4px}
.qside{display:flex;flex-direction:column;align-items:flex-end;gap:6px;padding-top:4px}
.stake{font:500 .8125rem/1 var(--font-mono);color:var(--muted-foreground);white-space:nowrap}
.go{font:var(--text-label);font-weight:600;white-space:nowrap}
.go::after{content:" ›"}
.q.open .go{font-size:0}
.q.open .go::after{content:"Close";font:var(--text-label);font-weight:600}
@media (max-width:640px){.qhead{grid-template-columns:28px minmax(0,1fr)}.qside{grid-column:2;flex-direction:row;align-items:center;justify-content:space-between;padding-top:0}}
.qbody{display:none;border-top:1px solid var(--border);padding:16px;flex-direction:column;gap:14px}
.q.open .qbody{display:flex}
.how{background:var(--muted);border-radius:var(--radius);padding:10px 12px}
.how b{display:block;font:var(--text-overline);letter-spacing:.06em;text-transform:uppercase;color:var(--faint);margin-bottom:2px}
.lbl{font:var(--text-label);color:var(--muted-foreground);margin-bottom:6px;display:block}
.perk + .perk{border-top:1px solid var(--border);padding-top:14px}
.perk{display:flex;flex-direction:column;gap:10px}
.perk .ask{font:var(--text-label);font-size:.9375rem;color:var(--foreground)}
.perk .ask small{display:block;font:var(--text-body);color:var(--muted-foreground);margin-top:2px}
.choices{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:8px}
.choice{text-align:left;background:var(--background);border:1px solid var(--input);border-radius:var(--radius);padding:10px 12px;min-height:44px;font:var(--text-body);font-weight:600}
.choice small{display:block;font:var(--text-caption);color:var(--muted-foreground);margin-top:2px}
.choice:hover{border-color:var(--faint)}
.choice[aria-pressed="true"]{border-color:var(--ring);background:color-mix(in srgb,var(--primary) 12%,var(--card))}
.amount{display:flex;flex-wrap:wrap;align-items:center;gap:8px;font:var(--text-label);color:var(--muted-foreground)}
input,textarea{font:16px/1.3 var(--font-mono);color:var(--foreground);background:var(--background);border:1px solid var(--input);border-radius:var(--radius);padding:9px 10px}
input[type=number]{width:8.5rem;text-align:right}
textarea{width:100%;min-height:56px;font-family:var(--font-sans);resize:vertical}
input:focus,textarea:focus{border-color:var(--ring);outline:none}
details{border-top:1px solid var(--border);padding-top:8px}
summary{cursor:pointer;min-height:36px;display:flex;align-items:center;gap:6px;font:var(--text-label);color:var(--muted-foreground);list-style:none}
summary::-webkit-details-marker{display:none}
summary::before{content:"▸";font-size:.7em}
details[open] > summary::before{content:"▾"}
details[open] > summary{color:var(--foreground)}
.range3{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;padding-top:4px}
.range3 label{display:flex;flex-direction:column;gap:4px;font:var(--text-caption);color:var(--muted-foreground)}
.range3 input{width:100%}
.result{display:flex;flex-wrap:wrap;align-items:baseline;gap:6px 10px;padding:10px 12px;border:1px solid var(--border);border-radius:var(--radius)}
.result span{color:var(--muted-foreground)}
.netv{font:600 .9375rem/1.2 var(--font-mono);color:var(--c)}
.actions{display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px}
.btn{min-height:44px;padding:0 16px;border-radius:var(--radius);border:1px solid var(--input);background:var(--muted);font:var(--text-body);font-weight:600;white-space:nowrap}
.btn.primary{background:var(--primary);border-color:var(--primary);color:var(--primary-foreground)}
.btn:disabled{opacity:.45;cursor:default}
.status{font:var(--text-label);color:var(--muted-foreground)}
.status.ok{color:var(--good)} .status.err{color:var(--crit)}
.hint{font:var(--text-caption);color:var(--faint)}
.saved{display:flex;flex-direction:column;gap:8px;font:var(--text-caption);color:var(--muted-foreground)}
.saved ul{margin:0;padding-left:18px;display:flex;flex-direction:column;gap:3px}
.saved pre{margin:0;background:var(--background);border:1px solid var(--border);border-radius:var(--radius);padding:8px 10px;font-size:.75rem;line-height:1.35;white-space:pre-wrap;color:var(--foreground)}

/* nothing to do, wiki */
.plain{background:var(--card);border:1px solid var(--input);border-radius:var(--radius)}
.plain > li{list-style:none;padding:12px 16px;border-top:1px solid var(--border)}
.plain > li:first-child{border-top:0}
.plain{margin:0;padding:0}
.plain .t{font:var(--text-label);font-size:.875rem;color:var(--foreground);display:flex;flex-wrap:wrap;gap:6px 10px;align-items:baseline}
.plain .t .mono{color:var(--faint);font:var(--text-caption)}
.plain p{color:var(--muted-foreground);margin-top:2px}
.plain details{margin-top:8px}
.plain .q{border:0;background:none}

/* rail */
aside{display:flex;flex-direction:column;gap:28px}
aside h2{margin-bottom:10px}
aside dl{margin:0;display:grid;grid-template-columns:auto minmax(0,1fr);gap:6px 12px;font:var(--text-label)}
aside dt{color:var(--muted-foreground)}
aside dd{margin:0;font-family:var(--font-mono);font-size:.75rem;text-align:right}
aside dd.warn{color:var(--warn)}
aside ul{margin:0;padding:0;list-style:none;display:flex;flex-direction:column;gap:8px}
aside li{display:grid;grid-template-columns:14px minmax(0,1fr);gap:8px}
aside li i{width:8px;height:8px;border-radius:50%;background:var(--c);margin-top:7px}
aside li small{display:block;font:var(--text-caption);color:var(--faint)}
aside .fine{font:var(--text-caption);color:var(--faint)}

/* pages (docs/board-redesign-plan.md) */
[hidden]{display:none!important}
.app{display:grid;grid-template-columns:236px minmax(0,1fr);min-height:100vh;background:linear-gradient(90deg,var(--side) 235px,var(--border) 235px 236px,transparent 236px)}
.side{position:sticky;top:0;height:100vh;overflow-y:auto;display:flex;flex-direction:column;gap:18px;padding:24px 14px 18px;background:var(--side);border-right:1px solid var(--border)}
.brand{display:flex;align-items:center;padding:0 10px;color:var(--foreground);text-decoration:none}
.brand .logo{height:30px;width:auto;display:block}
.logo .dot{fill:var(--logo-dot)}
.side .links{display:flex;flex-direction:column;gap:2px}
.side .grp{font:var(--text-overline);letter-spacing:.1em;text-transform:uppercase;color:var(--faint);padding:16px 10px 6px}
.side .links a{display:flex;align-items:center;gap:10px;min-height:38px;padding:0 10px;border-radius:var(--radius);font:var(--text-label);font-size:.875rem;color:var(--muted-foreground);text-decoration:none}
.side .links a svg{flex-shrink:0;color:var(--faint)}
.side .links a:hover{color:var(--foreground);background:var(--muted)}
.side .links a.on{color:var(--foreground);background:var(--card);box-shadow:inset 0 0 0 1px var(--border)}
.side .links a.on svg{color:var(--primary)}
.side .links .count{margin-left:auto}
.count{margin-left:6px;padding:1px 7px;border-radius:999px;background:var(--primary);color:var(--primary-foreground);font:600 11px/16px var(--font-mono)}
.side .meta{margin-top:auto;padding:0 10px;font:var(--text-caption);color:var(--faint)}
.side .meta b{font:500 12px/1 var(--font-mono);color:var(--foreground)}
.content{min-width:0;padding:0 clamp(16px,4vw,48px) 64px}
.content > main{max-width:1120px;margin:0 auto}
@media (max-width:900px){
  .app{display:block;background:none}
  .side{position:static;height:auto;overflow:visible;flex-direction:row;flex-wrap:wrap;align-items:center;gap:6px 12px;padding:14px 16px 0;border-right:0;border-bottom:1px solid var(--border)}
  .brand{padding:0}
  .side .links{order:3;flex-direction:row;flex-wrap:nowrap;overflow-x:auto;scrollbar-width:none;width:calc(100% + 32px);margin-inline:-16px;padding:0 12px 8px}
  .side .links::-webkit-scrollbar{display:none}
  .side .grp,.side .links a svg{display:none}
  .side .links a{white-space:nowrap;min-height:44px;padding:0 12px}
  .side .links a.on{background:var(--muted);box-shadow:none}
  .side .links .count{margin-left:6px}
  .side .meta{margin:0 0 0 auto;padding:0;text-align:right}
}
main{padding-top:28px;display:flex;flex-direction:column;gap:28px}
.pagehead{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:12px 24px}
.pagehead h1{font:var(--text-title);letter-spacing:-.01em}
.pagehead .chips{margin-top:6px}
.ov{font:var(--text-overline);letter-spacing:.1em;text-transform:uppercase;color:var(--muted-foreground)}
.muted{color:var(--muted-foreground)} .faint{color:var(--faint)}
.pos{color:var(--good)} .neg{color:var(--crit)} .warnc{color:var(--warn)}
.syncbar{margin:0 0 16px;padding:10px 14px;border-radius:10px;font:var(--text-label);color:var(--crit);border:1px solid color-mix(in srgb,var(--crit) 40%,transparent);background:color-mix(in srgb,var(--crit) 8%,transparent)}
.note{font:var(--text-caption);color:var(--faint)}
.note a{color:var(--muted-foreground);font-weight:600;text-decoration:none}
.panel{background:var(--card);border:1px solid var(--input);border-radius:var(--radius)}
.panel.pad{padding:14px 16px;display:flex;flex-direction:column;gap:12px}
.chip.w{color:var(--warn);border-color:color-mix(in srgb,var(--warn) 40%,transparent)}
.badge.lg{font-size:.875rem;padding:6px 12px}
a.btn,span.btn{display:inline-flex;align-items:center;justify-content:center;text-decoration:none;align-self:flex-start}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px}
.tile{background:var(--card);border:1px solid var(--input);border-radius:var(--radius);padding:18px 20px;display:flex;flex-direction:column;gap:4px;text-decoration:none}
a.tile:hover,a.row:hover,a.wcard:hover,.strip a:hover{background:color-mix(in srgb,var(--muted) 60%,var(--card))}
.tile .big{font:var(--text-figure);font-variant-numeric:tabular-nums;letter-spacing:-.01em}
.tile .foot{font:var(--text-caption);color:var(--faint);margin-top:4px}
.tile .foot.warnc{color:var(--warn)}
.cols{display:grid;grid-template-columns:minmax(0,7fr) minmax(0,5fr);gap:24px;align-items:start}
.cols.even{grid-template-columns:repeat(2,minmax(0,1fr))}
.cols.rev{grid-template-columns:minmax(0,5fr) minmax(0,7fr)}
@media (max-width:900px){.cols,.cols.even,.cols.rev{grid-template-columns:minmax(0,1fr)}}
.stack{display:flex;flex-direction:column;gap:24px;min-width:0}
.block{display:flex;flex-direction:column;gap:12px;min-width:0;margin:0}
.bhead{display:flex;align-items:baseline;justify-content:space-between;gap:12px}
.bhead a{font:var(--text-label);font-weight:600;color:var(--muted-foreground);text-decoration:none}
.row{display:grid;grid-template-columns:minmax(0,1fr) auto auto;gap:6px 16px;align-items:center;padding:12px 16px;border-top:1px solid var(--border);text-decoration:none;min-height:44px}
.row:first-child{border-top:0}
.kv{display:flex;flex-direction:column;gap:2px;min-width:0}
.kv b{font-weight:600;font-size:.9375rem}
.kv small{font:var(--text-caption);color:var(--faint)}
.val{font:500 .8125rem/1 var(--font-mono);white-space:nowrap}
.swipe{padding:4px 16px}
.swipe > div{display:grid;grid-template-columns:minmax(0,1fr) auto 52px;gap:12px;padding:9px 0;border-top:1px solid var(--border);align-items:baseline}
.swipe > div:first-child{border-top:0}
.swipe .c{font:600 .8125rem/1.2 var(--font-sans);text-align:right}
.swipe .r{font:400 .8125rem/1.2 var(--font-mono);color:var(--faint);text-align:right}
@media (min-width:901px){.swipe.two{columns:2;column-gap:40px}.swipe.two > div{break-inside:avoid}}
.hbars{padding:16px 20px;display:flex;flex-direction:column;gap:10px}
.hbar{display:grid;grid-template-columns:minmax(0,190px) minmax(0,1fr) 64px;gap:12px;align-items:center}
.hbar .track{position:relative;height:10px;background:var(--track);border-radius:5px}
.hbar .fill{position:absolute;left:0;top:0;bottom:0;background:var(--info);border-radius:5px}
.hbar.dim > span:first-child,.hbar.dim .v{color:var(--faint)}
.hbar.dim .fill{background:var(--faint)}
.hbar .v{font:500 .8125rem/1 var(--font-mono);text-align:right}
.hfoot{display:flex;flex-wrap:wrap;justify-content:space-between;gap:6px 16px;padding-top:8px;border-top:1px solid var(--border)}
.hfoot a{font:500 .75rem/1.2 var(--font-mono);color:var(--muted-foreground);text-decoration:none;white-space:nowrap}
.wcard{display:grid;grid-template-columns:240px minmax(0,1fr) 130px 150px;gap:8px 20px;align-items:start;padding:18px 20px;border-top:1px solid var(--border);text-decoration:none}
.wcard:first-child{border-top:0}
.wcard.dim{background:var(--background)}
.wcard.dim .name,.wcard.dim .amt b{color:var(--muted-foreground)}
.wcard .who,.wcard .what,.wcard .amt{display:flex;flex-direction:column;gap:4px;min-width:0}
.wcard .name{font-weight:600;font-size:1rem}
.wcard small{font:var(--text-caption);color:var(--faint)}
.wcard .what small{font:var(--text-body);font-size:.8125rem;color:var(--muted-foreground)}
.wcard .amt b{font:600 1.125rem/1.4 var(--font-mono)}
.wcard .badge{justify-self:end}
@media (max-width:900px){
  .wcard{grid-template-columns:minmax(0,1fr) auto;padding:14px 16px}
  .wcard .who{grid-column:1;grid-row:1} .wcard .badge{grid-column:2;grid-row:1}
  .wcard .amt{grid-column:1/-1;grid-row:2;flex-direction:row;align-items:baseline;gap:8px} .wcard .what{grid-column:1/-1;grid-row:3}
}
.tscroll{overflow-x:auto}
.t{border-collapse:collapse;width:100%}
.t th{font:var(--text-overline);letter-spacing:.06em;text-transform:uppercase;color:var(--faint);text-align:left;padding:8px 12px;border-bottom:1px solid var(--input);white-space:nowrap}
.t td{padding:10px 12px;border-bottom:1px solid var(--border);vertical-align:top}
.t tr:last-child td{border-bottom:0}
.t .n{text-align:right;font-family:var(--font-mono);font-variant-numeric:tabular-nums;font-size:.8125rem;white-space:nowrap}
.t tr.total td{font-weight:600}
@media (max-width:640px){
  .t.stack thead{display:none}
  .t.stack tr{display:block;padding:10px 12px;border-bottom:1px solid var(--border)}
  .t.stack tr:last-child{border-bottom:0}
  .t.stack td{display:flex;justify-content:space-between;gap:12px;padding:3px 0;border:0;text-align:right}
  .t.stack td:first-child{display:block;text-align:left;font-weight:600;padding-bottom:6px}
  .t.stack td[data-l]::before{content:attr(data-l);font:var(--text-caption);color:var(--muted-foreground);text-align:left;font-family:var(--font-sans)}
  .t.stack td:empty{display:none}
}
.bnote{border:0;padding:0;display:inline-block;margin-left:6px}
.bnote summary{display:inline-flex;min-height:0;font:var(--text-caption);color:var(--faint)}
.bnote p{white-space:normal;max-width:48ch;margin-top:4px}
.sum{display:flex;flex-direction:column;font-size:.8125rem}
.sum > div{display:flex;justify-content:space-between;gap:12px;padding:6px 0;border-bottom:1px solid var(--border)}
.sum > div:last-child{border-bottom:0}
.sum span:first-child{color:var(--muted-foreground)}
.sum i{font-style:normal;color:var(--faint)}
.tiers{display:flex;flex-direction:column;gap:10px;padding-top:10px;border-top:1px solid var(--border)}
.vcard a.btn{white-space:normal;text-align:left;max-width:100%;padding:8px 14px;line-height:1.35}
.strip{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));column-gap:32px;padding:6px 20px}
.strip a{padding:12px 0;display:flex;flex-direction:column;gap:2px;text-decoration:none;font-size:.8125rem}
.strip b{font-size:.875rem}
.strip b .mono{font:400 .75rem/1.4 var(--font-sans);color:var(--muted-foreground);margin-left:4px}
.filters{display:flex;flex-wrap:wrap;gap:8px}
.filters .grow{flex-grow:1}
.fchip{min-height:44px;padding:0 14px;border-radius:999px;border:1px solid var(--input);background:none;font:var(--text-label);font-weight:600;color:var(--muted-foreground)}
.fchip[aria-pressed="true"]{color:var(--foreground);background:var(--muted);border-color:var(--faint)}
.fchip .mono{margin-left:6px;color:var(--faint)}
.allrows{display:none}
.tfilter,.block.rows,.block.sheet{max-width:820px}  /* merchant and amount stay within reach of each other */
@media (max-width:640px){
  .hbars.cap:not(:has(.allrows :checked)) .hbar:nth-of-type(n+9){display:none}
  .hbars.cap:not(:has(.allrows :checked)) .allrows{display:block;padding-top:10px;font:var(--text-label);font-weight:600;color:var(--muted-foreground);cursor:pointer}
}
@media (max-width:640px){
  .filters{flex-wrap:nowrap;overflow-x:auto;scrollbar-width:none;margin-inline:-16px;padding-inline:16px}
  .filters::-webkit-scrollbar{display:none}
  .filters .fchip{flex:0 0 auto;white-space:nowrap}
  .filters .grow{display:none}
}
.tag{font:600 11px/14px var(--font-sans);padding:2px 7px;border-radius:4px;background:var(--muted);color:var(--muted-foreground)}
.now{margin-top:0}
select{font:16px/1.3 var(--font-sans);color:var(--foreground);background:var(--background);border:1px solid var(--input);border-radius:var(--radius);padding:9px 10px;min-height:44px;max-width:100%}
select:focus{border-color:var(--ring);outline:none}
.ovrgrid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
.ovrgrid label{display:flex;flex-direction:column;gap:6px;font:var(--text-label)}
.ovrgrid input,.ovrgrid select{width:100%}
@media (max-width:640px){.ovrgrid{grid-template-columns:minmax(0,1fr)}.tfx .tsel{grid-template-columns:repeat(2,minmax(0,1fr))}}
table.t a.on{font-weight:700;text-decoration-color:var(--ring)}
.hbar .fill.over{background:var(--crit)}
.hbar .cap{position:absolute;top:-3px;bottom:-3px;width:2px;background:var(--foreground)}
.hbar .pace{position:absolute;top:-3px;bottom:-3px;width:2px;background:var(--warn);opacity:.8}
.hbar .v small{font-size:.75rem}
.bgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:10px 28px}
.bline small{display:block;margin-top:2px}
.bline{display:flex;align-items:center;justify-content:space-between;gap:10px;font:var(--text-label)}
.bline input{width:7rem;text-align:right}
.linkbtn{background:none;border:0;padding:0;font:inherit;color:inherit;cursor:pointer;text-decoration:underline dotted;text-underline-offset:3px}
.list{background:var(--card);border:1px solid var(--input);border-radius:var(--radius);overflow:hidden}
.lday{font:var(--text-overline);letter-spacing:.06em;text-transform:uppercase;color:var(--faint);padding:10px 16px 6px;background:var(--muted);border-top:1px solid var(--border)}
.lday:first-child{border-top:0}
.lrow{display:flex;flex-direction:column;gap:3px;padding:11px 16px;border-top:1px solid var(--border);text-decoration:none;color:var(--foreground)}
.lday + .lrow{border-top:0}
.lrow:first-child{border-top:0}
a.lrow:hover{background:color-mix(in srgb,var(--muted) 60%,transparent)}
.lrow.on{background:color-mix(in srgb,var(--primary) 10%,var(--card));box-shadow:inset 3px 0 0 var(--primary)}
.l1{display:flex;justify-content:space-between;align-items:baseline;gap:12px;min-width:0}
.l1 b{font-weight:600;font-size:.9375rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
.l1 .amt{font:500 .875rem/1.2 var(--font-mono);font-variant-numeric:tabular-nums;white-space:nowrap}
.l2{font:var(--text-caption);color:var(--muted-foreground);display:flex;flex-wrap:wrap;gap:2px 6px;align-items:center}
.l2 .sep{color:var(--faint)}
.l3{font:400 .75rem/1.1rem var(--font-mono);color:var(--muted-foreground);overflow-wrap:anywhere}
.lfoot{display:flex;justify-content:space-between;gap:12px;padding:11px 16px;border-top:1px solid var(--input);font-weight:600}
.lfoot .amt{font:600 .875rem/1.2 var(--font-mono)}
.tfilter{display:flex;flex-direction:column;gap:6px;margin:20px 0 16px}
.tsearch{display:flex;gap:8px}
.tsearch input{flex:1;min-width:0;min-height:44px}
.tfx{border-top:0;padding-top:0}
.tfx .tsel{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:8px;padding-top:8px}
.callout{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:12px;padding:16px 18px;border:1px solid var(--input);border-left:3px solid var(--primary);border-radius:var(--radius);background:var(--card)}
.callout p{max-width:60ch}
.datagrid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:28px 40px;align-items:start}
@media (max-width:640px){.datagrid{grid-template-columns:minmax(0,1fr)}}
/* the overview (UI U2) */
.today{font:var(--text-label);margin-bottom:-12px}
.ov-top{align-items:stretch}
.hero{display:flex;flex-direction:column;gap:8px;padding:22px 24px;background:var(--card);border:1px solid var(--border);border-radius:var(--radius);margin:0}
.hero .bhead h1{margin:0}
.hero .big{font:500 4rem/1 var(--font-display);letter-spacing:-.03em;font-variant-numeric:tabular-nums;margin-top:4px}
.hero > p{font-size:.9375rem}
.prog{position:relative;height:8px;border-radius:4px;background:var(--track);margin-top:10px}
.prog .fill{position:absolute;left:0;top:0;bottom:0;border-radius:4px;background:var(--primary)}
.prog .fill.over{background:var(--crit)}
.prog .pace{position:absolute;top:-5px;bottom:-5px;width:2px;background:var(--foreground);margin-left:-1px}
.legend{display:flex;justify-content:space-between;gap:12px;font:var(--text-caption)}
h2.h{font:var(--text-heading);letter-spacing:0;text-transform:none;color:var(--foreground)}
.bhead.rule{padding-bottom:8px;border-bottom:1px solid var(--foreground)}
.bhead > .faint{font:var(--text-caption)}
.alerts{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:8px}
.alerts li{padding:11px 14px;border-radius:var(--radius);background:color-mix(in srgb,var(--c) 11%,transparent);border-left:3px solid var(--c);line-height:1.45}
.alerts li.neutral{--c:var(--faint)}
.alerts a{text-decoration:none}
.bills{display:flex;flex-direction:column}
.bills a{display:grid;grid-template-columns:64px minmax(0,1fr) auto;gap:10px;align-items:baseline;padding:11px 0;border-bottom:1px solid var(--border);text-decoration:none}
.bills .d{font:600 .75rem/1 var(--font-sans);letter-spacing:.06em;text-transform:uppercase;color:var(--primary)}
.bills .mono{font-size:.875rem}
a.more{font:var(--text-label);color:var(--muted-foreground);text-decoration:none;align-self:flex-start}
.cats{display:flex;flex-direction:column;gap:14px;padding-top:4px}
.cat{display:flex;flex-direction:column;gap:6px;text-decoration:none}
.cat .l1{font-size:.9375rem}
.cat .mono{font-size:.8125rem}
.thin{display:block;height:4px;border-radius:2px;background:var(--track)}
.thin i{display:block;height:4px;border-radius:2px;background:var(--primary)}
.thin i.over{background:var(--crit)}
.list.flat{background:none;border:0;border-radius:0}
.list.flat .lrow{padding-inline:0}
.key{white-space:nowrap}
.key i{display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:4px}
.tiles.slim{grid-template-columns:minmax(0,1fr);gap:10px}
.tiles.slim .tile{padding:14px 16px}
.tiles.slim .big{font-size:1.75rem;line-height:2rem}
.acct{display:grid;grid-template-columns:minmax(0,1.3fr) repeat(2,minmax(0,1fr));background:var(--card);border:1px solid var(--input);border-radius:var(--radius);overflow:hidden}
.acct .tile{border:0;border-radius:0;background:none;padding:14px 18px}
.acct .tile + .tile{border-left:1px solid var(--border)}
.acct .tile .big{font-size:1.5rem;line-height:2rem}
.acct .tile:first-child .big{font-size:2rem;line-height:2.5rem}
.acct .muted{font-size:.8125rem}
@media (max-width:640px){
  .hero{padding:18px}
  .hero .big{font-size:3.25rem}
  .acct{grid-template-columns:repeat(2,minmax(0,1fr))}
  .acct .tile{padding:12px 14px}
  .acct .tile:first-child{grid-column:1 / -1;border-bottom:1px solid var(--border)}
  .acct .tile:nth-child(2){border-left:0}
  .acct .tile .big{font-size:1.25rem;line-height:1.75rem}
  .acct .muted{font-size:.75rem;line-height:1.05rem}
  /* the phone's overview: what's used at a till first, the verdicts left to their tab */
  .ovc{display:contents}
  .ovc > span:empty,.ov-worth,.ov-flow .chart{display:none}
  main:has(> .ov-top) .ov-hero{order:1} main:has(> .ov-top) .ov-look{order:2} main:has(> .ov-top) .ov-swipe{order:3}
  main:has(> .ov-top) .ov-acct{order:4} main:has(> .ov-top) .ov-coming{order:5} main:has(> .ov-top) .ov-where{order:6}
  main:has(> .ov-top) .ov-recent{order:7} main:has(> .ov-top) .ov-flow{order:8} main:has(> .ov-top) .ov-links{order:9}
  .ov-recent .lrow:nth-child(n+4){display:none}
}
/* charts and the phone sheet (UI U3) */
.chart{display:flex;flex-direction:column;gap:6px}
.plot{position:relative;display:flex;align-items:flex-end;gap:6px;padding-top:6px}
.plot .c{flex:1;min-width:0;height:100%;display:flex;align-items:flex-end;justify-content:center;gap:2px;text-decoration:none;border-radius:4px}
.plot a.c:hover{background:var(--muted)}
.plot .c i{display:block;flex:0 1 16px;min-height:1px;border-radius:2px 2px 0 0}
.plot .c.part i{opacity:.5}
.plot .c.on{background:color-mix(in srgb,var(--primary) 9%,transparent)}
.chart i.in,.key i.in{background:var(--primary)}
.chart i.out,.key i.out{background:var(--input)}
.chart i.amt{background:var(--primary)}
.chart i.over{background:var(--crit)}
.plot.nw .c{position:relative}
.plot.nw .c i{position:absolute;left:50%;transform:translateX(-50%);width:16px;max-width:70%;border-radius:2px}
.plot .zero{position:absolute;left:0;right:0;height:0;border-top:1px solid var(--faint);pointer-events:none}
.plot .ref{position:absolute;left:0;right:0;height:0;border-top:1.5px dashed var(--foreground);opacity:.55;pointer-events:none}
.plot .ref span{position:absolute;right:0;bottom:3px;font:var(--text-caption);background:var(--background);padding:0 4px}
.xl{display:flex;gap:6px}
.xl span{flex:1;min-width:0;text-align:center;font:var(--text-caption);color:var(--faint)}
.xl span.on{color:var(--foreground);font-weight:600}
.block > .bhead > .ov{font:var(--text-heading);letter-spacing:0;text-transform:none;color:var(--foreground)}
.block > .bhead{padding-bottom:8px;border-bottom:1px solid var(--foreground)}
input[type=search],input[type=text],input:not([type]){font-family:var(--font-sans)}
.scrim{display:none}
@media (max-width:640px){
  .plot,.xl{gap:2px}
  .xl span{font-size:10px}
  .scrim{display:block;position:fixed;inset:0;z-index:30;background:rgba(0,0,0,.4)}
  .sheet{position:fixed;left:0;right:0;bottom:0;z-index:31;max-height:86vh;overflow-y:auto;margin:0;padding:10px 16px calc(20px + env(safe-area-inset-bottom));background:var(--background);border-radius:16px 16px 0 0;box-shadow:0 -10px 30px rgba(0,0,0,.25)}
  .sheet::before{content:"";display:block;width:40px;height:4px;border-radius:2px;background:var(--input);margin:0 auto 6px}
}
/* Worth it? and To do (UI U4) */
.nw{white-space:nowrap}
.t tbody tr:hover td{background:color-mix(in srgb,var(--muted) 50%,transparent)}
.wcard .amt b,.vcard .net{color:var(--foreground)}
.vcard{gap:10px}
.vcard h3{font-size:1.375rem;line-height:1.75rem}
.now{border-left-color:var(--warn)}
.now .days{color:var(--warn)}
@media (max-width:640px){
  .t th,.t td{padding:8px 9px}
  .t td{font-size:.8125rem}
  .t .n{font-size:.75rem}
  .swipe > div{grid-template-columns:minmax(0,1fr) auto 44px;gap:10px}
}
.tabs,.sectabs{display:none}
@media (max-width:640px){
  body{padding-bottom:96px}
  .side .meta,.side .links{display:none}
  .side{padding-bottom:12px}
  .sectabs{display:flex;gap:2px;padding:3px;margin-bottom:-8px;background:var(--muted);border-radius:10px;overflow-x:auto;scrollbar-width:none}
  .sectabs a{flex:1 0 auto;text-align:center;white-space:nowrap;padding:8px 12px;border-radius:8px;font:var(--text-label);font-weight:600;color:var(--muted-foreground);text-decoration:none}
  .sectabs a.on{background:var(--card);color:var(--foreground);box-shadow:0 1px 2px rgba(0,0,0,.12)}
  .tiles.pair{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
  .pair .tile .big{font-size:1.25rem;line-height:1.75rem}
  .pair .tile .muted,.pair .tile .foot{font-size:.75rem;line-height:1.05rem}
  .tabs{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));position:fixed;left:0;right:0;bottom:0;z-index:10;background:color-mix(in srgb,var(--background) 92%,transparent);-webkit-backdrop-filter:blur(12px);backdrop-filter:blur(12px);border-top:1px solid var(--border);padding-bottom:env(safe-area-inset-bottom)}
  .tabs a{display:flex;flex-direction:column;align-items:center;gap:4px;padding:10px 0 12px;min-height:44px;font:600 11px/14px var(--font-sans);color:var(--faint);text-decoration:none}
  .tabs a.on{color:var(--primary)}
  .row{grid-template-columns:minmax(0,1fr) auto}
  .row .btn{display:none}
  .hbar{grid-template-columns:minmax(0,1fr) 56px}
  .hbar .track{grid-column:1/-1;grid-row:2}
  .tile{padding:12px 14px}
}
"""

JS = """
(function(){
  var LO=+document.body.dataset.lo, HI=+document.body.dataset.hi, span=(HI-LO)||1;
  function pct(v){return (v-LO)/span*100;}
  function money(v){var r=Math.round(v);return r===0?'$0':(r<0?'−$':'+$')+Math.abs(r).toLocaleString('en-US');}
  function usd(v){var r=Math.round(v);return (r<0?'−$':'$')+Math.abs(r).toLocaleString('en-US');}
  function el(tag,cls){var e=document.createElement(tag);e.className=cls;return e;}
  function num(e){if(!e)return null;var v=parseFloat(e.value);return isFinite(v)?v:null;}

  document.querySelectorAll('.bar').forEach(function(b){
    var lo=+b.dataset.lo, base=+b.dataset.base, hi=+b.dataset.hi;
    b.appendChild(el('span','axis'));
    var z=el('span','zero');z.style.left=pct(0)+'%';b.appendChild(z);
    var r=el('span','range');r.style.left=pct(lo)+'%';r.style.width=Math.max(0.8,pct(hi)-pct(lo))+'%';b.appendChild(r);
    var m=el('span','mark');m.style.left=pct(base)+'%';b.appendChild(m);
    if(hi!==lo){
      var l1=el('span','lab lo');l1.textContent=money(lo);l1.style.left=pct(lo)+'%';b.appendChild(l1);
      var l2=el('span','lab hi');l2.textContent=money(hi);l2.style.left=pct(hi)+'%';b.appendChild(l2);
    }
    b.setAttribute('role','img');b.setAttribute('aria-label','a year: low '+money(lo)+', best guess '+money(base)+', high '+money(hi));
  });

  function openItem(q){q.classList.add('open');var h=q.querySelector('.qhead');if(h)h.setAttribute('aria-expanded','true');}
  document.querySelectorAll('.qhead').forEach(function(h){
    h.addEventListener('click',function(){var o=h.parentElement.classList.toggle('open');h.setAttribute('aria-expanded',o?'true':'false');});
  });
  function fromHash(){
    var t=location.hash&&document.getElementById(decodeURIComponent(location.hash.slice(1)));
    if(!t)return;
    var sd=t.closest('.settled');if(sd)sd.hidden=false;
    var q=t.classList.contains('q')?t:t.querySelector('.q');
    if(q){openItem(q);}
    var d=t.closest('details');if(d)d.open=true;
    t.scrollIntoView({block:'start'});
  }
  window.addEventListener('hashchange',fromHash);fromHash();
  document.querySelectorAll('a.goto').forEach(function(a){a.addEventListener('click',function(){if(location.hash===a.getAttribute('href'))fromHash();});});

  var fchips=document.querySelectorAll('.fchip[data-tag]');
  fchips.forEach(function(c){c.addEventListener('click',function(){
    fchips.forEach(function(o){o.setAttribute('aria-pressed','false');});c.setAttribute('aria-pressed','true');
    var t=c.dataset.tag;
    document.querySelectorAll('[data-tags]').forEach(function(e){e.hidden=!!t&&(' '+e.dataset.tags+' ').indexOf(' '+t+' ')<0;});
  });});
  var shown=document.querySelector('.fchip[data-settled]');
  if(shown)shown.addEventListener('click',function(){
    var on=shown.getAttribute('aria-pressed')!=='true';shown.setAttribute('aria-pressed',on?'true':'false');
    document.querySelectorAll('.settled').forEach(function(e){e.hidden=!on;});
  });

  function r6(x){return Math.round(x*1e6)/1e6;}
  function perkNote(b){
    var n=b.querySelector('[data-key=note]');
    if(n&&n.value.trim())return n.value.trim();
    var lo=num(b.querySelector('[data-key=low]')),base=num(b.querySelector('[data-key=base]')),hi=num(b.querySelector('[data-key=high]'));
    var amt=(lo===base&&hi===base)?usd(base)+' a year':usd(lo)+' / '+usd(base)+' / '+usd(hi)+' a year (low / best guess / high)';
    return (b.dataset.say?b.dataset.say+', ':'')+amt;
  }
  function writes(f){
    var out=[],kind=f.dataset.kind;
    if(kind==='perks'){
      f.querySelectorAll('.perk').forEach(function(b){
        if(!b.dataset.touched)return;
        ['low','base','high'].forEach(function(k){
          var e=b.querySelector('[data-key='+k+']'),v=num(e);
          if(v!==null)out.push({file:'assumptions',section:e.dataset.section,key:k,value:v});
        });
        out.push({file:'assumptions',section:b.dataset.section,key:'note',value:perkNote(b)});
      });
      return out;
    }
    if(kind==='cpp'){
      var c=num(f.querySelector('[name=cpp]')),s=f.dataset.section,m=JSON.parse(f.dataset.mults||'{}');
      if(c!==null){
        out.push({file:'rules',section:s,key:'default_rate',value:r6(c/100)});
        Object.keys(m).forEach(function(k){out.push({file:'rules',section:s,key:'rates',subkey:k,value:r6(c*m[k]/100)});});
      }
    }
    f.querySelectorAll('[data-key]').forEach(function(e){
      var v;
      if(e.dataset.type==='true'){v=true;}
      else if(e.dataset.type==='bool'){if(!e.checked)return;v=true;}
      else if(e.dataset.type==='number'){v=num(e);if(v===null)return;}
      else{v=e.value.trim();if(v==='')return;}
      var w={file:e.dataset.file,section:e.dataset.section,key:e.dataset.key,value:v};
      if(e.dataset.subkey)w.subkey=e.dataset.subkey;
      out.push(w);
    });
    if(f.dataset.audit)out.forEach(function(w){w.note=f.dataset.audit;});
    return out;
  }
  function update(f){
    var ws=writes(f),pre=(f.parentElement||f).querySelector('pre.preview');
    if(pre){
      var last=null,lines=[];
      ws.forEach(function(w){if(w.section!==last){lines.push('['+w.section+']');last=w.section;}lines.push((w.subkey?w.key+'.'+w.subkey:w.key)+' = '+JSON.stringify(w.value));});
      pre.textContent=lines.join('\\n')||'(nothing yet — pick or type an answer)';
    }
    var res=f.querySelector('.netv');
    if(res){
      var n=+f.dataset.net0;
      f.querySelectorAll('.perk').forEach(function(b){var v=num(b.querySelector('[data-key=base]'));if(v!==null)n+=v-(+b.dataset.base0);});
      var rl=f.querySelector('.resl');if(rl)rl.textContent=(ws.length?'With your answer, ':'Right now ')+rl.dataset.m+' is';
      res.textContent=(n>=0?'keep':'drop')+' · '+money(n)+' a year';
      res.className='netv '+(n>=0?'good':'crit');
    }
    var btn=f.querySelector('button[type=submit]');
    if(btn&&f.dataset.kind==='perks')btn.disabled=!ws.length;
  }
  var cur=document.querySelector('.side .links a.on');
  if(cur&&cur.parentElement.scrollWidth>cur.parentElement.clientWidth)cur.parentElement.scrollLeft=cur.offsetLeft-cur.parentElement.clientWidth/2+cur.offsetWidth/2;
  document.querySelectorAll('form.tfilter select').forEach(function(e){e.addEventListener('change',function(){e.form.submit();});});
  document.querySelectorAll('form.ovr').forEach(function(f){
    var st=f.querySelector('.status');
    function send(body){
      st.className='status';st.textContent='Saving…';
      fetch('/override',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
        .then(function(r){return r.json().then(function(j){return [r.ok,j];});})
        .then(function(res){
          if(!res[0]){st.className='status err';st.textContent=res[1].error||'Refused.';return;}
          if(!res[1].changed){st.className='status';st.textContent='Nothing changed.';return;}
          st.className='status ok';st.textContent='Saved. Reloading…';setTimeout(function(){location.reload();},700);
        })
        .catch(function(err){st.className='status err';st.textContent='Failed: '+err;});
    }
    f.addEventListener('submit',function(e){
      e.preventDefault();
      var scope=(e.submitter&&e.submitter.dataset.scope)||'row';
      var body={id:f.dataset.id,scope:scope},fam=f.elements.family.value,c=f.elements.category.value.trim().toLowerCase();
      if(fam!==f.dataset.fam)body.family=fam;
      if(c&&(c!==f.dataset.cat||scope==='merchant'))body.category=c;  // for the merchant, the category shown is the point
      if(!body.family&&!body.category){st.className='status';st.textContent='Change the family or the category first.';return;}
      send(body);
    });
    f.querySelectorAll('[data-clear]').forEach(function(b){b.addEventListener('click',function(){send({id:f.dataset.id,scope:b.dataset.clear,clear:true});});});
  });
  document.querySelectorAll('[data-fill-avg]').forEach(function(b){b.addEventListener('click',function(){
    var f=b.closest('form');
    var all=b.dataset.fillAvg==='all';
    f.querySelectorAll('input[data-avg]').forEach(function(e){if(all||!(+e.value))e.value=e.dataset.avg;});
    f.dispatchEvent(new Event('input'));
  });});
  document.querySelectorAll('[data-use-avg]').forEach(function(b){b.addEventListener('click',function(e){
    e.preventDefault();  // it sits in the input's label
    var i=b.closest('label').querySelector('input[data-avg]');
    i.value=i.dataset.avg;i.form.dispatchEvent(new Event('input'));
  });});
  document.querySelectorAll('form.rec').forEach(function(f){
    f.querySelectorAll('.perk').forEach(function(b){
      var ans=b.querySelector('[name=answer]'),levels=b.querySelectorAll('.range3 input');
      function unpress(){b.querySelectorAll('.choice').forEach(function(o){o.setAttribute('aria-pressed','false');});}
      function fill(v){levels.forEach(function(e){e.value=v===null?e.defaultValue:v;});}
      b.querySelectorAll('.choice').forEach(function(c){
        c.addEventListener('click',function(){
          unpress();c.setAttribute('aria-pressed','true');
          b.dataset.say=c.dataset.say;b.dataset.touched='1';
          if(ans)ans.value=c.dataset.fill;
          fill(c.dataset.fill);update(f);
        });
      });
      if(ans)ans.addEventListener('input',function(){
        unpress();b.dataset.say='';
        if(ans.value===''){b.dataset.touched='';fill(null);}else{b.dataset.touched='1';fill(ans.value);}
        update(f);
      });
      levels.forEach(function(e){e.addEventListener('input',function(){
        unpress();b.dataset.say='';b.dataset.touched='1';
        if(ans)ans.value='';
        update(f);
      });});
      var note=b.querySelector('[data-key=note]');
      if(note)note.addEventListener('input',function(){if(note.value.trim())b.dataset.touched='1';update(f);});
    });
    f.addEventListener('input',function(){update(f);});
    f.addEventListener('change',function(){update(f);});
    f.querySelectorAll('.seg .choice').forEach(function(c){
      c.addEventListener('click',function(){
        f.querySelectorAll('.seg .choice').forEach(function(o){o.setAttribute('aria-pressed','false');});
        c.setAttribute('aria-pressed','true');
        f.querySelector('input[type=hidden][data-key]').value=c.dataset.fill;update(f);
      });
    });
    update(f);
    f.addEventListener('submit',function(e){
      e.preventDefault();
      var st=f.querySelector('.status'),btn=f.querySelector('button[type=submit]'),ws=writes(f);
      if(!ws.length){st.className='status err';st.textContent='Pick or type an answer first.';return;}
      st.className='status';st.textContent='Saving…';btn.disabled=true;
      fetch('/record',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(ws)})
        .then(function(r){return r.json().then(function(j){return [r.ok,j];});})
        .then(function(res){
          var ok=res[0],j=res[1];btn.disabled=false;
          if(!ok){st.className='status err';st.textContent=j.error||'Refused.';return;}
          if(!j.recorded.length){st.className='status';st.textContent='Nothing changed: the file already says this.';return;}
          st.className='status ok';st.textContent='Saved. Reloading…';
          setTimeout(function(){location.hash='';location.reload();},900);
        })
        .catch(function(err){btn.disabled=false;st.className='status err';st.textContent='Failed: '+err;});
    });
  });
})();
"""


def md(text: str) -> str:
    """Escape, then the little markdown the wiki rows use: bold, code, links.
    Doc paths become links to /docs/."""
    s = escape(text)
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)

    def code(m):
        inner = m.group(1)
        return f"<a href='/{inner}'><code>{inner}</code></a>" if DOC_REF.fullmatch(inner) else f"<code>{inner}</code>"

    return re.sub(r"`([^`]+)`", code, s)


@dataclass
class Item:
    """One thing on the page, in words: what is asked and what it changes."""

    key: str
    group: str  # now | ask | check | quiet | wiki
    title: str
    why: str
    how: str = ""
    stake: str = ""
    weight: float = 0.0  # dollars a year, for ordering within a group
    membership: str | None = None
    form: str = ""  # rendered form, empty when nothing on the page writes it
    saved: str = ""  # rendered "What gets saved"
    d: Decision | None = None


def _verdict_word(net: float) -> str:
    return "keep" if round(net) >= 0 else "drop"


def _perk_why(p: dict) -> str:
    m, label, net, base = p["mlabel"], p["label"], p["net"], p["base"]
    n_lo, n_hi = net - base + p["low"], net - base + p["high"]
    need = base - net  # what the perk must be worth for the net to reach zero
    if n_lo >= 0:
        return f"{m} is a keep whatever you answer ({signed(n_lo)} to {signed(n_hi)} a year). The answer only sharpens the number."
    if n_hi < 0:
        return f"{m} is a drop whatever you answer ({signed(n_lo)} to {signed(n_hi)} a year)."
    if net >= 0:
        return f"{m} is a keep at {signed(net)} a year, counting {label} at {usd(base)}. If it is worth less than {usd(need)} a year, {m} becomes a drop."
    return f"{m} is a drop at {signed(net)} a year. If {label} is worth {usd(need)} or more a year to you, it becomes a keep."


def _saved(d: Decision, extra: str = "") -> str:
    src = d.source
    keys = ", ".join(src.get("keys", []))
    explain = "".join(f"<li>{md(x)}</li>" for x in d.explain)
    where = f"{escape(src['file'])} [{escape(src['section'])}]" + (f" {escape(keys)}" if keys else "")
    return (
        "<details class=saved><summary>What gets saved</summary>"
        f"<p>Listed as: {escape(d.title)}. {escape(d.sub)}</p>"
        f"<ul>{explain}</ul>"
        f"<p>Saving writes {where}, keeps every comment, and adds a line to <code>data/board-decisions.jsonl</code>. Nothing is committed.</p>"
        f"{extra}<pre class=preview></pre></details>"
    )


def _range_field(sec: str, lvl: str, value: float) -> str:
    name = {"low": "low", "base": "best guess", "high": "high"}[lvl]
    return f"<label>{name}<input type=number inputmode=decimal step=any data-file=assumptions data-section='{sec}' data-key={lvl} data-type=number value='{value:g}'></label>"


def _perk_block(p: dict, group: bool) -> str:
    sec = escape(p["section"])
    ask = p.get("ask") or (f"What is {p['label']} worth to you a year?" if not group else f"{p['label'].capitalize()}: what does it save you a year?")
    parts = [f"<div class=perk data-section='{sec}' data-base0='{p['base']:g}'>"]
    if group:
        parts.append(f"<p class=ask>{escape(ask)}" + (f"<small>{escape(p['how'])}</small>" if p.get("how") else "") + "</p>")
    choices = [(p[f"{lvl}_if"], p[lvl]) for lvl in ("low", "measured", "high") if p.get(f"{lvl}_if") and lvl in p]
    if choices:
        parts.append("<div class=choices role=group aria-label='answers'>")
        for say, v in choices:
            after = p["net"] - p["base"] + v
            parts.append(f"<button type=button class=choice data-fill='{v:g}' data-say='{escape(say)}' aria-pressed=false>{escape(say)}<small>{usd(v)} a year → {_verdict_word(after)} {signed(after)}</small></button>")
        parts.append("</div>")
    parts.append(
        f"<label class=amount>{'Or your own number' if choices else 'Your number'}"
        f"<input name=answer type=number inputmode=decimal step=any placeholder='{p['base']:g}' aria-label='dollars a year'>$ a year</label>"
    )
    parts.append(
        "<details><summary>Not sure? Give a range, or add a note</summary>"
        f"<div class=range3>{''.join(_range_field(sec, lvl, p[lvl]) for lvl in LEVELS)}</div>"
        f"<p class=hint>Now in the file: {num(p['low'])} / {num(p['base'])} / {num(p['high'])}. The best guess is what the verdict uses; low and high draw the bar.</p>"
        f"<textarea data-file=assumptions data-section='{sec}' data-key=note data-type=string placeholder='What you based it on (optional; your answer is noted either way)'></textarea>"
        "</details>"
    )
    parts.append("</div>")
    return "".join(parts)


def _perk_form(perks: list[dict], net: float, mlabel: str) -> str:
    group = len(perks) > 1
    return (
        f"<form class=rec data-kind=perks data-net0='{net}'>"
        + "".join(_perk_block(p, group) for p in perks)
        + f"<div class=result><span class=resl data-m='{escape(mlabel)}'>Right now {escape(mlabel)} is</span><b class=netv></b></div>"
        + "<div class=actions><button class='btn primary' type=submit disabled>Save answer</button><span class=status role=status></span></div>"
        + "</form>"
    )


def _simple_form(inner: str, button: str, audit: str = "", kind: str = "", attrs: str = "") -> str:
    if kind:
        attrs += f" data-kind={kind}"
    if audit:
        attrs += f" data-audit='{escape(audit)}'"
    return (
        f"<form class=rec{attrs}>{inner}"
        f"<div class=actions><button class='btn primary' type=submit>{escape(button)}</button><span class=status role=status></span></div></form>"
    )


def _cpp_form(section: str, cpp: float, mults: dict[str, float], labels: dict[str, str]) -> str:
    """The baseline's ¢/pt: every rate on the card rescales with it."""
    rates = ", ".join(f"{labels.get(f, f)} {m:g}x" for f, m in mults.items())
    inner = (
        f"<label class=amount>¢ a point<input type=number inputmode=decimal step=0.01 min=0 name=cpp value='{cpp:g}'></label>"
        f"<p class=hint>Every rate on the card rescales with it{': ' + escape(rates) if rates else ''}.</p>"
        f"<label class=amount><input type=checkbox data-file=rules data-section='{escape(section)}' data-key=verified data-type=bool style='width:22px;height:22px'> The multipliers match the card's terms (sets verified = true)</label>"
    )
    return _simple_form(inner, "Save ¢ a point", kind="cpp", attrs=f" data-section='{escape(section)}' data-mults='{escape(json.dumps(mults))}'")


def _verified_inputs(section: str, card: dict, today: date) -> str:
    """verified = true, and today's date when the card carries verified_on
    (the board never adds a key, so a card without one gets only the flag)."""
    out = f"<input type=hidden data-file=rules data-section='{escape(section)}' data-key=verified data-type=true>"
    if "verified_on" in card:
        out += f"<input type=hidden data-file=rules data-section='{escape(section)}' data-key=verified_on data-type=string value='{today.isoformat()}'>"
    return out


def plan(s: State) -> list[Item]:
    """Sort every decision into a page group and put it into words."""
    rules, today = s.rules, s.today
    verdicts = by_key(model.evaluate(rules, s.assumptions, s.spend, s.window.annualize))
    rows = {r.membership: r for r in s.verdicts}
    cards, mems = rules.get("cards", {}), rules.get("memberships", {})
    labels = _labels(rules)
    bkey = rules["held"]["baseline_card"]
    blabel = cards[bkey].get("label", bkey)
    items: list[Item] = []
    for d in s.decisions:
        k = d.kind
        mlabel = mems.get(d.membership, {}).get("label", d.membership or "")
        if k == "external":
            items.append(Item(d.key, "now" if d.due else "wiki", d.title, md(d.data.get("detail", "")), d=d, stake=escape(d.data.get("heading", ""))))
        elif k in ("placeholder", "choice"):
            p = d.data
            how = p.get("how") or ("The value in the file is a placeholder. " if k == "placeholder" else "") + f"Count what you would otherwise pay without {mlabel}, not the list price. Note in the file: {p['note']}"
            group = "ask" if abs(d.swing or 0) >= WASH else "quiet"
            items.append(Item(d.key, group, p.get("ask") or f"What is {p['label']} worth to you a year?", escape(_perk_why(p)), escape(how), f"up to {usd(abs(d.swing or 0))}/yr", abs(d.swing or 0), d.membership, _perk_form([p], p["net"], mlabel), _saved(d), d))
        elif k == "defaults":
            stuck = [m for m, r in rows.items() if "can't tell" in r.verdict]
            rest = [p for p in d.data["perks"] if p["membership"] not in stuck]
            for m in stuck:
                ps = [p for p in d.data["perks"] if p["membership"] == m]
                if not ps:
                    continue
                net, ml, fee = ps[0]["net"], ps[0]["mlabel"], ps[0]["fee"]
                why = f"Nothing is counted for {ml} yet except the {usd(fee)} fee, so it nets {signed(net)} a year and the verdict can't tell. If what it saves you comes to {usd(-net)} or more a year, it's a keep; less, a drop."
                how = "Answer the ones that apply. Count what you would pay without the membership, not list prices. An answer of $0 is an answer: it settles the verdict as a drop."
                sub = Decision(f"ask:{m}", f"{ml} perks at zero", m, "defaults", None, d.explain, {"file": "assumptions.toml", "section": ", ".join(p["section"] for p in ps), "keys": list(LEVELS) + ["note"]}, "perks", sub=", ".join(p["label"] for p in ps))
                items.append(Item(sub.key, "ask", f"Does {ml} save you {usd(-net)} or more a year?", escape(why), escape(how), f"{usd(-net)}/yr", -net, m, _perk_form(ps, net, ml), _saved(sub), sub))
            if rest:
                names = ", ".join(f"{p['mlabel']} {p['label']}" for p in rest)
                forms = "".join(f"<details><summary>Put a value on {escape(p['mlabel'])} {escape(p['label'])}</summary>{_perk_form([p], p['net'], p['mlabel'])}</details>" for p in rest)
                items.append(Item(d.key, "quiet", "Perks counted as $0", escape(f"{names}. Change one only if you would pay for it without the membership."), form=forms, d=d))
        elif k == "verify":
            ckey = d.data["section"].split(".", 1)[1]
            card = cards[ckey]
            clabel = card.get("label", ckey)
            members = [m for m, mm in mems.items() if mm.get("card") == ckey]
            attr = [(mems[m].get("label", m), verdicts[headline_key(rules, m, with_card=True)].attributable) for m in members]
            why = f"{usd(d.swing or 0)} a year of cash back over the {blabel} rides on these rates" + "".join(f"; {usd(a)} of it counts toward keeping {ml}" for ml, a in attr if round(a)) + "."
            how = f"On file: {rate_text(card, labels)}. Compare with the card's rewards page or the card issuer's app."
            form = _simple_form(
                _verified_inputs(d.data["section"], card, today)
                + f"<p class=hint>If a rate is different, change it by hand in rules.toml [{escape(d.data['section'])}]; this button only records that you checked.</p>",
                "Yes, they match",
                audit=f"checked the terms on {today.isoformat()}",
            )
            group = "check" if abs(d.swing or 0) >= WASH else "quiet"
            items.append(Item(d.key, group, f"Does the {clabel} still pay these rates?", escape(why), escape(how), f"{usd(d.swing or 0)}/yr", abs(d.swing or 0), d.membership, form, _saved(d), d))
        elif k == "baseline":
            p = d.data
            form = _cpp_form(p["section"], p["cpp"], p["mults"], labels)
            why = f"Half a cent either way moves a verdict by at most {usd(d.swing or 0)} a year."
            if abs(d.swing or 0) >= WASH:
                items.append(Item(d.key, "check", f"What is a {p['label']} point worth to you?", escape(f"Set at {p['cpp']:g}¢. {why} Every card's edge is measured against this card."), "", f"{usd(d.swing or 0)} per ½¢", abs(d.swing or 0), None, form, _saved(d), d))
            else:
                items.append(Item(d.key, "quiet", f"{p['label']} points at {p['cpp']:g}¢", escape(f"{why} Not worth revisiting unless how you redeem points changes."), form=f"<details><summary>Change it</summary>{form}</details>", d=d))
        elif k == "apply":
            ckey = d.key.split(":")[1]
            clabel = cards[ckey].get("label", ckey)
            w = verdicts[headline_key(rules, d.membership, with_card=True)]
            wo = verdicts[headline_key(rules, d.membership, with_card=False)]
            if (d.swing or 0) < WASH:
                items.append(Item(d.key, "quiet", f"Skip the {clabel}", escape(f"At the rates on file it would earn {usd(d.swing or 0)} a year more than the {blabel} on your spend." + (" Those rates are unverified recollections." if d.data.get("unverified") else "")), d=d))
            else:
                form = ""
                if d.data.get("unverified"):
                    form = _simple_form(_verified_inputs(d.data["section"], cards[ckey], today), "I checked its rates", audit=f"checked the terms on {today.isoformat()}")
                why = f"It would earn {usd(d.swing or 0)} a year more than the {blabel}. {mlabel} nets {signed(w.net['base'])} with it, {signed(wo.net['base'])} without."
                items.append(Item(d.key, "ask", f"Apply for the {clabel}?", escape(why), escape("Applying happens outside this page. Once you hold it, add it to [held].cards in rules.toml."), f"{usd(d.swing or 0)}/yr", d.swing or 0, d.membership, form, _saved(d), d))
        elif k == "tier":
            p = d.data
            held = p["labels"][p["held"]]
            btns = "".join(
                f"<button type=button class=choice data-fill='{escape(t)}' aria-pressed={'true' if t == p['held'] else 'false'}>{escape(p['labels'][t])}<small>{'what you have' if t == p['held'] else signed(n) + ' a year vs now'}</small></button>"
                for t, n in p["nets"].items()
            )
            inner = f"<div class='choices seg' role=group aria-label=tier>{btns}</div><input type=hidden data-file=rules data-section=held data-key={escape(p['key'])} data-type=string value='{escape(p['held'])}'>"
            if p.get("refund"):
                form = f"<details><summary>Upgraded? Record it</summary>{_simple_form(inner, 'Save tier')}{_saved(d)}</details>"
                items.append(Item(d.key, "quiet", f"{mlabel}: take {p['labels'][p['best']]} at renewal", escape(d.sub), form=form, d=d))
            elif d.settled:
                items.append(Item(d.key, "quiet", f"{mlabel}: stay on {held}", escape(d.sub), d=d))
            else:
                items.append(Item(d.key, "ask", f"Switch {mlabel} from {held}?", escape(" ".join([d.sub + "."] + d.explain)), "", f"{signed(d.swing or 0)}/yr", abs(d.swing or 0), d.membership, _simple_form(inner, "Save tier"), _saved(d), d))
        elif k == "fee":
            p = d.data
            inner = f"<label class=amount>List fee<input type=number inputmode=decimal step=any min=0 data-file=rules data-section='{escape(p['section'])}' data-key=fee data-type=number value='{p['value']:g}'>$ a year</label><p class=hint>If the charge is something else, leave the fee and tighten fee_patterns in rules.toml instead.</p>"
            items.append(Item(d.key, "check", f"Did the {mlabel} fee change?", escape(" ".join(d.explain)), "", f"{signed(d.swing or 0)}/yr", abs(d.swing or 0), d.membership, _simple_form(inner, "Update the list fee"), _saved(d), d))
    order = {"now": 0, "ask": 1, "check": 2, "quiet": 3, "wiki": 4}
    return sorted(items, key=lambda i: (order[i.group], -i.weight if i.group in ("ask", "check") else 0))


def item_tags(it: Item) -> list[str]:
    """The To do page's filter tags: the membership, the card a key names, wiki."""
    tags = ["wiki"] if it.group in ("now", "wiki") else []
    if it.membership:
        tags.append(f"m:{it.membership}")
    kind, _, rest = it.key.partition(":")
    if kind in ("verify", "cpp", "apply"):
        tags.append(f"c:{rest.split(':')[0]}")
    return tags


def _tags(it: Item) -> str:
    return f" data-tags='{escape(' '.join(item_tags(it)))}'"


def render_question(n: int, it: Item) -> str:
    how = f"<p class=how><b>How to answer</b>{it.how}</p>" if it.how else ""
    return (
        f"<article class=q id='{escape(it.key)}'{_tags(it)}>"
        f"<button class=qhead type=button aria-expanded=false><span class=qn>{n}</span>"
        f"<span><span class=qt>{escape(it.title)}</span><span class=qwhy>{it.why}</span></span>"
        f"<span class=qside><span class=stake>{it.stake}</span><span class=go>Answer</span></span></button>"
        f"<div class=qbody>{how}{it.form}{it.saved}</div></article>"
    )


def render_plain(it: Item) -> str:
    extra = it.form
    if it.group == "quiet" and it.saved and it.form and not it.form.startswith("<details"):
        extra = f"<details><summary>Answer anyway</summary>{it.form}{it.saved}</details>"
    stake = f"<span class=mono>{it.stake}</span>" if it.stake else ""
    tag = "<span class=tag>to-do</span>" if it.group == "wiki" else ""
    return f"<li id='{escape(it.key)}'{_tags(it)}><div class=t>{escape(it.title)}{stake}{tag}</div><p>{it.why}</p>{extra}</li>"


def render_now(it: Item, today: date) -> str:
    days = (it.d.due - today).days
    return (
        f"<div class=now id='{escape(it.key)}'{_tags(it)}><div><h3>{escape(it.title)}</h3><p>{it.why}</p>"
        f"<p class=hint>When it's done, rewrite the to-do row <code>{escape(it.d.source['keys'][0])}</code>; the page reads it on reload.</p></div>"
        f"<div class=days>{days}<small>day{'s' if days != 1 else ''} to {it.d.due.isoformat()}</small></div></div>"
    )


def _split_verdict(v: str) -> tuple[str, str]:
    """"keep, if X" → ("keep", "if X"); "keep or drop: can't tell" → ("can't tell", "")."""
    if "can't tell" in v:
        return "can't tell", ""
    word, _, rest = v.partition(", ")
    return word, rest


def render_walmart(s: State) -> str:
    fam = s.rules.get("families", {}).get(WALMART_FAMILY)
    if not fam:
        return ""
    label = escape(fam.get("label", WALMART_FAMILY))
    export_yr = s.walmart_export * s.window.annualize
    m = s.walmart_measured
    if s.export is None:
        parts = [f"<div><h2>{label}</h2><dl>", f"<dt>spend</dt><dd>{usd(export_yr)} a year</dd>"]
        if m and s.walmart_feed_span is not None:
            parts += [f"<dt>capture</dt><dd>{usd(m.total)}, {m.first} → {m.last}</dd>",
                      f"<dt>feed, same days</dt><dd>{usd(s.walmart_feed_span)}</dd>"]
        parts.append("</dl><p class=fine>From the card feed, which has every OnePay row."
                     + (" The order capture is a cross-check: the feed runs a little under it where Walmart charged less than the order total."
                        if m else "") + "</p></div>")
        return "".join(parts)
    if not m:
        return (f"<div><h2>{label}</h2><dl><dt>spend</dt><dd class=warn>{usd(export_yr)} a year, a floor</dd></dl>"
                "<p class=fine>From the export only, which drops OnePay purchase rows. Capture Walmart order history "
                "(scripts/walmart-orders-capture.js) into data/ for the real figure.</p></div>")
    what = "order totals" if m.listed else "opened orders' line items, before tax"
    parts = [
        f"<div><h2>{label}</h2><dl>",
        f"<dt>spend</dt><dd>{usd(m.annual)} a year</dd>",
        f"<dt>from</dt><dd>{escape(m.path.name)}</dd>",
        f"<dt>orders</dt><dd>{m.orders}, {m.first} → {m.last}</dd>",
        f"<dt>export had</dt><dd>{usd(export_yr)} a year</dd>",
        (f"<dt>left out</dt><dd>{usd(s.walmart_dropped * s.window.annualize)} of Walmart.com and store rows</dd>" if s.walmart_dropped > 0.5 else "")
        + "</dl>",
        f"<p class=fine>Walmart's order history ({what}), not the export, which drops OnePay purchase rows."
        + (f" {m.days} days scaled to a year: an estimate, not a measurement." if m.days < 365 else "")
        + (" The export's Walmart.com orders and pre-capture store trips are left out: the capture lists those orders, and its scaling stands in for those months." if s.walmart_dropped > 0.5 else "")
        + "</p></div>",
    ]
    return "".join(parts)


def render_costco(s: State) -> str:
    """The receipts check the export: they agree on Costco, so they are shown, not substituted."""
    m = s.costco_measured
    if not m:
        return ""
    fam = s.rules["families"][COSTCO_FAMILY]
    export_yr = s.spend.get(COSTCO_FAMILY, 0.0) * s.window.annualize
    return (
        f"<div><h2>{escape(fam.get('label', COSTCO_FAMILY))} receipts</h2><dl>"
        f"<dt>warehouse</dt><dd>{usd(m.annual)} a year before tax</dd>"
        f"<dt>from</dt><dd>{escape(m.path.name)}</dd>"
        f"<dt>receipts</dt><dd>{m.receipts}, {m.first} → {m.last}</dd>"
        f"<dt>{s.noun} had</dt><dd>{usd(export_yr)} a year with tax</dd></dl>"
        f"<p class=fine>A check on the {s.noun}, which already has Costco right: the verdicts use the {s.noun}'s full year."
        + (f" The receipts cover {m.days} days, scaled to a year." if m.days < 365 else "")
        + "</p></div>"
    )


def render_rail(s: State) -> str:
    w = s.window
    covered = (w.end - max(s.first, w.start)).days + 1
    total = sum(s.spend.values()) or 1.0
    outside = s.spend.get(cat.OTHER, 0.0) / total
    parts = [
        "<div><h2>The data</h2><dl>",
        (f"<dt>export</dt><dd>{escape(s.export.name[:10])}</dd>" if s.export else "<dt>source</dt><dd>Plaid + Apple Card</dd>"),
        f"<dt>window</dt><dd>{w.start} → {w.end}</dd>",
        f"<dt>transactions</dt><dd>{s.n_txns:,}</dd>",
    ]
    if covered < w.days:
        parts.append(f"<dt>covers</dt><dd class=warn>{covered} of {w.days} days</dd>")
    parts.append(f"<dt>outside the families</dt><dd>{outside:.0%} of spend</dd></dl>")
    if covered < w.days:
        parts.append("<p class=fine>Less than a year of data: spend is short, not scaled up.</p>")
    parts.append("</div>")
    parts.append(render_walmart(s))
    parts.append(render_costco(s))

    mis = {(m, t.date, t.amount) for m, t, _ in model.fee_mismatches(s.found, s.rules)}
    parts.append("<div><h2>Fees charged</h2><ul>")
    for mkey, m in s.rules.get("memberships", {}).items():
        label = m.get("label", mkey)
        hits = s.found.get(mkey, [])
        if not hits:
            parts.append(f"<li><i class=neutral></i><span>{escape(label)}<small>no charge in the window</small></span></li>")
            continue
        t = max(hits, key=lambda x: x.date)
        bad = (mkey, t.date, t.amount) in mis
        parts.append(f"<li><i class={'warn' if bad else 'good'}></i><span>{escape(label)} {usd(t.amount)}<small>{t.date}, {'off the list fee' if bad else 'matches the list fee'}</small></span></li>")
    parts.append("</ul></div>")

    parts.append("<div><h2>Saved answers</h2><ul>")
    if s.git is None:
        parts.append("<li><i class=neutral></i><span>rules.toml, assumptions.toml<small>git state unknown</small></span></li>")
    else:
        for name, st in s.git.items():
            dirty = st != "committed"
            parts.append(f"<li><i class={'warn' if dirty else 'good'}></i><span>{escape(name)}<small>{'changed here, not committed yet' if dirty else 'nothing uncommitted'}</small></span></li>")
    parts.append("</ul><p class=fine style='margin-top:10px'>Saving edits the file straight away, comments intact, and logs the change to <code>data/board-decisions.jsonl</code>. A Claude session commits it. Nothing here is a hand-kept list: every question is worked out from rules.toml, assumptions.toml, " + ("the export" if s.export else "the card feed") + " and the to-do file (if given) on each load, and leaves once answered.</p></div>")
    return "".join(parts)


FONTS = "<link rel=preconnect href='https://fonts.gstatic.com' crossorigin><link rel=stylesheet href='https://fonts.googleapis.com/css2?family=Newsreader:opsz,wght@6..72,400;6..72,500;6..72,600&family=IBM+Plex+Sans:ital,wght@0,400;0,500;0,600;0,700;1,400&family=IBM+Plex+Mono:wght@400;500;600&display=swap'>"


LOGO = (  # docs/design/logo/final/penny-wordmark.svg; the word takes the text colour
    "<svg class=logo viewBox='0 0 714.64 256' role=img aria-label=penny><g transform='translate(7.26,0)'>"
    "<path fill=currentColor d='M46.08 77.46V89.04L47.29 91.98V198.96Q47.29 203.21 48.39 205.07Q49.5 206.93 51.52 207.74L56.37 208.78Q58.6 209.86 59.82 211.45Q61.04 213.04 61.04 215.41Q61.04 218.37 59.15 220.04Q57.26 221.72 53.84 221.72H13.56Q10.23 221.72 8.3 220.05Q6.36 218.38 6.36 215.41Q6.36 213.04 7.58 211.5Q8.8 209.95 11.03 208.78L14.19 207.74Q16.26 206.85 17.34 205.03Q18.43 203.21 18.43 198.96V100.34Q18.43 96.54 17.32 95.01Q16.22 93.47 14.17 92.84L9.2 92.37Q7.14 91.48 5.94 90.13Q4.74 88.78 4.74 86.64Q4.74 84.26 6.27 82.62Q7.79 80.98 11.07 79.67L27.43 73.32Q31.25 71.77 34.03 70.98Q36.82 70.19 39.39 70.19Q42.52 70.19 44.3 72.19Q46.08 74.19 46.08 77.46ZM41 105.51 36.13 98.04Q44.3 84.74 55.33 77.19Q66.36 69.64 79.62 69.64Q92.24 69.64 102.04 76.03Q111.84 82.42 117.42 93.66Q123 104.91 123 119.66Q123 136.46 116.32 148.82Q109.64 161.18 98.25 167.91Q86.86 174.65 72.66 174.65Q60.06 174.65 50.1 168.42Q40.15 162.19 34.05 150.98L41.61 140.29Q46.49 149.91 53.23 154.41Q59.98 158.92 67.57 158.92Q74.81 158.92 80.55 154.68Q86.28 150.44 89.61 142.36Q92.94 134.28 92.94 122.74Q92.94 111.21 89.57 103.3Q86.21 95.39 80.38 91.32Q74.55 87.25 67.15 87.25Q59.09 87.25 52.48 91.92Q45.86 96.59 41 105.51ZM236.42 107.89Q236.42 114.99 232.32 118.72Q228.22 122.44 220.61 122.44H157.3V112.13H200.56Q207.32 112.13 207.32 105.81Q207.32 95.72 202.03 89.97Q196.74 84.23 188.63 84.23Q182.06 84.23 176.87 87.8Q171.67 91.37 168.71 98.15Q165.74 104.93 165.74 114.64Q165.74 133.83 175.1 143.78Q184.46 153.73 199.59 153.73Q209.06 153.73 216.07 149.66Q223.08 145.59 226.97 139.87Q228.85 137.93 230.09 137.11Q231.33 136.29 232.68 136.39Q234.32 136.44 235.43 137.69Q236.54 138.94 236.53 141.57Q236.27 150.11 230.64 157.64Q225.02 165.17 215.05 169.88Q205.08 174.6 191.67 174.6Q175.73 174.6 164.02 168.12Q152.3 161.65 145.92 150.08Q139.54 138.52 139.54 123.38Q139.54 107.99 145.89 95.85Q152.24 83.71 164.15 76.68Q176.05 69.64 192.63 69.64Q205.86 69.64 215.7 74.68Q225.54 79.72 230.98 88.37Q236.42 97.02 236.42 107.89ZM294.4 77.46V149.25Q294.4 153.84 295.39 155.78Q296.38 157.73 298.51 158.64L301.73 159.81Q305.66 161.88 305.66 165.69Q305.66 172 299.07 172H260.67Q257.35 172 255.41 170.33Q253.47 168.66 253.47 165.69Q253.47 163.32 254.69 161.78Q255.9 160.23 258.14 159.06L261.31 158.02Q263.37 157.13 264.46 155.31Q265.54 153.49 265.54 149.25V100.34Q265.54 96.54 264.44 95.01Q263.33 93.47 261.28 92.84L256.31 92.37Q254.25 91.48 253.06 90.13Q251.86 88.78 251.86 86.64Q251.86 84.26 253.38 82.56Q254.9 80.86 258.19 79.67L275.84 73.28Q279.7 71.8 282.42 70.99Q285.15 70.19 287.73 70.19Q290.86 70.19 292.63 72.19Q294.4 74.19 294.4 77.46ZM290.87 103.95 284.76 96.99 290.1 91.96Q302.58 80.09 312.79 74.83Q322.99 69.57 332.07 69.57Q345.55 69.57 353.9 78.21Q362.26 86.85 362.26 101.07V149.25Q362.26 153.49 363.34 155.34Q364.43 157.19 366.49 158.02L369.58 159.06Q371.82 160.33 373.04 161.83Q374.25 163.32 374.25 165.69Q374.25 168.66 372.36 170.33Q370.47 172 367.05 172H328.65Q322.06 172 322.06 165.69Q322.06 161.88 326 159.81L329.29 158.64Q331.41 157.73 332.41 155.78Q333.4 153.84 333.4 149.25V108.19Q333.4 99.4 329.06 94.76Q324.72 90.11 317.24 90.11Q312.25 90.11 306.67 92.41Q301.1 94.71 295.21 100.05ZM430.1 77.46V149.25Q430.1 153.84 431.1 155.78Q432.09 157.73 434.22 158.64L437.43 159.81Q441.36 161.88 441.36 165.69Q441.36 172 434.77 172H396.37Q393.05 172 391.11 170.33Q389.17 168.66 389.17 165.69Q389.17 163.32 390.39 161.78Q391.6 160.23 393.84 159.06L397.01 158.02Q399.08 157.13 400.16 155.31Q401.25 153.49 401.25 149.25V100.34Q401.25 96.54 400.14 95.01Q399.03 93.47 396.99 92.84L392.01 92.37Q389.96 91.48 388.76 90.13Q387.56 88.78 387.56 86.64Q387.56 84.26 389.08 82.56Q390.6 80.86 393.89 79.67L411.54 73.28Q415.4 71.8 418.13 70.99Q420.85 70.19 423.43 70.19Q426.56 70.19 428.33 72.19Q430.1 74.19 430.1 77.46ZM426.58 103.95 420.47 96.99 425.81 91.96Q438.28 80.09 448.49 74.83Q458.7 69.57 467.77 69.57Q481.25 69.57 489.6 78.21Q497.96 86.85 497.96 101.07V149.25Q497.96 153.49 499.04 155.34Q500.13 157.19 502.2 158.02L505.29 159.06Q507.52 160.33 508.74 161.83Q509.95 163.32 509.95 165.69Q509.95 168.66 508.06 170.33Q506.17 172 502.75 172H464.35Q457.76 172 457.76 165.69Q457.76 161.88 461.7 159.81L464.99 158.64Q467.12 157.73 468.11 155.78Q469.1 153.84 469.1 149.25V108.19Q469.1 99.4 464.76 94.76Q460.42 90.11 452.95 90.11Q447.95 90.11 442.38 92.41Q436.8 94.71 430.92 100.05ZM590.61 160.76 575.51 189.42 533.08 94.22Q531.1 89.68 528.61 88.12Q526.12 86.55 522.28 84.98Q519.97 83.72 518.81 82.13Q517.65 80.55 517.65 78.4Q517.65 75.34 519.61 73.67Q521.58 72 524.85 72H569.28Q572.67 72 574.55 73.64Q576.42 75.28 576.42 78.32Q576.42 80.73 574.97 82.36Q573.52 83.99 570.79 84.9L567.05 85.97Q563.45 87.01 563.13 90.05Q562.81 93.09 565.29 99.09ZM568.68 186.68 574.19 174.17 579.81 164.79 605.87 99.21Q608.11 93.52 607.24 90.28Q606.37 87.03 602.66 85.96L598.49 84.9Q595.82 84.03 594.47 82.39Q593.13 80.74 593.13 78.32Q593.13 75.29 595.08 73.64Q597.02 72 600.35 72H630.14Q633.55 72 635.45 73.64Q637.35 75.28 637.35 78.32Q637.35 80.2 636.46 81.83Q635.57 83.46 632.87 84.82Q628.68 86.44 626.17 89.07Q623.67 91.69 621.15 97.65L586.01 182.33Q579.8 197.05 574.19 205.78Q568.59 214.5 562.21 218.29Q555.82 222.07 547.27 222.07Q534.46 222.07 527.17 214.9Q519.87 207.73 519.87 197.23Q519.87 190.76 523.08 186.8Q526.29 182.84 531.79 182.84Q537.29 182.84 540 186.01Q542.72 189.19 544.73 194.63L546.12 198.56Q547.01 202.03 548.93 203.67Q550.85 205.31 553.31 205.31Q556.1 205.31 558.55 203.63Q561 201.95 563.46 197.88Q565.93 193.81 568.68 186.68Z'/><path class=dot d='M647.39,148.8a24,24 0 1,0 48,0a24,24 0 1,0 -48,0Z'/></g></svg>"
)
STATIC = Path(__file__).parent / "static"
STATIC_FILES = {  # the logo kit's web icons, served from the root like a site's
    "/favicon.ico": "image/x-icon", "/favicon.svg": "image/svg+xml", "/apple-touch-icon.png": "image/png",
    "/icon-192.png": "image/png", "/icon-512.png": "image/png", "/icon-maskable-512.png": "image/png",
    "/site.webmanifest": "application/manifest+json",
}
ICON_LINKS = (
    "<link rel=icon href='/favicon.ico' sizes=any><link rel=icon href='/favicon.svg' type='image/svg+xml'>"
    "<link rel=apple-touch-icon href='/apple-touch-icon.png'><link rel=manifest href='/site.webmanifest'>"
    "<meta name=apple-mobile-web-app-title content=penny>"
)


def _head(title: str) -> str:
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<meta name=color-scheme content='light dark'><meta name=theme-color content='#F6F3EC' media='(prefers-color-scheme: light)'><meta name=theme-color content='#131614' media='(prefers-color-scheme: dark)'><title>{escape(title)}</title>{ICON_LINKS}{FONTS}<style>{CSS}</style></head>"
    )


# --------------------------------------------------------------------------
# Pages: / (overview), /cashflow, /cards, /cards/<key>, /memberships, /todo, /checks, /data.
# One CSS block, one nav, server-rendered; docs/board-redesign-plan.md.

PAGES = (("/", "Overview"), ("/accounts", "Accounts"), ("/cashflow", "Cash flow"), ("/cards", "Cards"), ("/memberships", "Memberships"), ("/todo", "To do"), ("/transactions", "Transactions"), ("/budget", "Budget"), ("/recurring", "Recurring"), ("/checks", "Checks"), ("/data", "Data"))
SECTIONS = ((None, ("/", "/accounts", "/todo")), ("Spending", ("/transactions", "/budget", "/recurring", "/cashflow")),
            ("Worth it?", ("/cards", "/memberships")), ("More", ("/checks", "/data")))  # the sidebar's groups
TABS = (("/", "Home"), ("/transactions", "Spending"), ("/cards", "Worth it"), ("/todo", "To do"), ("/more", "More"))  # the phone's tab bar, one per section
ICONS = {  # the sidebar and the phone's tab bar
    "/": "<path d='M3 11l9-8 9 8v10a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1z'/>",
    "/accounts": "<path d='M3 21h18'/><path d='M5 21v-10'/><path d='M10 21v-10'/><path d='M14 21v-10'/><path d='M19 21v-10'/><path d='M12 3l9 5H3z'/>",
    "/cashflow": "<path d='M4 20V10'/><path d='M10 20V4'/><path d='M16 20v-7'/><path d='M22 20H2'/>",
    "/cards": "<rect x='2' y='5' width='20' height='14' rx='2'/><path d='M2 10h20'/>",
    "/memberships": "<path d='M20 7H4a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2z'/><path d='M16 7V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v2'/>",
    "/todo": "<path d='M9 11l3 3L22 4'/><path d='M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11'/>",
    "/transactions": "<path d='M8 6h13'/><path d='M8 12h13'/><path d='M8 18h13'/><path d='M3 6h.01'/><path d='M3 12h.01'/><path d='M3 18h.01'/>",
    "/budget": "<circle cx='12' cy='12' r='9'/><path d='M12 3v9l6.4 6.4'/>",
    "/recurring": "<path d='M17 2l4 4-4 4'/><path d='M3 11v-1a4 4 0 0 1 4-4h14'/><path d='M7 22l-4-4 4-4'/><path d='M21 13v1a4 4 0 0 1-4 4H3'/>",
    "/checks": "<circle cx='12' cy='12' r='9'/><path d='M8 12l3 3 5-6'/>",
    "/more": "<circle cx='5' cy='12' r='1'/><circle cx='12' cy='12' r='1'/><circle cx='19' cy='12' r='1'/>",
    "/data": "<ellipse cx='12' cy='5' rx='8' ry='3'/><path d='M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5'/><path d='M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3'/>",
}


def _icon(href: str, size: int = 18) -> str:
    return f"<svg width={size} height={size} viewBox='0 0 24 24' fill=none stroke=currentColor stroke-width=2 stroke-linecap=round stroke-linejoin=round aria-hidden=true>{ICONS[href]}</svg>"
GROUPS = {"now": 0, "ask": 1, "check": 2, "quiet": 3, "wiki": 4}


def _tone(x: float) -> str:
    return "pos" if round(x) > 0 else "neg" if round(x) < 0 else ""


def _plain(html: str, limit: int = 200) -> str:
    """Rendered HTML as short escaped text, for a preview inside a link."""
    text = unescape(re.sub(r"<[^>]+>", "", html))
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return escape(text)


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


@dataclass
class View:
    """What every page shares, worked out once per request."""

    s: State
    items: list[Item]
    numbers: dict[str, int]  # ask and check items, numbered across both
    cards: list[CardRow]
    swipe: list[SwipeRow]
    margin: dict[str, float] | None  # the first card-worth alternative over the second
    flip: float | None  # $/pt where that margin crosses zero, inside [point_value]
    verdicts: dict[tuple[str, str, bool], model.Verdict]
    labels: dict[str, str]
    even: float | None = None  # the same crossing searched down to 0.1¢, when it lies below the range

    @property
    def open_count(self) -> int:
        return len(self.numbers)

    def card(self, key: str) -> CardRow | None:
        return next((r for r in self.cards if r.key == key), None)

    def hang(self, mkey: str) -> Item | None:
        """The question a membership's verdict hangs on, if one is open."""
        return next((it for it in self.items if it.key in self.numbers and it.membership == mkey and it.group == "ask" and it.d and (it.d.kind == "defaults" or it.d.data.get("flips"))), None)


def view(s: State) -> View:
    rules = s.rules
    bkey = rules["held"]["baseline_card"]
    items = plan(s)
    for it in items:
        if it.key.startswith("cpp:"):
            # The ¢/pt is set on the baseline card's page; To do keeps a row that links there.
            it.group, it.saved = "quiet", ""
            it.form = f"<p><a class=btn href='/cards/{escape(bkey)}#cpp'>Change it on the card's page ›</a></p>"
    items.sort(key=lambda i: GROUPS[i.group])
    numbered = [i for i in items if i.group in ("ask", "check")]
    margin = worth_margin(s.worth)
    alts = s.assumptions.get("card_worth", {}).get("alternatives", [])
    flip = flip_point(rules, s.assumptions, s.carried.get(alts[0], {})) if margin else None
    # The margin's low end takes benefits at their low too, so it can straddle
    # zero with no ¢/pt in the range flipping it; then say where it would.
    even = flip_point(rules, s.assumptions, s.carried.get(alts[0], {}), lo=0.001) if margin and not flip and margin["low"] < 0 <= margin["high"] else None
    return View(
        s, items, {it.key: n for n, it in enumerate(numbered, 1)},
        card_rows(rules, s.assumptions, s.spend, s.window.annualize, s.worth, s.today),
        swipe_rows(rules, s.spend, s.window.annualize, s.today), margin, flip,
        by_key(model.evaluate(rules, s.assumptions, s.spend, s.window.annualize)), _labels(rules), even,
    )


def _meta(s: State) -> str:
    bkey = s.rules["held"]["baseline_card"]
    bcard = s.rules["cards"][bkey]
    return f"{s.noun} to <b>{s.window.end}</b> · {escape(bcard.get('label', bkey))} @ <b>{cents(float(bcard.get('default_rate', 0.0)))}</b>"


def _section(href: str) -> tuple[str | None, tuple[str, ...]]:
    """The sidebar group a page sits in; /more belongs to More."""
    return next(((g, hs) for g, hs in SECTIONS if href in hs or (g == "More" and href == "/more")), (None, ()))


def nav(v: View, current: str) -> str:
    s = v.s
    labels = dict(PAGES)
    links = []
    for group, hrefs in SECTIONS:
        if group:
            links.append(f"<span class=grp>{escape(group)}</span>")
        for href in hrefs:
            on = " class=on aria-current=page" if href == current else ""
            count = f"<span class=count>{v.open_count}</span>" if href == "/todo" and v.open_count else ""
            links.append(f"<a href='{href}'{on}>{_icon(href)}{labels[href]}{count}</a>")
    return (
        f"<header class=side><a class=brand href='/'>{LOGO}</a><nav class=links aria-label=pages>{''.join(links)}</nav>"
        f"<span class=meta>{_meta(s)}</span></header>"
    )


def tabs(v: View, current: str) -> str:
    """The phone's tab bar: a tab is the current page, or marks the section it is in."""
    here = _section(current)[0]
    out = []
    for href, label in TABS:
        on = " class=on aria-current=page" if href == current else " class=on aria-current=true" if _section(href)[0] == here and here else ""
        text = label + (f" · {v.open_count}" if href == "/todo" and v.open_count else "")
        out.append(f"<a href='{href}'{on}>{_icon(href, 20)}{text}</a>")
    return f"<nav class=tabs aria-label=sections>{''.join(out)}</nav>"


def seg(current: str) -> str:
    """The phone's switcher between the pages of a section; the sidebar lists them on wider screens."""
    group, hrefs = _section(current)
    if not group or len(hrefs) < 2 or current not in hrefs:  # /more lists them itself
        return ""
    labels = dict(PAGES)
    links = "".join(f"<a href='{h}'" + (" class=on aria-current=page" if h == current else "") + f">{labels[h]}</a>" for h in hrefs)
    return f"<nav class=sectabs aria-label='{escape(group)}'>{links}</nav>"


def _page(v: View, current: str, title: str, body: str, bars: tuple[float, float] = (0.0, 1.0)) -> str:
    return (
        _head(f"{title} · penny" if title != "penny" else title)
        + f"<body data-lo='{bars[0]:.0f}' data-hi='{bars[1]:.0f}'><div class=app>{nav(v, current)}<div class=content><main>{seg(current)}{_syncbar(v.s)}{body}</main></div></div>{tabs(v, current)}<script>{JS}</script></body></html>"
    )


def _syncbar(s: State) -> str:
    out = f"<p class=syncbar role=alert>{escape(s.sync_alert)}</p>" if s.sync_alert else ""
    lines = check.attention(s.checks, s.today) if s.checks else []
    if lines:
        out += f"<p class=syncbar role=alert>{escape('; '.join(lines))}. <a href='/checks'>Checks ›</a></p>"
    return out


def _title(h1: str, lede: str = "", side: str = "") -> str:
    """``lede`` and ``side`` are HTML."""
    return f"<div class=pagehead><div><h1>{escape(h1)}</h1>" + (f"<p class=lede>{lede}</p>" if lede else "") + f"</div>{side}</div>"


def _tile(over: str, big: str, tone: str, line: str, foot: str = "", href: str | None = None, foot_cls: str = "foot") -> str:
    """``line`` and ``foot`` are HTML."""
    tag = f"a class=tile href='{href}'" if href else "div class=tile"
    end = "a" if href else "div"
    return f"<{tag}><span class=ov>{escape(over)}</span><span class='big {tone}'>{big}</span><span class=muted>{line}</span>" + (f"<span class={foot_cls}>{foot}</span>" if foot else "") + f"</{end}>"


def _block(over: str, inner: str, link: tuple[str, str] | None = None, note: str = "", bid: str = "", cls: str = "") -> str:
    head = f"<span class=ov>{escape(over)}</span>" + (f"<a href='{link[0]}'>{link[1]}</a>" if link else "")
    return f"<section class='block{' ' + cls if cls else ''}'{f' id={bid}' if bid else ''}><div class=bhead>{head}</div>{inner}" + (f"<p class=note>{note}</p>" if note else "") + "</section>"


@dataclass
class Col:
    """One column of a bar chart: bars as (css class, value), left to right."""

    label: str
    bars: list[tuple[str, float]]
    href: str = ""
    title: str = ""
    part: bool = False  # a month still running, drawn lighter
    on: bool = False  # the month the page is showing


def _chart(cols: list[Col], ref: tuple[float, str] | None = None, height: int = 150) -> str:
    """Server-drawn column chart; ``ref`` is a dashed line at a value, labelled."""
    top = max([v for c in cols for _, v in c.bars] + [ref[0] if ref else 0.0, 1.0])
    pct = lambda x: max(0.0, x) / top * 100
    out = []
    for c in cols:
        tag = f"a href='{escape(c.href)}'" if c.href else "span"
        bars = "".join(f"<i class='{k}' style='height:{pct(v):.1f}%'></i>" for k, v in c.bars)
        cls = "c" + (" part" if c.part else "") + (" on" if c.on else "")
        out.append(f"<{tag} class='{cls}' title='{escape(c.title)}'>{bars}</{tag.split()[0]}>")
    line = f"<i class=ref style='bottom:{pct(ref[0]):.1f}%'><span>{escape(ref[1])}</span></i>" if ref else ""
    labels = "".join(f"<span{' class=on' if c.on else ''}>{escape(c.label)}</span>" for c in cols)
    return f"<div class=chart><div class=plot style='height:{height}px'>{''.join(out)}{line}</div><div class=xl>{labels}</div></div>"


def _fee_text(r: CardRow) -> str:
    return f"{usd(r.fee)} a year" if r.fee else "no fee"


def _check_chip(r: CardRow) -> str:
    if r.verified:
        return f"<span class=chip>verified{' ' + escape(r.verified_on) if r.verified_on else ''}</span>"
    return "<span class='chip w'>" + ("¢/pt is a judgment" if r.role == "baseline" else "rates unverified") + "</span>"


def _mlabel(rules: dict, mkey: str) -> str:
    return rules["memberships"][mkey].get("label", mkey)


def membership_totals(rules: dict, verdicts: dict, rows: list[VerdictRow]) -> tuple[float, float]:
    """Sum of each membership's headline net at base, and of its held fee."""
    return sum(r.base for r in rows), sum(verdicts[headline_key(rules, r.membership)].fee for r in rows)


def flip_text(v: View) -> str:
    """Where the card-worth margin crosses zero with benefits at base."""
    if v.flip:
        return f" · flips below {cents(v.flip)}"
    if v.even:
        return f" · with benefits at base it stays ahead down to {cents(v.even)}; the low end is low benefits too"
    return ""


def card_story(v: View, r: CardRow) -> str:
    """The wallet row's second line, in words (plain text)."""
    s, rules = v.s, v.s.rules
    bkey = rules["held"]["baseline_card"]
    blabel = rules["cards"][bkey].get("label", bkey)
    card = rules["cards"][r.key]
    if r.role == "baseline":
        w = next((w for w in s.worth if w.card == r.key), None)
        if not w:
            return "Every other card's edge is measured against this one."
        out = f"Carries {usd(sum(s.carried.get(r.key, {}).values()))} a year. Earn {usd(w.earn['base'])} + benefits {usd(w.benefits['base'])} − fee {usd(w.fee)} = {signed(w.worth['base'])}"
        if v.margin and s.worth[0].card == r.key:
            out += f", or {signed(v.margin['base'])} against the {s.worth[1].label}"
        return out + "."
    out = f"{usd(r.edge)} a year over the {blabel}" + (f": {detail_text(r.detail, v.labels, 3)}." if r.detail else ".")
    for mkey, a in r.attributable.items():
        ml = _mlabel(rules, mkey)
        if card.get("requires_membership", True):
            out += f" It closes if {ml} lapses, so all of it counts toward {ml}."
        elif round(a):
            out += f" {usd(a)} of that needs {ml} and counts in its verdict; the rest is the card's own."
    if round(r.default_part):
        out += f" {usd(r.default_part)} of it is the {pct(float(card.get('default_rate', 0.0)))} default rate beating the {blabel} where neither has a bonus."
    if r.role == "dormant":
        out += f" Wherever it earns a bonus, the {blabel} pays as much or more."
    return out


def _swipe_list(v: View, rows: list[SwipeRow], spend: bool, cls: str = "") -> str:
    out = []
    for w in rows:
        amt = f" <span class=faint>{usd(w.spend)}</span>" if spend else ""
        amt += f" <span class=faint>· {escape(w.until)}</span>" if w.until else ""
        out.append(f"<div title='{escape(w.via)} pays {pct(w.rate)} here{escape(' ' + w.until) if w.until else ''}; {usd(w.gain)} a year over the baseline'><span>{escape(_cap(w.label))}{amt}</span><span class=c>{escape(w.via)}</span><span class=r>{pct(w.rate)}</span></div>")
    return f"<div class='panel swipe{' ' + cls if cls else ''}'>{''.join(out)}</div>"


def swipe_note(v: View) -> str:
    rules = v.s.rules
    bkey = rules["held"]["baseline_card"]
    b = rules["cards"][bkey]
    head = f"Best held card per family at {cents(float(b.get('default_rate', 0.0)))} a point; ties go to the {b.get('label', bkey)}."
    other = next((w for w in v.swipe if w.family == cat.OTHER), None)
    if not other or other.card == bkey:
        return escape(head)
    pv = model.point_values(rules, v.s.assumptions)
    gap_hi = max(0.0, (other.rate - pv["high"]) * other.spend)
    row = v.card(other.card)
    txt = (f"{head} On the {usd(other.spend)} outside every family, {other.via}'s {pct(other.rate)} beats the "
           f"{b.get('label', bkey)}'s {pct(float(b.get('default_rate', 0.0)))}: {usd(other.gain)} a year")
    if row and round(row.edge) != round(other.gain):
        txt += f", part of the {row.label}'s {usd(row.edge)} edge"
    return escape(txt + f". At {cents(pv['high'])} a point that gap is {usd(gap_hi)}.")


UPCOMING_DAYS = 14  # how far ahead the overview lists recurring charges
RISE_DAYS = 45  # a price rise stays on the overview this long after the charge


def _month_name(key: str) -> str:
    return date.fromisoformat(key + "-01").strftime("%B")


def _hero(s: State, sel: cashflow.Month, full: list[cashflow.Month], lines: list[budget.Line]) -> str:
    """Left to spend this month, or spent so far against the average while no budget is set."""
    now = cashflow.month_of(s.today)
    gone = budget.elapsed(sel.key, s.today)
    month = _month_name(sel.key)
    days = (calendar.monthrange(s.today.year, s.today.month)[1] - s.today.day) if sel.key == now else 0
    left_days = f" · {days} day{'s' if days != 1 else ''} left" if sel.key == now else ""
    set_ = [ln for ln in lines if ln.budget > 0]
    if set_:
        total_b = sum(ln.budget for ln in set_)
        spent = sum(ln.actual for ln in set_)
        left = total_b - spent
        over = round(left) < 0
        head = f"{'Over budget' if over else 'Left to spend'} in {month}"
        big = f"<span class='big{' neg' if over else ''}'>{usd(abs(left))}</span>"
        sub = f"{usd(spent)} spent of {usd(total_b)}{left_days}"
        other = sel.total_spent - spent
        if round(other) > 0:
            sub += f" · {usd(other)} more outside the budget"
        share, target = spent / total_b if total_b else 0.0, total_b
        link = "<a href='/budget'>Budget ›</a>"
    else:
        n = len(full)
        avg = sum(m.total_spent for m in full) / n if n else 0.0
        head = f"Spent in {month}" + (" so far" if sel.key == now else "")
        big = f"<span class=big>{usd(sel.total_spent)}</span>"
        sub = f"against {usd(avg)} in an average month{left_days}"
        share, target = (sel.total_spent / avg if avg else 0.0), avg
        link = "<a href='/budget'>Set budgets ›</a>"
    fill = f"<i class='fill{' over' if share > 1 else ''}' style='width:{min(1.0, max(0.0, share)) * 100:.1f}%'></i>"
    pace = f"<i class=pace style='left:{gone * 100:.1f}%' title='where an even month would be today'></i>" if gone is not None and target else ""
    legend = f"<span>{share:.0%} spent</span>" + (f"<span>today: {gone:.0%} of the month</span>" if gone is not None else "")
    return (f"<section class='hero ov-hero'><div class=bhead><h1 class=ov>{escape(head)}</h1>{link}</div>{big}"
            f"<p class=muted>{sub}</p><div class=prog>{fill}{pace}</div><div class='legend faint'>{legend}</div></section>")


def _attention(v: View, lines: list[budget.Line], found: list[recurring.Recurring]) -> str:
    """Only what needs a look: a deadline on To do, a category over budget, a price rise, open questions."""
    s = v.s
    out = []
    for it in v.items:
        if it.group == "now" and it.d and it.d.due:
            out.append(("warn", f"<a href='/todo#{escape(it.key)}'><b>{escape(it.title)}</b></a> <span class=muted>{(it.d.due - s.today).days} days left.</span>"))
    for ln in lines:
        if ln.budget > 0 and ln.actual > ln.budget + 0.5:
            out.append(("crit", f"<b>{escape(_cap(ln.category))} is {usd(ln.actual - ln.budget)} over budget.</b> <span class=muted>{usd(ln.actual)} of {usd(ln.budget)}.</span>"))
    for r in found:
        if not r.stopped and r.before is not None and r.amount > r.before and (s.today - r.last).days <= RISE_DAYS:
            out.append(("warn", f"<b>{escape(r.merchant)} went up.</b> <span class=muted>${r.amount:,.2f} {escape(r.cadence)}, was ${r.before:,.2f}.</span>"))
    asks = sum(1 for it in v.items if it.key in v.numbers)
    if asks:
        out.append(("neutral", f"<a href='/todo'><b>{asks} open question{'s' if asks != 1 else ''}</b></a> <span class=muted>could change a verdict.</span>"))
    if not out:
        return ""
    items = "".join(f"<li class={tone}>{text}</li>" for tone, text in out)
    return f"<section class='block ov-look'><div class=bhead><h2 class=h>Needs a look</h2></div><ul class=alerts>{items}</ul></section>"


def _coming(s: State, found: list[recurring.Recurring], days: int = UPCOMING_DAYS, more: bool = True) -> str:
    soon = sorted((r for r in found if not r.stopped and s.today <= r.next_due <= s.today + timedelta(days=days)), key=lambda r: r.next_due)
    if not soon:
        rows = f"<p class=note>No recurring charge due in the next {days} days.</p>"
    else:
        rows = "<div class=bills>" + "".join(
            f"<a href='{escape(_txn_href({'show': 'all'}, q=r.key))}'><span class=d>{r.next_due:%b %-d}</span><span>{escape(r.merchant)}</span><span class=mono>${r.amount:,.2f}</span></a>"
            for r in soon) + "</div>"
    total = f"next {days} days · ${sum(r.amount for r in soon):,.2f}" if soon else ""
    link = "<a class=more href='/recurring'>Every recurring charge ›</a>" if more else ""
    return f"<section class='block ov-coming'><div class='bhead rule'><h2 class=h>Coming up</h2><span class=faint>{total}</span></div>{rows}{link}</section>"


def _where(sel: cashflow.Month, lines: list[budget.Line]) -> str:
    top = sorted((ln for ln in lines if round(ln.actual) > 0), key=lambda ln: -ln.actual)[:5]
    rows = []
    for ln in top:
        base = ln.budget if ln.budget > 0 else ln.average
        over = ln.budget > 0 and ln.actual > ln.budget + 0.5
        label = f"{usd(ln.actual)} of {usd(ln.budget)}" if ln.budget > 0 else f"{usd(ln.actual)} · avg {usd(ln.average)}"
        width = min(1.0, ln.actual / base) * 100 if base > 0 else 100.0
        href = escape(_txn_href({}, cat=ln.category, m=sel.key))
        rows.append(f"<a class=cat href='{href}'><span class=l1><span>{escape(_cap(ln.category))}</span><span class='mono{' neg' if over else ' muted'}'>{label}</span></span>"
                    f"<span class=thin><i class='{'over' if over else ''}' style='width:{width:.1f}%'></i></span></a>")
    inner = "".join(rows) or "<p class=note>Nothing spent yet this month.</p>"
    return f"<section class='block ov-where'><div class='bhead rule'><h2 class=h>Where it went</h2><a href='/budget'>Budget ›</a></div><div class=cats>{inner}</div></section>"


def _recent(s: State, n: int = 4) -> str:  # the phone shows three
    names: dict[str, set[str]] = {}
    for t in s.txns:
        names.setdefault(t.account, set()).add(t.account_number)
    rows = sorted((t for t in s.txns if not t.transfer), key=lambda t: t.date, reverse=True)[:n]  # spend and income; card payments are transfers
    items = "".join(
        f"<a class=lrow href='{escape(_txn_href({'show': 'all'}, id=t.txn_id))}#edit'><span class=l1><b>{escape(_merchant(t))}</b><span class=amt>{_amount_cell(t)}</span></span>"
        f"<span class=l2>{escape(_cap(t.budget_category) if feed.is_spend(t) else t.kind)}<span class=sep>·</span>{_ago(t.date, s.today)}<span class=sep>·</span>{escape(_account_label(t, names))}</span></a>"
        for t in rows)
    return f"<section class='block ov-recent'><div class='bhead rule'><h2 class=h>Recent</h2><a href='/transactions'>All ›</a></div><div class='list flat'>{items}</div></section>"


def _ago(d: date, today: date) -> str:
    days = (today - d).days
    return "today" if days == 0 else "yesterday" if days == 1 else f"{d:%a %-d %b}"


def _flow(s: State, months: list[cashflow.Month], full: list[cashflow.Month]) -> str:
    now = cashflow.month_of(s.today)
    shown = full + [m for m in months if m.key == now]
    if not shown:
        return ""
    cols = _chart([Col(_month_name(m.key)[:3], [("in", m.total_in), ("out", m.total_spent)], f"/cashflow?m={m.key}",
                       f"{_month_name(m.key)}: {usd(m.total_in)} in, {usd(m.total_spent)} spent", m.key == now) for m in shown])
    n = len(full)
    net = sum(m.net for m in full)
    avg_in, avg_out = (sum(m.total_in for m in full) / n, sum(m.total_spent for m in full) / n) if n else (0.0, 0.0)
    span = f"the last {n} full months" if n == CASH_YEAR else f"the {n} full month{'s' if n != 1 else ''} on file"
    note = (f"<p class=note><span class=key><i class=in></i>in {usd(avg_in)}/mo</span> <span class=key><i class=out></i>spent {usd(avg_out)}/mo</span> · "
            f"net <b class={_tone(net)}>{signed(net)}</b> over {span}</p>") if n else ""
    return f"<section class='block ov-flow'><div class='bhead rule'><h2 class=h>Cash flow</h2><a href='/cashflow'>Cash flow ›</a></div>{cols}{note}</section>"


def _worth(s: State, v: View) -> str:
    """The cards-and-memberships verdicts in three figures, and where to swipe."""
    rules = s.rules
    bkey = rules["held"]["baseline_card"]
    bcard = rules["cards"][bkey]
    blabel = bcard.get("label", bkey)
    net, fees = membership_totals(rules, v.verdicts, s.verdicts)
    words = " · ".join(f"{_mlabel(rules, r.membership)} {_split_verdict(r.verdict)[0]}" for r in s.verdicts)
    tiles = [_tile("Memberships", signed(net), _tone(net), f"a year, net of {usd(fees)} in fees", escape(words), "/memberships")]
    if v.margin:
        m = v.margin
        foot = f"{signed(m['low'])} to {signed(m['high'])} as the point value and benefits move" + flip_text(v)
        tiles.append(_tile(blabel, signed(m["base"]), _tone(m["base"]), f"a year over the {escape(s.worth[1].label)} card", foot, f"/cards/{escape(bkey)}"))
    else:
        tiles.append(_tile(blabel, cents(float(bcard.get("default_rate", 0.0))), "", "a point: every card's edge is measured against this card", "", f"/cards/{escape(bkey)}"))
    others = sorted((r for r in v.cards if r.held and r.role != "baseline"), key=lambda r: -r.bonus)
    bonus = sum(r.bonus for r in others)
    tiles.append(_tile("Store and category cards", signed(bonus), _tone(bonus), f"a year of bonus rates over swiping the {escape(blabel)}", escape(" · ".join(f"{r.label} {usd(r.bonus)}" for r in others)), "/cards"))
    top = [w for w in v.swipe if w.family != cat.OTHER][:7] + [w for w in v.swipe if w.family == cat.OTHER]
    return ("<div class='cols ovc'>"
            + f"<section class='block ov-worth'><div class='bhead rule'><h2 class=h>Worth it?</h2><a href='/cards'>Cards ›</a></div><div class='tiles slim'>{''.join(tiles)}</div></section>"
            + f"<section class='block ov-swipe'><div class='bhead rule'><h2 class=h>Where to swipe</h2><a href='/cards'>Every card ›</a></div>{_swipe_list(v, top, False)}<p class=note>{swipe_note(v)}</p></section>"
            + "</div>")


def render_page(s: State, v: View | None = None) -> str:
    """The overview (docs/ui-overhaul-plan.md § The new Overview): this month's
    money first, then what needs a look, bills coming up, where it went, recent
    rows, cash flow, and the cards-and-memberships verdicts."""
    v = v or view(s)
    if not s.txns:
        body = [_title("penny", "This board was started on a Rocket Money export, which carries no income, transfers or balances: the month's money needs the card feed."),
                _attention(v, [], []), _worth(s, v)]
        return _page(v, "/", "penny", "".join(body))
    months, full, sel = cash_months(s, cashflow.month_of(s.today))
    lines = budget.lines(sel, full, s.assumptions.get("budget", {}))
    found = recurring.find(s.txns, s.today, recurring_counts(s))
    body = [
        f"<p class='today faint'>{s.today:%A %-d %B}</p>",
        "<div class='cols ovc ov-top'>", _hero(s, sel, full, lines), _attention(v, lines, found) or "<span></span>", "</div>",
        _balances(s),
        "<div class='cols even ovc'>", _coming(s, found), _where(sel, lines), "</div>",
        "<div class='cols even ovc'>", _recent(s), _flow(s, months, full), "</div>",
        _worth(s, v),
        "<p class='note ov-links'><a href='/data'>The data behind these numbers ›</a> · <a href='/checks'>Balance checks ›</a></p>",
    ]
    return _page(v, "/", "penny", "".join(body))


def render_cards(s: State, v: View | None = None) -> str:
    v = v or view(s)
    rules = s.rules
    bkey = rules["held"]["baseline_card"]
    blabel = rules["cards"][bkey].get("label", bkey)
    held = [r for r in v.cards if r.held]
    cons = [r for r in v.cards if not r.held]
    unv = [r for r in v.cards if not r.verified]
    side = f"<div class=chips><span class=chip>{len(held)} held</span>" + (f"<span class=chip>{len(cons)} considered</span>" if cons else "") + (f"<span class='chip w'>{len(unv)} unverified</span>" if unv else "") + "</div>"

    bars = sorted((r for r in v.cards if r.role != "baseline"), key=lambda r: -r.edge)
    top = max([r.edge for r in bars] + [1.0])
    chart = []
    for r in bars:
        width = f"<i class=fill style='width:{max(0.0, r.edge) / top * 100:.1f}%'></i>" if round(r.edge) else ""
        chart.append(f"<div class='hbar{' dim' if not r.held else ''}' title='{escape(r.label)}: {usd(r.edge)} a year over the {escape(blabel)}{'' if r.held else ', not held'}'><span>{escape(r.label)}</span><span class=track>{width}</span><span class=v>{usd(r.edge)}</span></div>")
    pv = model.point_values(rules, s.assumptions)
    annual = model.annualized(s.spend, s.window.annualize)
    r_at = {lvl: model.rules_at(rules, pv[lvl])["cards"] for lvl in LEVELS}
    at = {r.key: {lvl: model.card_edge_on(r_at[lvl][r.key], r_at[lvl][bkey], usable_spend(rules, rules["cards"][r.key], annual), s.today)[0] for lvl in LEVELS} for r in bars}
    movers = [(abs(at[r.key]["high"] - at[r.key]["low"]), r) for r in bars if r.key in at]
    mover = ""
    if movers and pv["low"] != pv["high"]:
        d, r = max(movers, key=lambda x: x[0])
        if round(d):
            mover = f"At {cents(pv['low'])} the {escape(r.label)} edge is {usd(at[r.key]['low'])}; at {cents(pv['high'])} it is {usd(at[r.key]['high'])}. The point value moves these more than anything else."
    chart_html = (f"<div class='panel hbars'>{''.join(chart)}<div class=hfoot><span class=note>{mover or 'Held cards in blue; cards you do not hold in grey.'}</span>"
                  f"<a href='/cards/{escape(bkey)}#cpp'>change ¢/pt ›</a></div></div>")

    wallet = []
    for r in v.cards:
        chips = [f"<span class=chip>{escape(_mlabel(rules, m))}</span>" for m in r.members] + [_check_chip(r)]
        w = next((w for w in s.worth if w.card == r.key), None)
        amt, what = (w.worth["base"], "worth, a year") if w else (r.edge, "edge, a year" if r.held else "a year if you applied")
        role = r.role + (f" for {_and([_mlabel(rules, m) for m in r.members])}" if r.members and r.role == "store card" else "")
        rates = r.rates + (f" · without the membership: {r.without}" if r.without else "")
        wallet.append(
            f"<a class='wcard{' dim' if not r.held else ''}' href='/cards/{escape(r.key)}'>"
            f"<span class=who><span class=name>{escape(r.label)}</span><small>{escape(role)} · {_fee_text(r)}</small><span class=chips>{''.join(chips)}</span></span>"
            f"<span class=what><span>{escape(rates)}</span><small>{escape(card_story(v, r))}</small></span>"
            f"<span class=amt><b>{signed(amt)}</b><small>{what}</small></span>"
            f"<span class='badge {r.tone}'>{escape(r.badge)}</span></a>"
        )
    body = [
        _title("Cards", f"Every card's edge is measured against the {escape(blabel)}, the card you would use if the other did not exist. Rates come from rules.toml.", side),
        _block("Where to swipe", _swipe_list(v, v.swipe, True, "two"), note=swipe_note(v)),
        _block(f"Edge over the {blabel}, a year", chart_html),
        _block("The wallet", f"<div class=panel>{''.join(wallet)}</div>"),
    ]
    return _page(v, "/cards", "Cards", "".join(body))


def _table(head: list[str], rows: list[list[str]], num: set[int], total: list[str] | None = None, stack: bool = False) -> str:
    """Cells are HTML. Columns in ``num`` are right-aligned figures. ``stack``:
    a phone shows each row as a card of label: value lines, for a table too
    wide to scroll comfortably."""
    def tr(cells, tag="td"):
        return "<tr>" + "".join(f"<{tag}{' class=n' if i in num else ''}"
                                + (f' data-l="{head[i]}"' if stack and tag == "td" and i else "") + f">{c}</{tag}>" for i, c in enumerate(cells)) + "</tr>"
    body = "".join(tr(r) for r in rows) + (tr(total).replace("<tr>", "<tr class=total>", 1) if total else "")
    return f"<div class='panel tscroll'><table class='t{' stack' if stack else ''}'><thead>{tr(head, 'th')}</thead><tbody>{body}</tbody></table></div>"


def _span_dates(cell: str) -> str:
    """A "start → end" cell that breaks only at the arrow, so a phone shows two lines, not four."""
    start, sep, rest = cell.partition(" → ")
    return f"<span class=nw>{start} →</span> <span class=nw>{rest}</span>" if sep else cell


def credit_rows(s: State, key: str) -> list[list[str]]:
    """The report's fee-and-credits table, plus one projected row for the year
    in progress, or the next one, when its fee has not billed yet."""
    rules = s.rules
    spec = rules["card_credits"][key]
    credits = spec.get("credits", {})
    card = rules["cards"].get(key, {})
    years = s.credit_years.get(key, [])
    rows = []
    for y in years:
        cells = [f"{usd(y.credits[k])} / {usd(float(c['face']))}" if "face" in c else usd(y.credits[k]) for k, c in credits.items()]
        rows.append([f"{y.start} → {y.end}", usd(y.fee), *cells, f"<span class=faint>{escape(y.partial or '')}</span>"])
    if "annual_fee" in card:
        mmdd = spec.get("year_starts", "01-01")
        by = {y.start: y for y in years}
        cur = cardworth.year_start(s.today, mmdd)
        nxt = date(cur.year + 1, cur.month, cur.day)
        proj = nxt if cur in by and by[cur].fee > 0 else cur
        if not (proj in by and by[proj].fee > 0):
            end = date(proj.year + 1, proj.month, proj.day) - timedelta(days=1)
            # A credit whose `until` falls before the projected year is gone (the Reserve's).
            cells = ["—" if c.get("until") and date.fromisoformat(str(c["until"])) < proj else f"$0 / {usd(float(c['face']))}" if "face" in c else "$0" for c in credits.values()]
            rows.append([f"{proj} → {end} <span class=faint>projected</span>", f"{usd(float(card['annual_fee']))} <span class=faint>list</span>", *cells,
                         "<span class=faint>not billed yet; a credit's descriptor is a guess until one posts</span>"])
    return rows


def render_card(s: State, key: str, v: View | None = None) -> str | None:
    """One card's page, or None for a key that is not a card on the board."""
    v = v or view(s)
    r = v.card(key)
    if r is None:
        return None
    rules, a = s.rules, s.assumptions
    card = rules["cards"][key]
    bkey = rules["held"]["baseline_card"]
    blabel = rules["cards"][bkey].get("label", bkey)
    w = next((w for w in s.worth if w.card == key), None)
    spec = rules.get("card_credits", {}).get(key)
    points = key == bkey or key in a.get("point_value", {}) or "points_of" in card
    chips = [f"<span class=chip>{escape(r.role)}</span>"] + [f"<span class=chip>{escape(_mlabel(rules, m))}</span>" for m in r.members]
    chips += [f"<span class=chip>{_fee_text(r)}</span>", _check_chip(r)]
    if spec and spec.get("year_starts"):
        chips.append(f"<span class=chip>year starts {escape(spec['year_starts'])}</span>")
    head = (f"<p class=note><a href='/cards'>‹ Cards</a></p><div class=pagehead><div><h1>{escape(r.label)}</h1><span class=chips>{''.join(chips)}</span></div>"
            f"<span class='badge {r.tone} lg'>{escape(r.badge)}</span></div>")

    tiles = []
    if w:
        tiles.append(_tile("Worth, a year", signed(w.worth["base"]), _tone(w.worth["base"]), f"earn {usd(w.earn['base'])} + benefits {usd(w.benefits['base'])} − fee {usd(w.fee)}", f"{signed(w.worth['low'])} to {signed(w.worth['high'])}"))
    if v.margin and s.worth[0].card == key:
        alt, m = s.worth[1], v.margin
        foot = f"{signed(m['low'])} to {signed(m['high'])}" + flip_text(v)
        tiles.append(_tile(f"Over the {alt.label}", signed(m["base"]), _tone(m["base"]), f"the {escape(alt.label)} would earn {usd(alt.worth['base'])}", foot, foot_cls="foot warnc" if v.flip or v.even else "foot"))
    if key != bkey:
        attr = " · ".join(f"{usd(x)} needs {escape(_mlabel(rules, mk))}" for mk, x in r.attributable.items() if round(x))
        tiles.append(_tile(f"Edge over the {blabel}", usd(r.edge), "", "a year" + (f"; {usd(r.default_part)} from its default rate" if round(r.default_part) else ""), attr))
    rated = card.get("rates", {})
    if key in s.carried:
        sp = s.carried[key]
        bonus_sp = sum(x for f, x in sp.items() if f in rated and float(rated[f]) > float(card.get("default_rate", 0.0)))
        tiles.append(_tile("Spend it carries", usd(sum(sp.values())), "", "a year, on its own accounts", f"{usd(bonus_sp)} at bonus rates · the rest at {pct(float(card.get('default_rate', 0.0)))}"))
    elif rated:
        annual = model.annualized(s.spend, s.window.annualize)
        fams = [f for f in rated if annual.get(f, 0.0) > 0]
        tiles.append(_tile("Where it earns a bonus", usd(sum(annual.get(f, 0.0) for f in fams)), "", "a year of household spend there", escape(", ".join(v.labels.get(f, f) for f in rated))))
    if points:
        pv = cardworth.card_point_values(rules, a, key)
        tiles.append(_tile("A point is worth", cents(pv["base"]), "", "a judgment, not a card term" if key == bkey else f"pooled into the {rules['cards'][card['points_of']].get('label', card['points_of'])}" if "points_of" in card else "its default rate", f"{cents(pv['low'])} low · {cents(pv['high'])} high" if pv["low"] != pv["high"] else ""))

    left, right = [], []
    mults = rate_multiples(card)
    def rate_cell(f):
        rate = model.card_rate(card, f)
        return f"{mults[f]:g}x · {pct(rate)}" if points and f in mults else f"1x · {pct(rate)}" if points else pct(rate)
    if key in s.carried:
        sp = s.carried[key]
        rows = [[escape(v.labels.get(f, f)), rate_cell(f), usd(x), usd(model.card_rate(card, f) * x)] for f, x in sorted(sp.items(), key=lambda kv: -kv[1]) if f in rated]
        rest = sum(x for f, x in sp.items() if f not in rated)
        rows.append(["Everything else it carries", rate_cell(cat.OTHER), usd(rest), usd(float(card.get("default_rate", 0.0)) * rest)])
        earn = w.earn["base"] if w else sum(model.card_rate(card, f) * x for f, x in sp.items())
        left.append(_block("What it earns", _table(["Family", "Rate", "Spend / yr", "Earn"], rows, {1, 2, 3}, [f"Earn at {cents(float(card.get('default_rate', 0.0)))}" if points else "Earn", "", usd(sum(sp.values())), usd(earn)]),
                           note="On the spend on this card's own account numbers. Only families in rules.toml earn a bonus rate; anything else sits at the default."))
    elif r.detail or rated:
        annual = model.annualized(s.spend, s.window.annualize)
        rows = [[escape(v.labels.get(f, f)), pct(model.card_rate(card, f)), pct(model.card_rate(rules["cards"][bkey], f)), usd(annual.get(f, 0.0)), usd(x)] for f, x in sorted(r.detail.items(), key=lambda kv: -kv[1])]
        if rows:
            left.append(_block(f"Where it beats the {blabel}", _table(["Family", "This card", blabel, "Spend / yr", "Edge"], rows, {1, 2, 3, 4}, ["Edge", "", "", "", usd(r.edge)]),
                               note="Household spend by family, whichever card carried it: what this card would add if it carried all of it."))
        else:
            left.append(_block(f"Where it beats the {blabel}", f"<p class=empty>Nowhere: the {escape(blabel)} pays as much or more on every family with spend.</p>"))
        if r.without:
            left.append(f"<p class=note>Without the membership: {escape(r.without)}.</p>")
    if card.get("quarters"):
        annual = usable_spend(rules, card, model.annualized(s.spend, s.window.annualize))
        rows = []
        for q in card["quarters"]:
            start, end = model._q(q, "start"), model._q(q, "end")
            when = "over" if end < s.today else "running" if start <= s.today else "to come"
            adds = model.rotating_edge({**card, "quarters": [q]}, rules["cards"][bkey], annual, s.today)[0]
            mult = f"{float(q['points']):g}x · " if "points" in q else ""
            rows.append([f"{start:%b %-d} – {end:%b %-d, %Y} <span class=faint>{when}</span>", escape(", ".join(v.labels.get(f, f) for f in q["families"])),
                         mult + pct(float(q["rate"])), usd(float(q.get("cap", 0))), usd(adds)])
        left.append(_block("Bonus quarters", _table(["Quarter", "Families", "Rate", "Cap", "Adds"], rows, {2, 3, 4}),
                           note=f"What each adds over the better of this card's own rate and the {escape(blabel)}'s, on a quarter of the household's year of spend there. Only announced quarters; one already running counts its days left."))
    bens = cardworth.benefits(a, key)
    if bens:
        rows = []
        for bk, b in bens.items():
            chip = "<span class=chip>checked</span>" if b.get("verified") else "<span class='chip w'>unverified</span>"
            note = f"<details class=bnote><summary>why</summary><p class=note>{escape(str(b.get('note', '')))}</p></details>" if b.get("note") else ""
            rows.append([escape(_cap(bk.replace("_", " "))) + note, *(usd(float(b.get(lvl, 0.0))) for lvl in LEVELS), chip])
        tot = {lvl: sum(float(b.get(lvl, 0.0)) for b in bens.values()) for lvl in LEVELS}
        right.append(_block("Benefits you would pay for", _table(["Benefit", "Low", "Base", "High", ""], rows, {1, 2, 3}, ["Benefits", *(usd(tot[lvl]) for lvl in LEVELS), ""]),
                            note="What you would otherwise pay, not face value. assumptions.toml [card_worth.benefits]."))
    if key == bkey:
        form = _cpp_form(f"cards.{key}", float(card.get("default_rate", 0.0)) * 100, mults, v.labels)
        right.append(_block("A point is worth", f"<div class='panel pad'><p class=muted>The ¢/pt is the one number here that is a judgment, not a term. Changing it rescales every rate on the card and every other card's edge.</p>{form}</div>", bid="cpp"))
    body = [head, f"<div class='tiles pair'>{''.join(tiles)}</div>"]
    if left or right:
        body.append(f"<div class='cols even'><div class=stack>{''.join(left)}</div><div class=stack>{''.join(right)}</div></div>")
    if spec:
        credits = spec.get("credits", {})
        body.append(_block("Fee and credits by anniversary year", _table(["Year", "Fee", *(escape(c.get("label", k)) for k, c in credits.items()), "Coverage"], [[_span_dates(r[0]), *r[1:]] for r in credit_rows(s, key)], set(range(1, 2 + len(credits))), stack=True),
                           note="Used / face. A credit is net of its clawbacks. In-app credits (DashPass, Lyft) leave no row in the export."))
    return _page(v, "/cards", r.label, "".join(body))


def tier_picker(v: View, mkey: str) -> str:
    rules = v.s.rules
    m = rules["memberships"][mkey]
    tiers = m.get("tiers") or {}
    if len(tiers) < 2:
        return ""
    held = held_tier(rules, mkey)
    btns = []
    for t, tr in tiers.items():
        net = v.verdicts[headline_key(rules, mkey, tier=t)].net["base"]
        sub = "what you have" if t == held else ("refundable" if tr.get("refundable") else "")
        btns.append(f"<button type=button class=choice data-fill='{escape(t)}' aria-pressed={'true' if t == held else 'false'}>{escape(tier_label(m, t))}<small>{signed(net)} a year{' · ' + sub if sub else ''}</small></button>")
    d = next((d for d in v.s.decisions if d.key == f"tier:{mkey}"), None)
    inner = (f"<div class='choices seg' role=group aria-label=tier>{''.join(btns)}</div><input type=hidden data-file=rules data-section=held data-key={escape(mkey)}_tier data-type=string value='{escape(held)}'>"
             + (f"<p class=hint>{escape(d.sub)}</p>" if d else ""))
    return f"<div class=tiers><span class=ov>Tier</span>{_simple_form(inner, 'Save tier')}</div>"


def render_memberships(s: State, v: View | None = None) -> str:
    v = v or view(s)
    rules = s.rules
    rows = s.verdicts
    lo = min([0.0] + [r.low for r in rows])
    hi = max([0.0] + [r.high for r in rows])
    pad = max(40.0, (hi - lo) * 0.08)
    net, fees = membership_totals(rules, v.verdicts, rows)
    side = f"<div class=chips><span class=chip>{usd(fees)} in fees</span><span class=chip>{signed(net)} net</span></div>"
    cards = []
    for r in rows:
        mv = v.verdicts[headline_key(rules, r.membership)]
        word, cond = _split_verdict(r.verdict)
        tone = {"keep": "good", "drop": "crit"}.get(word, "neutral")
        lines = "".join(f"<div><span>{escape(label)}{f' <i>{escape(mark)}</i>' if mark else ''}</span><span class=mono>{signed(x)}</span></div>" for label, x, mark in net_lines(rules, s.assumptions, mv))
        hang = v.hang(r.membership)
        hang_html = f"<p class=hang>Hangs on question {v.numbers[hang.key]}:</p><a class=btn href='/todo#{escape(hang.key)}'>{escape(hang.title)}</a>" if hang else ""
        cards.append(
            f"<article class=vcard id='m-{escape(r.membership)}'><div class=head><div><h3>{escape(r.title)}</h3><p class=sub>{escape(r.sub)}</p></div><span class='badge {tone}'>{escape(word)}</span></div>"
            f"<p class=net>{signed(r.base)}<small>a year</small></p>"
            + (f"<p class=cond>{escape(_cap(cond))}</p>" if cond else "")
            + f"<div class=bar data-lo='{r.low:.0f}' data-base='{r.base:.0f}' data-hi='{r.high:.0f}'></div>"
            f"<div class=sum>{lines}</div>"
            f"<p class=why>{escape(r.why)} <span class=sub>{escape(r.foot)}.</span></p>{tier_picker(v, r.membership)}{hang_html}</article>"
        )
    bkey = rules["held"]["baseline_card"]
    blabel = rules["cards"][bkey].get("label", bkey)
    strip = []
    for mkey, m in rules.get("memberships", {}).items():
        cr = v.card(m.get("card", ""))
        if not cr:
            continue
        ml = m.get("label", mkey)
        if rules["cards"][cr.key].get("requires_membership", True):
            txt = f"Closes if {ml} lapses, so all of it counts toward {ml}."
        else:
            txt = f"{usd(cr.attributable.get(mkey, 0.0))} of it needs {ml} and counts above." + (f" Without {ml}: {cr.without}." if cr.without else "")
        strip.append(f"<a href='/cards/{escape(cr.key)}'><b>{escape(cr.label)} <span class='mono faint'>{signed(cr.edge)}/yr over the {escape(blabel)}{'' if cr.held else ', not held'}</span></b><span class=muted>{escape(txt)}</span></a>")
    body = [
        _title("Memberships", "Net a year = store-card cash back that needs the membership + its own reward + the perks you have valued − the fee. The bar runs low to high; the mark is the best guess.", side),
        f"<div class=vgrid>{''.join(cards)}</div>",
    ]
    if strip:
        body.append(_block("The store cards, apart from the memberships", f"<div class='panel strip'>{''.join(strip)}</div>"))
    return _page(v, "/memberships", "Memberships", "".join(body), (lo - pad, hi + pad))


def render_todo(s: State, v: View | None = None) -> str:
    v = v or view(s)
    rules = s.rules
    by = {g: [i for i in v.items if i.group == g] for g in GROUPS}
    open_items = by["ask"] + by["check"]
    counts: dict[str, int] = {}
    for it in v.items:
        for tag in item_tags(it):
            counts.setdefault(tag, 0)
            if it.group != "quiet":  # settled rows hide behind "Show settled"
                counts[tag] += 1

    def tag_label(t):
        kind, _, key = t.partition(":")
        if kind == "m":
            return _mlabel(rules, key)
        if kind == "c":
            return rules["cards"].get(key, {}).get("label", key)
        return t

    order = sorted(counts, key=lambda t: ({"m": 0, "c": 1}.get(t.partition(":")[0], 2), tag_label(t)))
    chips = "<button type=button class=fchip data-tag='' aria-pressed=true>All</button>" + "".join(
        f"<button type=button class=fchip data-tag='{escape(t)}' aria-pressed=false>{escape(tag_label(t))}<span class=mono>{counts[t]}</span></button>" for t in order
    ) + f"<span class=grow></span><button type=button class=fchip data-settled aria-pressed=false>Show settled<span class=mono>{len(by['quiet'])}</span></button>"
    rides = sum(i.weight for i in open_items)
    side = f"<span class=note>{len(open_items)} open · {len(by['quiet'])} settled" + (f" · {usd(rides)} a year rides on them" if rides else "") + "</span>"
    body = [
        _title("To do", "Every open input, biggest money first. Answering one writes it into the file it belongs to, and the row leaves on reload. Nothing here is a hand-kept list.", side),
        f"<div class=filters role=group aria-label='filter'>{chips}</div>",
    ]
    body.extend(render_now(it, s.today) for it in by["now"])
    ask = f"<div class=qlist>{''.join(render_question(v.numbers[it.key], it) for it in by['ask'])}</div>" if by["ask"] else "<p class=empty>Nothing to answer. Every verdict rests on inputs you've given.</p>"
    body.append(_block("Answer · only you know these", ask))
    if by["check"]:
        body.append(_block("Check · compare with the terms", f"<div class=qlist>{''.join(render_question(v.numbers[it.key], it) for it in by['check'])}</div>"))
    if by["wiki"]:  # read-only here, so after what the page can answer
        body.append(_block("Also open, in the to-do file", f"<ul class=plain>{''.join(render_plain(it) for it in by['wiki'])}</ul>", note="Tracked in the to-do file; nothing on this page changes them."))
    if by["quiet"]:
        settled = "".join(render_plain(it) for it in by["quiet"])
        body.append(f"<div class=settled hidden>{_block('Settled · nothing to do', f'<ul class=plain>{settled}</ul>', note='Settled, or moves less than $10 a year.')}</div>")
    return _page(v, "/todo", "To do", "".join(body))


CASH_YEAR = 12  # full months the averages and the total row run over
PHONE_CATS = 8  # categories a phone lists on Cash flow before "All"


def _account_label(t: ld.Txn, names: dict[str, set[str]]) -> str:
    """The account's name, with its last four where two accounts share it (the Chase cards)."""
    return f"{t.account} …{t.account_number}" if len(names.get(t.account, ())) > 1 and t.account_number else t.account


def _late_accounts(txns: list[ld.Txn]) -> list[tuple[str, date]]:
    """Accounts whose rows begin more than a month after the feed's first: the
    months before are missing them."""
    names: dict[str, set[str]] = {}
    for t in txns:
        names.setdefault(t.account, set()).add(t.account_number)
    first: dict[str, date] = {}
    for t in txns:
        k = _account_label(t, names)
        first[k] = min(first.get(k, t.date), t.date)
    if not first:
        return []
    start = min(first.values())
    return sorted(((k, d) for k, d in first.items() if (d - start).days > 31), key=lambda x: x[1])


_STATEMENTS: dict[Path, tuple[tuple, list]] = {}
_MONTHS: dict[Path | None, tuple] = {}


def _statements(d: Path, key: tuple) -> list:
    """``statements.load_dir(d)``, reparsed only when a PDF there changed (``key``: its ``_scan``)."""
    with _CACHE_LOCK:
        hit = _STATEMENTS.get(d)
    if hit and hit[0] == key:
        return hit[1]
    sts = statements.load_dir(d)[0]
    with _CACHE_LOCK:
        _STATEMENTS[d] = (key, sts)
    return sts


def cash_months(s: State, month: str | None = None) -> tuple[list[cashflow.Month], list[cashflow.Month], cashflow.Month]:
    """Every month on the feed, the last CASH_YEAR full ones, and the one
    ``month`` names (else the last full one). Cash flow and Budget share it."""
    lacks = {str(k) for k, a in s.rules.get("accounts", {}).items() if a.get("feed_lacks_rewards")}
    payees = s.rules.get("cashflow", {}).get("feed_card_payees", [])
    sdir = s.root / "data" / "statements" if s.root and lacks else None
    skey = _scan(sdir, "*.pdf") if sdir else ()
    key = (skey, frozenset(lacks), tuple(payees))
    with _CACHE_LOCK:
        hit = _MONTHS.get(s.root)
    # The same cached state's rows (by identity), the same statement PDFs, the same rules.
    if hit and hit[0] is s.txns and hit[1] == key:
        months = hit[2]
    else:
        sts = _statements(sdir, skey) if sdir else []
        months = cashflow.build(s.txns, sts, lacks, payees)
        with _CACHE_LOCK:
            _MONTHS[s.root] = (s.txns, key, months)
    now = cashflow.month_of(s.today)
    full = [m for m in months if m.key < now][-CASH_YEAR:]
    sel = {m.key: m for m in months}.get(month or "") or (full[-1] if full else months[-1])
    return months, full, sel


def render_cashflow(s: State, v: View | None = None, month: str | None = None) -> str:
    """Money in and out by month (budget-plan M4); ``month`` ("2026-08") picks
    the month the tiles and category table show, else the last full one."""
    v = v or view(s)
    if not s.txns:
        body = _title("Cash flow", "Cash flow reads the card and bank feed. This board was started on a Rocket Money export, which carries no income or transfers.")
        return _page(v, "/cashflow", "Cash flow", body)
    months, full, sel = cash_months(s, month)
    now = cashflow.month_of(s.today)
    n = len(full)
    avg = lambda f: sum(f(m) for m in full) / n if n else 0.0
    span = f"the last {n} full months" if n == CASH_YEAR else f"the {n} full month{'s' if n != 1 else ''} on file, under a year"
    late = _late_accounts(s.txns)
    missing = lambda key: [k for k, d in late if cashflow.month_of(d) > key]

    label = date.fromisoformat(sel.key + "-01").strftime("%B %Y") + (" to date" if sel.key == now else "")
    foot = lambda f, fmt=usd: f"{fmt(avg(f))} a month on average over {span}" if n else ""
    tiles = [
        _tile("In", usd(sel.total_in), "", escape(label), foot(lambda m: m.total_in)),
        _tile("Spent", usd(sel.total_spent), "", "refunds netted", foot(lambda m: m.total_spent)),
        _tile("Earned", usd(sel.earned), "", "card credits and rewards paid out", foot(lambda m: m.earned)),
        _tile("Net", signed(sel.net), _tone(sel.net), "in + earned − spent", foot(lambda m: m.net, signed)),
    ]

    cats = sorted(set(sel.spent) | {c for m in full for c in m.spent}, key=lambda c: (-sel.spent.get(c, 0.0), -avg(lambda m: m.spent.get(c, 0.0))))
    top = max([x for x in sel.spent.values()] + [1.0])
    bars = []
    for c in cats:
        x, a = sel.spent.get(c, 0.0), avg(lambda m, c=c: m.spent.get(c, 0.0))
        if round(x) == 0 and round(a) == 0:
            continue
        width = f"<i class=fill style='width:{max(0.0, x) / top * 100:.1f}%'></i>" if round(x) > 0 else ""
        bars.append(f"<div class=hbar title='{escape(_cap(c))}: {usd(x)} in {escape(label)}, {usd(a)} a month on average'><span><a href='{escape(_txn_href({}, cat=c, m=sel.key))}'>{escape(_cap(c))}</a> <span class='mono faint'>avg {usd(a)}</span></span><span class=track>{width}</span><span class=v>{usd(x)}</span></div>")
    more = f"<label class=allrows><input type=checkbox hidden>All {len(bars)} categories</label>" if len(bars) > PHONE_CATS else ""
    cat_html = f"<div class='panel hbars cap'>{''.join(bars) or '<p class=note>No spending this month.</p>'}{more}</div>"
    off = ", ".join(f"{escape(k)} {usd(x)}" for k, x in sorted(sel.off_feed.items(), key=lambda kv: -kv[1]))
    cat_note = (f"{_cap(cashflow.OFF_FEED)}: {off}. Loans and cards whose purchases the feed can't see, so the payment is the spending."
                if off else "")

    inc = "".join(f"<div><span>{escape(_cap(k))}</span><span class=mono>{usd(x)}</span></div>" for k, x in sorted(sel.income.items(), key=lambda kv: -kv[1]) if round(x))
    apps = f"Through PayPal, Venmo and Zelle: {usd(sel.apps_in)} came in and {usd(sel.apps_out)} went out. Neither is counted, because the feed can't see what it was for."
    in_html = f"<div class='panel pad'><div class=sum>{inc or '<span class=note>No income rows this month.</span>'}</div><p class=note>{apps}</p></div>"

    rows = []
    for m in reversed(months):
        mark = " <span class=faint>to date</span>" if m.key == now else ""
        gone = missing(m.key)
        if gone:
            mark += f" <span class=warnc title='no rows yet from {escape(', '.join(gone))}'>·</span>"
        name = f"<a href='/cashflow?m={m.key}'>{m.key}</a>" if m.key != sel.key else f"<b>{m.key}</b>"
        rows.append([name + mark, usd(m.total_in), usd(m.total_spent), usd(m.earned), f"<span class={_tone(m.net)}>{signed(m.net)}</span>"])
    total = [f"{n} full months", usd(sum(m.total_in for m in full)), usd(sum(m.total_spent for m in full)), usd(sum(m.earned for m in full)), signed(sum(m.net for m in full))] if n else None
    months_html = _table(["Month", "In", "Spent", "Earned", "Net"], rows, {1, 2, 3, 4}, total)
    late_note = " ".join(f"{escape(k)} rows begin {d:%B %Y}." for k, d in late)
    months_note = ("A dot marks a month missing an account: " + late_note if late else "") + " Pick a month to see its categories."

    lede = ("Money in and out by month, from the card and bank feed. Transfers between your own accounts and card payments are left out. "
            "Earned is what the cards paid back: statement credits, and OnePay's reward credits from its statement PDFs; points still on a card aren't counted.")
    body = [
        _title("Cash flow", lede, f"<div class=chips><span class=chip>{escape(label)}</span></div>"),
        f"<div class='tiles pair'>{''.join(tiles)}</div>",
        _block("Month by month", _chart([Col(_month_name(m.key)[:3], [("in", m.total_in), ("out", m.total_spent)], f"/cashflow?m={m.key}",
                                             f"{_month_name(m.key)} {m.key[:4]}: {usd(m.total_in)} in, {usd(m.total_spent)} spent, net {signed(m.net)}",
                                             m.key == now, m.key == sel.key) for m in months[-(CASH_YEAR + 1):]], height=180),
               note="<span class=key><i class=in></i>in</span> <span class=key><i class=out></i>spent</span> · pick a month to see it below."),
        f"<div class=cols>{_block(f'Spent by category, {label}', cat_html, (escape(_txn_href({}, m=sel.key)), 'Every row ›'), note=cat_note)}{_block(f'In, {label}', in_html)}</div>",
        _block("Every month", months_html, note=months_note.strip() + f" <a href='/budget?m={sel.key}'>Budget vs actual ›</a> · <a href='/recurring'>Recurring ›</a>"),
    ]
    return _page(v, "/cashflow", "Cash flow", "".join(body))


def render_budget(s: State, v: View | None = None, month: str | None = None) -> str:
    """Budget vs actual (budget-plan M5): each category's monthly budget from
    ``assumptions.toml`` ``[budget]`` against the month's spending, and the
    form that sets them. ``month`` as on Cash flow."""
    v = v or view(s)
    if not s.txns:
        return _page(v, "/budget", "Budget", _title("Budget", "The budget reads the card and bank feed. This board was started on a Rocket Money export."))
    months, full, sel = cash_months(s, month)
    plan_ = s.assumptions.get("budget", {})
    lines = budget.lines(sel, full, plan_)
    now = cashflow.month_of(s.today)
    gone = budget.elapsed(sel.key, s.today)
    label = date.fromisoformat(sel.key + "-01").strftime("%B %Y") + (" to date" if sel.key == now else "")
    n = len(full)
    span = f"the last {n} full months" if n == CASH_YEAR else f"the {n} full month{'s' if n != 1 else ''} on file, under a year"
    set_ = [ln for ln in lines if ln.budget > 0]
    unset = [ln for ln in lines if ln.budget <= 0]
    total_b = sum(ln.budget for ln in set_)
    total_a = sum(ln.actual for ln in set_)
    left = total_b - total_a
    pace = f"{gone:.0%} of the month gone" if gone is not None else ""
    tiles = [
        _tile("Budgeted", usd(total_b), "", f"{len(set_)} categor{'y' if len(set_) == 1 else 'ies'} with a budget", escape(label)),
        _tile("Spent in them", usd(total_a), "", "refunds netted", pace),
        _tile("Left" if round(left) >= 0 else "Over", usd(abs(left)), "pos" if round(left) >= 0 else "neg", "budgeted − spent"),
        _tile("Not budgeted", usd(sum(ln.actual for ln in unset)), "", f"spent in {sum(1 for ln in unset if round(ln.actual))} other categories"),
    ]

    def bar(ln: budget.Line) -> str:
        href = escape(_txn_href({}, cat=ln.category, m=sel.key))
        name = f"<a href='{href}'>{escape(_cap(ln.category))}</a>"
        if ln.budget <= 0:
            return f"<div><span>{name} <i>avg {usd(ln.average)}</i></span><span class=mono>{usd(ln.actual)}</span></div>"
        top = max(ln.budget, ln.actual) or 1.0
        over = ln.actual > ln.budget + 0.5
        fill = f"<i class='fill{' over' if over else ''}' style='width:{max(0.0, ln.actual) / top * 100:.1f}%'></i>"
        cap = f"<i class=cap style='left:{ln.budget / top * 100:.1f}%'></i>" if over else ""
        tick = f"<i class=pace style='left:{min(1.0, gone) * ln.budget / top * 100:.1f}%' title='where an even month would be today'></i>" if gone is not None else ""
        state = f"<span class=neg>{usd(-ln.left)} over</span>" if over else f"{usd(ln.left)} left"
        return (f"<div class=hbar title='{escape(_cap(ln.category))}: {usd(ln.actual)} of {usd(ln.budget)}, {usd(ln.average)} a month on average'>"
                f"<span>{name} <span class='mono faint'>{state}</span></span><span class=track>{fill}{cap}{tick}</span>"
                f"<span class=v>{usd(ln.actual)}<small class=faint> / {usd(ln.budget)}</small></span></div>")

    rows = f"<div class='panel hbars'>{''.join(bar(ln) for ln in set_)}</div>" if set_ else ""
    if unset:
        rows += (f"<div class='panel pad'>{'<p class=note>No budget set:</p>' if set_ else ''}<div class=sum>{''.join(bar(ln) for ln in unset)}</div></div>")
    note = ("The tick is where an even month would be today. " if gone is not None and set_ else "") + f"Averages over {span}."
    if not set_:
        note = f"Averages over {span}."

    inputs = []
    for ln in sorted(lines, key=lambda ln: (-ln.average, ln.category)):
        if not ln.key:
            continue
        sug = round(ln.average / 10) * 10
        spent = "" if set_ else f" · {usd(ln.actual)} in {_month_name(sel.key)[:3]}"  # with no budgets the form is the page's only category list
        use = f"<button type=button class=linkbtn data-use-avg=1 title='set it to {usd(sug)}'>avg {usd(ln.average)}</button>"
        inputs.append(f"<label class=bline><span>{escape(_cap(ln.category))} <small class='faint mono'>{use}{spent}</small></span>"
                      f"<input type=number inputmode=decimal min=0 step=1 data-file=assumptions data-section=budget data-key='{escape(ln.key)}' data-type=number "
                      f"data-avg='{sug:.0f}' value='{ln.budget:g}' aria-label='{escape(ln.category)} a month'></label>")
    missing = sorted(ln.category for ln in lines if not ln.key and ln.category != cat.UNCATEGORISED)  # fixed by categorising, not budgeting
    miss = (f"<p class=note>No <code>[budget]</code> key yet for {escape(', '.join(missing))}: add "
            + escape(", ".join(f"{budget.key_of(c)} = 0" for c in missing)) + " to <code>assumptions.toml</code> by hand.</p>") if missing else ""
    inner = (f"<p class=hint>Dollars a month. 0 means no budget. The average is over {escape(span)}, rounded to $10 when it fills a box: "
             "tap one category's average to use it there, or a button below for many at once. Nothing is written until you save.</p>"
             f"<div class=bgrid>{''.join(inputs)}</div>{miss}"
             "<p class=actions><button type=button class=btn data-fill-avg=empty>Fill empty ones with the average</button>"
             "<button type=button class=btn data-fill-avg=all>Set every one to the average</button></p>"
             "<details><summary>What gets saved</summary><pre class=preview></pre></details>")
    form = _simple_form(inner, "Save budgets", audit="set on the Budget page")

    mrows = []
    for m in list(reversed(months))[:CASH_YEAR + 1]:
        a = sum(m.spent.get(ln.category, 0.0) for ln in set_)
        name = f"<a href='/budget?m={m.key}'>{m.key}</a>" if m.key != sel.key else f"<b>{m.key}</b>"
        mark = " <span class=faint>to date</span>" if m.key == now else ""
        mrows.append([name + mark, usd(a), f"<span class={_tone(total_b - a)}>{signed(total_b - a)}</span>"])
    months_html = _table(["Month", "Spent", "Left"], mrows, {1, 2})
    shown = months[-(CASH_YEAR + 1):]
    if set_:
        spent_in = lambda m: sum(m.spent.get(ln.category, 0.0) for ln in set_)
        ref = (total_b, f"budget {usd(total_b)}")
    else:
        spent_in = lambda m: m.total_spent
        ref = (sum(m.total_spent for m in full) / n, f"average {usd(sum(m.total_spent for m in full) / n)}") if n else None
    chart = _block("Month by month", _chart([Col(_month_name(m.key)[:3], [("amt" + (" over" if set_ and spent_in(m) > total_b + 0.5 else ""), spent_in(m))],
                                                 f"/budget?m={m.key}", f"{_month_name(m.key)} {m.key[:4]}: {usd(spent_in(m))}", m.key == now, m.key == sel.key)
                                             for m in shown], ref, height=160),
                   note="Spent in the budgeted categories each month, against today's budgets." if set_ else "Everything spent each month, against the monthly average; set budgets to measure against them.")

    lede = ("Each category's monthly budget against what the month spent, refunds netted, the same figures as Cash flow. "
            "Budgets live in <code>assumptions.toml</code> <code>[budget]</code>; saving here writes them and never commits.")
    head = _title("Budget", lede, f"<div class=chips><span class=chip>{escape(label)}</span></div>")
    setter = _block("Set the budget", f"<div class='panel pad'>{form}</div>", bid="set")
    if not set_:
        # Nothing to compare yet: say so once, and put the form first.
        body = [head,
                (f"<div class=callout><p><b>No budgets yet.</b> <span class=muted>Each category below shows its monthly average and what it spent in {escape(label)}; "
                f"start from the averages and adjust.</span></p><a class='btn primary' href='#set'>Set budgets</a></div>"),
                chart, setter]
    else:
        body = [head, f"<div class='tiles pair'>{''.join(tiles)}</div>", chart,
                (f"<div class=cols>{_block(f'By category, {label}', rows, note=note)}"
                f"{_block('Every month', months_html, note='Spent in the budgeted categories, against today’s budgets.')}</div>"),
                setter]
    return _page(v, "/budget", "Budget", "".join(body))


def recurring_counts(s: State):
    """Rows the recurring page reads: money out, as Cash flow counts it."""
    payees = [re.compile(p, re.IGNORECASE) for p in s.rules.get("cashflow", {}).get("feed_card_payees", [])]
    return lambda t: feed.is_spend(t) or cashflow.off_feed(t, payees)


def fee_credit_rows(s: State, since: date) -> list[tuple[ld.Txn, str]]:
    """Membership and card fees and the credits against them since ``since``:
    the membership_fees family, fee rows, and what Cash flow counts as earned."""
    out = []
    for t in s.txns:
        if t.date < since:
            continue
        if t.family == cat.FEES and feed.is_spend(t):
            out.append((t, "membership fee"))
        elif t.kind == "fee":
            out.append((t, "account fee"))  # Plaid BANK_FEES: annual, interest, late
        elif t.budget_category == cashflow.EARNED or (t.transfer and t.account_type == "credit" and t.kind == "transfer" and t.amount < 0):
            out.append((t, "credit"))
    return sorted(out, key=lambda x: x[0].date, reverse=True)


def render_recurring(s: State, v: View | None = None) -> str:
    """Recurring charges (budget-plan M5): what bills on a cadence, what it
    costs a year, what stopped, and the fees and credits beside them."""
    v = v or view(s)
    if not s.txns:
        return _page(v, "/recurring", "Recurring", _title("Recurring", "Recurring charges read the card and bank feed. This board was started on a Rocket Money export."))
    found = recurring.find(s.txns, s.today, recurring_counts(s))
    live = [r for r in found if not r.stopped]
    year_ago = s.today - timedelta(days=365)
    gone = [r for r in found if r.stopped and r.last >= year_ago]
    yearly = sum(r.yearly for r in live)
    rises = [r for r in live if r.before is not None and r.amount > r.before]
    tiles = [
        _tile("Recurring", str(len(live)), "", "charges still running", f"{sum(1 for r in live if r.cadence == 'monthly')} of them monthly"),
        _tile("A year", usd(yearly), "", "at today's prices", f"{usd(yearly / 12)} a month"),
        _tile("Price changes", str(sum(1 for r in live if r.before is not None)), "warnc" if rises else "", "newest charge differs from the one before",
              f"{len(rises)} went up" if rises else ""),
        _tile("Stopped", str(len(gone)), "", "in the last year", "overdue by half an interval or more"),
    ]
    names: dict[str, set[str]] = {}
    for t in s.txns:
        names.setdefault(t.account, set()).add(t.account_number)
    soon = _coming(s, found, 30, more=False)

    def item(r: recurring.Recurring, amt: str, *l2: str) -> str:
        sep = "<span class=sep>·</span>"
        return (f"<a class=lrow href='{escape(_txn_href({}, q=r.key))}'><span class=l1><b>{escape(r.merchant)}</b><span class=amt>{amt}</span></span>"
                + "".join(f"<span class=l2>{sep.join(parts)}</span>" for parts in l2) + "</a>")

    def when(d: date) -> str:
        return f"{d:%-d %b %Y}"

    items = []
    for r in live:
        charge = f"{cents_usd(r.amount)} {escape(r.cadence)}"
        if r.before is not None:
            charge += f" <span class={'warnc' if r.amount > r.before else 'pos'}>(was {cents_usd(r.before)})</span>"
        due = f"<span class=warnc>due {when(r.next_due)}</span>" if r.next_due < s.today else f"next {when(r.next_due)}"
        items.append(item(r, f"{usd(r.yearly)}/yr", [charge, due], [escape(_account_label(r.rows[-1], names)), escape(r.category), f"{len(r.rows)} since {r.rows[0].date:%b %Y}"]))
    live_html = (f"<div class=list>{''.join(items)}<div class=lfoot><span>A year, all of them</span><span class=amt>{usd(yearly)}</span></div></div>"
                 if items else "<div class='panel pad'><p class=note>Nothing recurring found.</p></div>")
    gone_items = [item(r, cents_usd(r.amount), [escape(r.cadence), f"last {when(r.last)}", f"was {usd(r.yearly)}/yr"], [escape(_account_label(r.rows[-1], names)), escape(r.category)]) for r in gone]
    gone_html = f"<div class=list>{''.join(gone_items)}</div>" if gone_items else "<div class='panel pad'><p class=note>Nothing stopped in the last year.</p></div>"

    fc = fee_credit_rows(s, year_ago)
    fees = sum(t.amount for t, k in fc if k != "credit")
    credits = -sum(t.amount for t, k in fc if k == "credit")
    # A credit that posts in pieces (a travel credit, a clawback beside it) is one row with its count.
    groups: dict[object, tuple[str, list[ld.Txn]]] = {}
    for t, kind in fc:
        groups.setdefault((_merchant(t), kind, _account_label(t, names)) if kind == "credit" else id(t), (kind, []))[1].append(t)
    sep = "<span class=sep>·</span>"
    fc_items = ""
    for kind, ts in groups.values():
        t = ts[0]
        dates = when(t.date) if len(ts) == 1 else f"{len(ts)} rows, {min(u.date for u in ts):%-d %b %Y} – {when(max(u.date for u in ts))}"
        fc_items += (f"<a class=lrow href='{escape(_txn_href({'show': 'all'}, q=_merchant(t).lower()))}'><span class=l1><b>{escape(_merchant(t))}</b>"
                     f"<span class=amt>{_money(sum(u.amount for u in ts))}</span></span>"
                     f"<span class=l2>{dates}{sep}{escape(kind)}{sep}{escape(_account_label(t, names))}</span></a>")
    fc_html = (f"<div class=list>{fc_items}<div class=lfoot><span>Fees {usd(fees)} · credits {usd(credits)}</span><span class='amt {_tone(credits - fees)}'>{signed(credits - fees)}</span></div></div>"
               if fc else "<div class='panel pad'><p class=note>No fees or credits in the last year.</p></div>")

    lede = ("Charges from one merchant at a similar amount on a steady interval, three times or more: bills, subscriptions, "
            "insurance, and payments off the feed. A regular visit to a shop doesn't count unless it's most of what that shop charges.")
    method = (f"A price change within {recurring.AMOUNT_TOL:.0%} stays one charge; a bigger one starts a new line, so a raise past it shows as one stopped and one new. "
              "A yearly fee needs three years of rows to show here, so fees are listed below as well.")
    body = [
        _title("Recurring", lede),
        f"<div class='tiles pair'>{''.join(tiles)}</div>",
        soon,
        _block("Still running", live_html, note=method),
        (f"<div class=cols>{_block('Stopped in the last year', gone_html)}"
        f"{_block('Fees and the credits against them', fc_html, note='Membership fees, account fees (annual, interest, late), and the statement and reward credits the cards paid. Each card page nets them by anniversary year.')}</div>"),
    ]
    return _page(v, "/recurring", "Recurring", "".join(body))


def cents_usd(x: float) -> str:
    return f"${x:,.2f}"


TXN_LIMIT = 200  # rows the table shows; the filter narrows the rest
TXN_FILTERS = ("q", "fam", "cat", "acct", "m", "show", "hand")


def _txn_href(f: dict[str, str], **change) -> str:
    """This page's URL with the filter ``f``, some keys changed (None drops one)."""
    q = {k: v for k, v in {**f, **change}.items() if v and k in (*TXN_FILTERS, "id")}
    return "/transactions" + ("?" + urlencode(q) if q else "")


def _merchant(t: ld.Txn) -> str:
    return cat.merchant_of(t)


def _amount_cell(t: ld.Txn) -> str:
    """Money out plain, money in with a plus, to the cent."""
    return _money(t.amount)


def _money(x: float) -> str:
    return f"<span class=mono>${x:,.2f}</span>" if x >= 0 else f"<span class='mono pos'>+${-x:,.2f}</span>"


def txn_filter(txns: list[ld.Txn], f: dict[str, str], names: dict[str, set[str]], hand: dict[str, dict],
               payees: list[re.Pattern] = ()) -> list[ld.Txn]:
    """The rows the filter ``f`` keeps, newest first. ``q`` matches the merchant
    text and the source's own category; ``show`` is spend (default), all, or not
    spend (transfers, payments, income). ``cat`` of ``cashflow.OFF_FEED`` keeps
    the payments Cash flow counts there (``payees`` are the feed's own cards);
    they are transfers, so ``show`` doesn't apply to them."""
    needle = f.get("q", "").strip().lower()
    show = f.get("show") or "spend"
    off = f.get("cat") == cashflow.OFF_FEED
    out = []
    for t in txns:
        spend = feed.is_spend(t)
        if off:
            if not cashflow.off_feed(t, payees):
                continue
        elif (show == "spend" and not spend) or (show == "other" and spend) or (f.get("cat") and t.budget_category != f["cat"]):
            continue
        if needle and needle not in t.match_text and needle not in t.category.lower():
            continue
        if f.get("fam") and (t.family or cat.OTHER) != f["fam"]:
            continue
        if f.get("acct") and _account_label(t, names) != f["acct"]:
            continue
        if f.get("m") and cashflow.month_of(t.date) != f["m"]:
            continue
        if f.get("hand") and t.txn_id not in hand:
            continue
        out.append(t)
    out.sort(key=lambda t: (t.date, t.txn_id), reverse=True)
    return out


def ruled(t: ld.Txn, rules: dict) -> tuple[str, str]:
    """The family and category the rules give ``t``, before any hand override."""
    u = copy.copy(t)
    cat.categorize([u], cat.build_families(rules))
    return u.family or cat.OTHER, cat.category_of(u, rules, cat.category_rules(rules))


def merchant_checks(txns: list[ld.Txn], top: int = 3) -> tuple[list[tuple[str, float, int]], dict[str, tuple[float, list[tuple[str, float]]]]]:
    """The categoriser's two checks over spend rows: the biggest uncategorised
    merchants (name, spend, rows), and per family its total and top merchants."""
    left: dict[str, list[float]] = {}
    fams: dict[str, dict[str, float]] = {}
    for t in txns:
        m = _merchant(t)
        if t.budget_category == cat.UNCATEGORISED:
            left.setdefault(m, []).append(t.amount)
        by = fams.setdefault(t.family or cat.OTHER, {})
        by[m] = by.get(m, 0.0) + t.amount
    worst = sorted(((m, sum(a), len(a)) for m, a in left.items()), key=lambda r: -r[1])
    per = {k: (sum(v.values()), sorted(v.items(), key=lambda kv: -kv[1])[:top]) for k, v in fams.items()}
    return worst, dict(sorted(per.items(), key=lambda kv: -kv[1][0]))


def _rule_hint(t: ld.Txn, family: str, category: str, rules: dict) -> str:
    """What to add to rules.toml so every row from this merchant gets the same
    labels. The board never adds keys, so this is for pasting by hand."""
    pat = json.dumps(re.escape(_merchant(t).lower()).replace("\\ ", " "))  # a TOML basic string
    lines = [f"[[categories.rules]]\ncategory = {json.dumps(category)}\npatterns = [{pat}]"]
    if family not in (cat.OTHER, cat.FEES) and family in rules.get("families", {}):
        lines.append(f"# or, for the family, add {pat} to [families.{family}] patterns")
    return "\n".join(lines)


def _edit_panel(s: State, t: ld.Txn, f: dict[str, str], hand: dict[str, dict], names: dict[str, set[str]], categories: list[str],
                by_merchant: dict[str, dict] | None = None) -> str:
    rules = s.rules
    rfam, rcat = ruled(t, rules)
    o = hand.get(t.txn_id, {})
    mo = (by_merchant or {}).get(_merchant(t).lower(), {})
    fams = [(cat.OTHER, "Other"), (cat.FEES, "Membership fees")] + [(k, v.get("label", k)) for k, v in rules.get("families", {}).items()]
    fam_opts = "".join(f"<option value='{escape(k)}'{' selected' if k == (t.family or cat.OTHER) else ''}>{escape(lbl)}</option>" for k, lbl in sorted(fams, key=lambda kv: kv[1].lower()))
    cat_opts = "".join(f"<option value='{escape(c)}'>" for c in categories)
    same = sum(1 for x in s.txns if _merchant(x).lower() == _merchant(t).lower() and feed.is_spend(x))
    facts = [
        ("Date", f"{t.date}" + (f", posted {t.posted}" if t.posted and t.posted != t.date else "")),
        ("Account", escape(_account_label(t, names))),
        ("Amount", _amount_cell(t)),
        ("Bank's text", escape(t.name) + (f" <span class=faint>· {escape(t.category)}</span>" if t.category else "")),
        ("The rules say", f"{escape(rfam)} / {escape(rcat)}"),
    ]
    if mo:
        facts.append((f"Set for every {escape(_merchant(t))} row", escape(" / ".join(f"{k} {v}" for k, v in sorted(mo.items())))))
    if o:
        facts.append(("Set by hand for this row", escape(" / ".join(f"{k} {v}" for k, v in sorted(o.items())))))
    facts_html = "".join(f"<div><span>{k}</span><span>{v}</span></div>" for k, v in facts)
    if not feed.is_spend(t):
        form = "<p class=note>Transfers, card payments and income aren't spend, so they carry no family or category to change.</p>"
    else:
        clear = ("<button type=button class=btn data-clear=row>This row back to the rules</button>" if o else "") + (
            f"<button type=button class=btn data-clear=merchant>Every {escape(_merchant(t))} row back to the rules</button>" if mo else "")
        form = (
            f"<form class=ovr data-id='{escape(t.txn_id)}' data-fam='{escape(t.family or cat.OTHER)}' data-cat='{escape(t.budget_category)}'>"
            f"<div class=ovrgrid><label>Family <span class=hint>what a card earns on</span><select name=family>{fam_opts}</select></label>"
            f"<label>Category <span class=hint>what the money was for</span><input name=category list=cats value='{escape(t.budget_category)}' autocomplete=off></label></div>"
            f"<datalist id=cats>{cat_opts}</datalist>"
            f"<div class=actions><button type=submit class='btn primary' data-scope=merchant>Save for every {escape(_merchant(t))} row ({same})</button>"
            f"<button type=submit class=btn data-scope=row>Save for this row only</button>{clear}<span class=status role=status></span></div>"
            f"<p class=note>Every row from the merchant, past and future, goes in <code>data/merchants.json</code>; one row goes in <code>data/overrides.json</code> and beats the merchant's. "
            f"Both beat every rule and neither is committed.</p>"
            f"<details><summary>Or make it a rule in <code>rules.toml</code></summary>"
            f"<p class=note>The board never adds keys to <code>rules.toml</code>; paste this there by hand, then the hand settings can go.</p>"
            f"<pre class=preview>{escape(_rule_hint(t, t.family or cat.OTHER, t.budget_category, rules))}</pre>"
            f"<p><a href='{escape(_txn_href({}, q=_merchant(t).lower()))}'>Every row from {escape(_merchant(t))} ›</a></p></details></form>"
        )
    inner = f"<div class='panel pad'><div class=sum>{facts_html}</div>{form}</div>"
    close = escape(_txn_href(f, id=None))
    # On a phone this is a bottom sheet over the list; the scrim behind it closes it.
    return f"<a class=scrim href='{close}' aria-label='Close'></a>" + _block(_merchant(t), inner, (close, "Close ×"), bid="edit", cls="sheet")


def render_transactions(s: State, v: View | None = None, f: dict[str, str] | None = None) -> str:
    """Every feed row, searchable, with hand recategorising and the
    categoriser's checks (budget-plan M5). ``f`` is the query: the filters in
    ``TXN_FILTERS``, and ``id`` for the row being edited."""
    v = v or view(s)
    f = {k: val for k, val in (f or {}).items() if val}
    if not s.txns:
        body = _title("Transactions", "Transactions reads the card and bank feed. This board was started on a Rocket Money export.")
        return _page(v, "/transactions", "Transactions", body)
    hand = cat.load_overrides(s.root / feed.OVERRIDES) if s.root else {}
    by_merchant = cat.load_overrides(s.root / feed.MERCHANTS) if s.root else {}
    names: dict[str, set[str]] = {}
    for t in s.txns:
        names.setdefault(t.account, set()).add(t.account_number)
    spend = [t for t in s.txns if feed.is_spend(t)]
    categories = sorted({t.budget_category for t in spend} | set(s.rules.get("categories", {}).get("families", {}).values()) | set(s.rules.get("categories", {}).get("plaid", {}).values()) | {cashflow.OFF_FEED})
    payees = [re.compile(p, re.IGNORECASE) for p in s.rules.get("cashflow", {}).get("feed_card_payees", [])]
    rows = txn_filter(s.txns, f, names, hand, payees)

    def select(name: str, first: str, opts: list[tuple[str, str]]) -> str:
        o = "".join(f"<option value='{escape(k)}'{' selected' if f.get(name) == k else ''}>{escape(lbl)}</option>" for k, lbl in opts)
        return f"<select name={name} aria-label='{escape(first)}'><option value=''>{escape(first)}</option>{o}</select>"

    labels = {cat.OTHER: "Other", cat.FEES: "Membership fees", **{k: x.get("label", k) for k, x in s.rules.get("families", {}).items()}}
    fams = sorted({t.family or cat.OTHER for t in spend}, key=lambda k: labels.get(k, k).lower())
    months = sorted({cashflow.month_of(t.date) for t in s.txns}, reverse=True)
    accts = sorted({_account_label(t, names) for t in s.txns})
    shows = [("all", "Every row"), ("other", "Transfers and income only")]
    on = sum(1 for k in ("m", "cat", "fam", "acct", "show") if f.get(k))
    filters = (
        "<form class=tfilter method=get action='/transactions'>"
        f"<div class=tsearch><input type=search name=q value='{escape(f.get('q', ''))}' placeholder='Search merchants' aria-label='search'>"
        "<button type=submit class=btn>Search</button>"
        + ("<a class=btn href='/transactions'>Clear</a>" if any(f.get(k) for k in TXN_FILTERS) else "") + "</div>"
        f"<details class=tfx{' open' if on else ''}><summary>Filters{f' · {on} on' if on else ''}</summary><div class=tsel>"
        + select("m", "Any month", [(m, m) for m in months])
        + select("cat", "Any category", [(c, _cap(c)) for c in categories])
        + select("fam", "Any family", [(k, labels.get(k, k)) for k in fams])
        + select("acct", "Any account", [(a, a) for a in accts])
        + select("show", "Spend only", shows)
        + "</div></details>"
        + ("<input type=hidden name=hand value=1>" if f.get("hand") else "")
        + "</form>"
    )

    out_ = sum(t.amount for t in rows if t.amount > 0)
    in_ = -sum(t.amount for t in rows if t.amount < 0)
    shown = rows[:TXN_LIMIT]
    items, day = [], None
    for t in shown:
        if t.date != day:
            day = t.date
            items.append(f"<div class=lday>{t.date:%a %-d %b %Y}</div>")
        mark = " <span class='badge neutral' title='set by hand'>hand</span>" if t.txn_id in hand else ""
        what = (f"{escape(labels.get(t.family or cat.OTHER, t.family or ''))} <span class=sep>/</span> {escape(t.budget_category)}{mark}"
                if feed.is_spend(t) else escape(t.kind))
        items.append(f"<a class='lrow{' on' if f.get('id') == t.txn_id else ''}' href='{escape(_txn_href(f, id=t.txn_id))}#edit'>"
                     f"<span class=l1><b>{escape(_merchant(t))}</b><span class=amt>{_amount_cell(t)}</span></span>"
                     f"<span class=l2>{escape(_account_label(t, names))}<span class=sep>·</span>{what}</span></a>")
    more = f"Showing the newest {TXN_LIMIT} of {len(rows):,}; narrow the filter to see the rest. " if len(rows) > TXN_LIMIT else ""
    table = f"<div class=list>{''.join(items)}</div>" if items else "<div class='panel pad'><p class=note>No rows match.</p></div>"
    hand_link = (f"<a href='{escape(_txn_href(f, hand=None))}'>Every row</a>" if f.get("hand")
                 else f"<a href='{escape(_txn_href(f, hand='1'))}'>Only rows set by hand ({len(hand)})</a>")
    count = f"{len(rows):,} row{'s' if len(rows) != 1 else ''}: {usd(out_)} out" + (f", {usd(in_)} in" if round(in_) else "")

    body = [_title("Transactions", "Every row from the card and bank feed. Pick one to change its family (what a card earns on) or its category (what the money was for).",
                   f"<div class=chips><span class=chip>{escape(count)}</span></div>"), filters]
    sel = next((t for t in s.txns if t.txn_id == f.get("id")), None) if f.get("id") else None
    if sel:
        body.append(_edit_panel(s, sel, f, hand, names, categories, by_merchant))
    body.append(_block("Rows", table, note=more + hand_link, cls="rows"))

    win = [t for t in model.in_window(spend, s.window)]
    worst, per = merchant_checks(win)
    span = f"{s.window.start} to {s.window.end}"
    left = "".join(f"<div><span><a href='{escape(_txn_href({}, cat=cat.UNCATEGORISED, q=m.lower()))}'>{escape(m)}</a> <i>{n}</i></span><span class=mono>{usd(x)}</span></div>" for m, x, n in worst[:10])
    left_html = f"<div class='panel pad'><div class=sum>{left or '<span class=note>Nothing uncategorised.</span>'}</div></div>"
    fam_list = "<div class=list>" + "".join(
        f"<a class=lrow href='{escape(_txn_href({}, fam=k))}'><span class=l1><b>{escape(labels.get(k, k))}</b><span class=amt>{usd(tot)}</span></span>"
        f"<span class=l2>{escape(', '.join(m for m, _ in tops))}</span></a>" for k, (tot, tops) in per.items()) + "</div>"
    left_note = (f"{len(worst)} merchant{'s' if len(worst) != 1 else ''}, {usd(sum(x for _, x, _ in worst))}, over {span}. "
                 "Pick a row to set the category for every row from its merchant, or add a <code>[[categories.rules]]</code> pattern in <code>rules.toml</code>.") if worst else f"Over {span}."
    body.append(f"<div class=cols>{_block('Biggest uncategorised merchants', left_html, note=left_note)}"
                f"{_block('Top merchants per family', fam_list, note=f'Over {span}. A merchant in the wrong family moves card and membership figures; fix it with a pattern under its [families] entry.')}</div>")
    return _page(v, "/transactions", "Transactions", "".join(body))


STALE_BALANCE = 3  # days before a Plaid balance's date is called out; the sync runs daily
STALE_APPLE = 35  # the Apple Card's is as fresh as the newest Wallet export, a month apart


def _acct_label(a: networth.Account, names: dict[str, set[str]]) -> str:
    """The Transactions page's name for the account (``_account_label``)."""
    return f"{a.name} …{a.mask}" if len(names.get(a.name, ())) > 1 and a.mask else a.name


def _nw_chart(points: list[networth.Point], today: date, height: int = 150) -> str:
    """Month-end net worth as columns up or down from zero, today's last."""
    vals = [p.net for p in points]
    hi, lo = max(vals + [0.0]), min(vals + [0.0])
    span = (hi - lo) or 1.0
    at = lambda x: (x - lo) / span * 100
    cols = []
    for p in points:
        now = p.day == today
        low, high = sorted((at(0.0), at(p.net)))
        tip = f"{'Today' if now else f'{p.day:%b %-d, %Y}'}: {usd(p.net)} · {usd(p.assets)} held, {usd(p.debts)} owed"
        cols.append(f"<span class='c{' part' if now else ''}' title='{escape(tip)}'><i class='{'in' if p.net >= 0 else 'over'}' "
                    f"style='bottom:{low:.1f}%;height:{max(high - low, 0.5):.1f}%'></i></span>")
    zero = f"<i class=zero style='bottom:{at(0.0):.1f}%'></i>" if lo < 0 < hi else ""
    labels = "".join(f"<span>{'Now' if p.day == today else f'{p.day:%b}'}</span>" for p in points)
    return f"<div class=chart><div class='plot nw' style='height:{height}px'>{''.join(cols)}{zero}</div><div class=xl>{labels}</div></div>"


def _balance_line(a: networth.Account, today: date) -> str:
    """What else the account's balance comes with: available, limit, statement, due date, and how fresh it is."""
    bits = [escape(a.institution)] if a.institution and a.institution.lower() not in a.name.lower() else []
    if a.asset and a.available is not None and round(a.available) != round(a.balance):
        bits.append(f"{usd(a.available)} available")
    if a.limit:
        bits.append(f"{a.used:.0%} of {usd(a.limit)} limit")
    if a.statement is not None and not a.asset:
        bits.append(f"statement {usd(a.statement)}")
    if a.due and a.minimum:
        late = a.due < today
        bits.append(f"<span class={'warnc' if late else 'muted'}>{usd(a.minimum)} minimum due {a.due:%b %-d}</span>")
    stale = (today - a.as_of).days > (STALE_APPLE if a.key == networth.APPLE_KEY else STALE_BALANCE)
    when = "today" if a.as_of == today else f"{a.as_of:%b %-d}"
    bits.append(f"<span class={'warnc' if stale else 'faint'}>as of {when}" + (f" · from its {escape(a.source)}" if a.source != "Plaid" else "") + "</span>")
    return "<span class=sep>·</span>".join(bits)


def _balances(s: State) -> str:
    """The overview's account strip: net worth, then the cash and card balances it nets."""
    nw = s.worth_now
    if not nw or not nw.accounts:
        return ""
    past = [p for p in nw.history if p.day < s.today]
    since = f"{signed(nw.net - past[-1].net)} since {past[-1].day:%b %-d}" if past else "month-end history starts next month"
    cells = [_tile("Net worth", usd(nw.net), _tone(nw.net), escape(since), "", "/accounts"),
             _tile("Cash", usd(nw.assets), "", f"in {sum(1 for a in nw.accounts if a.asset)} bank accounts", "", "/accounts#g-depository")]
    cards = [a for a in nw.accounts if a.type == "credit"]
    if cards:
        limit = sum(a.limit or 0 for a in cards)
        used = f" · {sum(a.balance for a in cards if a.limit) / limit:.0%} of {usd(limit)} limits" if limit else ""
        cells.append(_tile("Card balances", usd(sum(a.balance for a in cards)), "", f"on {len(cards)} cards{used}", "", "/accounts#g-credit"))
    return f"<section class='block ov-acct'><div class='bhead rule'><h2 class=h>Accounts</h2><a href='/accounts'>Accounts ›</a></div><div class=acct>{''.join(cells)}</div></section>"


def render_accounts(s: State, v: View | None = None) -> str:
    """Every account's balance and net worth by month-end (Rocket Money's Net Worth page)."""
    v = v or view(s)
    nw = s.worth_now
    if not nw or not nw.accounts:
        why = ("This board was started on a Rocket Money export, which carries no balances: start it on the card feed." if s.export
               else "No balances yet: run <code>penny plaid sync</code>.")
        return _page(v, "/accounts", "Accounts", _title("Accounts", why))
    lede = "Net worth is what the bank accounts hold less what the cards owe, each at its newest balance."
    body = [_title("Accounts", lede)]
    past = [p for p in nw.history if p.day < s.today]
    change = f"<p class=muted>{signed(nw.net - past[-1].net)} since {past[-1].day:%b %-d} · {usd(nw.assets)} held, {usd(nw.debts)} owed</p>" if past else \
        f"<p class=muted>{usd(nw.assets)} held, {usd(nw.debts)} owed</p>"
    hero = (f"<section class=hero><div class=bhead><h2 class=ov>Net worth</h2></div>"
            f"<span class='big{' neg' if round(nw.net) < 0 else ''}'>{usd(nw.net)}</span>{change}</section>")
    if len(nw.history) > 1:
        worked = [p for p in nw.history if p.worked]
        note = (f"Month-ends before {nw.first_snapshot:%b %-d, %Y}, the first saved balance, are worked back from the rows: "
                "a statement plus the rows since for a card that has them, today's balance less the rows since for the rest."
                if worked and nw.first_snapshot else "")
        chart = _block("Net worth by month", _nw_chart(nw.history, s.today), note=note)
    else:
        chart = _block("Net worth by month", "<p class=empty>A month-end appears here once one has passed with balances on file.</p>")
    body.append(f"<div class='cols rev'>{hero}{chart}</div>")
    names: dict[str, set[str]] = {}
    for t in s.txns:
        names.setdefault(t.account, set()).add(t.account_number)
    groups = dict(networth.GROUPS)
    for kind in [*groups, *sorted({a.type for a in nw.accounts} - set(groups))]:
        mine = [a for a in nw.accounts if a.type == kind]
        if not mine:
            continue
        rows = "".join(
            f"<a class=lrow href='{escape(_txn_href({'show': 'all'}, acct=_acct_label(a, names)))}'><span class=l1><b>{escape(a.name)}"
            + (f" <span class=faint>…{escape(a.mask)}</span>" if a.mask else "")
            + f"</b><span class=amt>{usd(a.balance)}</span></span><span class=l2>{_balance_line(a, s.today)}</span></a>"
            for a in mine)
        total = sum(a.balance for a in mine)
        foot = f"<div class=lfoot><span>{'Held' if mine[0].asset else 'Owed'}</span><span class=amt>{usd(total)}</span></div>"
        body.append(_block(groups.get(kind, _cap(kind)), f"<div class=list>{rows}{foot}</div>", bid=f"g-{escape(kind)}"))
    body.append("<p class=note>A card's balance is what it owes today, pending purchases aside. The Apple Card has no bank feed: "
                "its balance is its newest statement plus the Wallet rows since, so it is as fresh as the newest <code>penny import apple</code>. "
                "<a href='/checks'>Balance checks ›</a></p>")
    return _page(v, "/accounts", "Accounts", "".join(body))


MARKS = {True: ("tie", "good"), False: ("miss", "crit"), None: ("not checked", "neutral")}


def render_checks(s: State, v: View | None = None) -> str:
    """Balance checks per account (budget-plan M4): what `penny check` prints,
    the gaps with the rows nearest to explaining them, and the Apple Card import."""
    v = v or view(s)
    c = s.checks or check.Checks([], [], [])
    lede = ("Each account's rows must carry one known balance to the next: a statement or a snapshot, plus the rows between, "
            "equals the next balance to the cent. A miss shows the gap and the rows nearest to explaining it.")
    counts = {k: sum(1 for r in c.results if r.ok is k) for k in MARKS}
    side = "<div class=chips>" + "".join(f"<span class=chip>{n} {MARKS[k][0]}{'es' if k is False and n != 1 else ''}</span>" for k, n in counts.items() if n) + "</div>"
    body = [_title("Checks", lede, side)]
    due = check.apple_due(c.statements, s.today)
    if due:
        last, nxt, late = due
        text = (f"The newest statement imported ends {last}. The next closes {nxt}: export its CSV from Wallet, "
                f"download the PDF, and run <code>penny import apple</code>" + (" — it's overdue." if late else f" after {nxt}."))
        body.append(_block("Apple Card import", f"<div class='panel pad'><p class={'warnc' if late else 'muted'}>{text}</p></div>"))
    if not c.results:
        body.append(_block("Accounts", "<div class='panel pad'><p class=note>No balances yet: run <code>penny plaid sync</code>.</p></div>"))
    else:
        rows = []
        for r in c.results:
            word, tone = MARKS[r.ok]
            extra = "".join(f"<span class='l3 faint'>{escape(t['date'])} {t['amount']:,.2f} {escape(t.get('name', ''))}</span>" for t in r.near + r.inferred)
            rows.append(f"<div class=lrow><span class=l1><b>{escape(r.account)}</b><span class='badge {tone}'>{word}</span></span>"
                        f"<span class=l2>through {r.through or '—'}</span><span class=l3>{escape(r.detail)}</span>{extra}</div>")
        body.append(_block("Accounts", f"<div class=list>{''.join(rows)}</div>",
                           note=f"An account not tied for {check.STALE_DAYS} days, or whose newest check misses, is flagged on every page."))
    if c.errors:
        body.append(_block("Statements that don't parse", "<div class='panel pad'>" + "".join(f"<p class=neg>{escape(e)}</p>" for e in c.errors) + "</div>"))
    return _page(v, "/checks", "Checks", "".join(body))


def render_data(s: State, v: View | None = None) -> str:
    v = v or view(s)
    body = _title("Data", "Where the numbers come from: the export, the captures that stand in for what it drops, the fees it shows and what has been saved here." if s.export else
                   "Where the numbers come from: the card feed, the captures that cross-check it, the fees it shows and what has been saved here.") + f"<aside class=datagrid>{render_rail(s)}</aside>"
    return _page(v, "/data", "Data", body)


def render_more(s: State) -> str:
    v = view(s)
    rows = "".join(f"<a class=lrow href='{h}'><span class=l1><b>{t}</b><span class=muted>›</span></span><span class=l2>{d}</span></a>" for h, t, d in (
        ("/checks", "Checks", "Every account tied to its statements and snapshots, to the cent"),
        ("/data", "Data", "Where the numbers come from, and what has been saved here")))
    return _page(v, "/more", "More", _title("More") + f"<div class=list>{rows}</div><p class=note>{_meta(s)}</p>")


def render_not_found(s: State, path: str) -> str:
    v = view(s)
    return _page(v, "/cards", "Not found", _title("Not found", f"No card <code>{escape(path.rsplit('/', 1)[-1])}</code> on the board. <a href='/cards'>Every card ›</a>"))


def render_error(e: Exception) -> str:
    return (
        _head("penny")
        + f"<body><div class=app><header class=side><a class=brand href='/'>{LOGO}</a></header><div class=content><main><p class=lede>The page could not read its sources: <code>{escape(type(e).__name__)}: {escape(str(e))}</code></p></main></div></div></body></html>"
    )


# --------------------------------------------------------------------------
# Server


ROUTES = {"/": render_page, "/index.html": render_page, "/accounts": render_accounts, "/cashflow": render_cashflow, "/cards": render_cards, "/memberships": render_memberships, "/todo": render_todo, "/transactions": render_transactions, "/budget": render_budget, "/recurring": render_recurring, "/checks": render_checks, "/data": render_data, "/more": render_more}
CARD_PATH = re.compile(r"/cards/([\w-]+)")


LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _host_port(value: str) -> tuple[str | None, int | None]:
    """A Host (or Origin netloc) as (lower-case name, port or None); (None, None) if malformed."""
    try:
        u = urlsplit("//" + value)
        name, port = u.hostname, u.port
    except ValueError:
        return None, None
    if not name or u.username is not None or u.path or u.query:
        return None, None
    return name.rstrip("."), port


def _is_ip(name: str) -> bool:
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True


def host_allowed(value: str | None, bound: str, port: int, names: frozenset[str] = frozenset()) -> bool:
    """Whether a request's Host header names this server. A DNS-rebinding page
    arrives under its own domain, so only the bound address, localhost and
    ``names`` (the Tailscale MagicDNS names) pass, on the bound port or none
    (a proxy in front). Bound to every interface, any IP literal passes: a
    rebinding attack needs a name."""
    if not value:
        return False
    name, p = _host_port(value)
    if name is None or (p is not None and p != port):
        return False
    if name in LOCAL_HOSTS or name in names or name == bound.lower():
        return True
    return bound in ("", "0.0.0.0", "::") and _is_ip(name)


def origin_allowed(origin: str | None, host: str | None) -> bool:
    """A cross-site form or fetch carries its own Origin; the board's own
    fetches carry one whose host:port is the Host they were sent to."""
    if origin is None:
        return True  # not a browser, or a same-origin request that sent none
    u = urlsplit(origin)
    return u.scheme in ("http", "https") and bool(host) and u.netloc.lower() == host.lower()


def make_handler(cfg: Config, today=date.today, names=()):
    """``names``: extra Host names the board answers to (its MagicDNS names)."""
    names = frozenset(n.lower().rstrip(".") for n in names)

    class Handler(BaseHTTPRequestHandler):
        def _send(self, body: bytes, ctype: str, code: int = 200):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200):
            self._send(json.dumps(obj, default=str).encode(), "application/json", code)

        def _host_ok(self) -> bool:
            bound, port = self.server.server_address[:2]
            return host_allowed(self.headers.get("Host"), str(bound), port, names)

        def do_GET(self):
            if not self._host_ok():
                self.close_connection = True
                return self._send(b"forbidden host", "text/plain", 403)
            try:
                self._get()
            except Exception:  # noqa: BLE001 -- the server's boundary: logged, answered 500
                _log(f"board: GET {urlsplit(self.path).path} failed\n{traceback.format_exc()}")
                self._send(b"server error", "text/plain", 500)

        def _get(self):
            url = urlsplit(self.path)
            path = url.path
            page, card = ROUTES.get(path), CARD_PATH.fullmatch(path)
            if page or card:
                try:
                    s = load_state(cfg, today())
                    if page in (render_cashflow, render_budget):
                        page = partial(page, month=(parse_qs(url.query).get("m") or [None])[0])
                    elif page is render_transactions:
                        q = {k: vs[0] for k, vs in parse_qs(url.query).items() if vs}
                        page = lambda s: render_transactions(s, f=q)
                    html, code = (page(s), 200) if page else (render_card(s, card.group(1)), 200)
                    if html is None:
                        html, code = render_not_found(s, path), 404
                except Exception as e:  # noqa: BLE001 -- show the failure on the phone, not a dropped connection
                    _log(f"board: GET {path} failed\n{traceback.format_exc()}")
                    html, code = render_error(e), 500
                return self._send(html.encode(), "text/html; charset=utf-8", code)
            if path in STATIC_FILES:
                return self._send((STATIC / path[1:]).read_bytes(), STATIC_FILES[path])
            m = DOC_PATH.fullmatch(path)
            if m and (cfg.root / "docs" / m.group(1)).is_file():
                return self._send((cfg.root / "docs" / m.group(1)).read_bytes(), "text/plain; charset=utf-8")
            self._send(b"not found", "text/plain", 404)

        def _refuse(self, msg: str, code: int):
            self.close_connection = True  # the body, if any, is left unread
            return self._json({"error": msg}, code)

        def do_POST(self):
            if not self._host_ok():
                return self._refuse("forbidden host", 403)
            path = urlsplit(self.path).path
            if path not in ("/record", "/override", "/import/apple"):
                return self._refuse("not found", 404)
            if not origin_allowed(self.headers.get("Origin"), self.headers.get("Host")):
                return self._refuse("cross-origin request refused", 403)
            if path == "/import/apple":
                return self._import_apple()
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                return self._refuse("send Content-Type: application/json", 415)
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._refuse("bad Content-Length", 400)
            if n < 0:
                return self._refuse("bad Content-Length", 400)
            if n > MAX_BODY:
                return self._refuse("request too large", 413)
            try:
                payload = json.loads(self.rfile.read(n) or b"null")
            except ValueError as e:
                return self._json({"error": f"not JSON: {e}"}, 400)
            try:
                res = record(cfg, payload, today()) if path == "/record" else record_override(cfg, payload, today=today())
            except Refused as e:
                return self._json({"error": str(e)}, 400)
            except Exception as e:  # noqa: BLE001 -- the server's boundary: logged, answered 500
                _log(f"board: POST {path} failed\n{traceback.format_exc()}")
                return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
            self._json(res)

        def _import_apple(self):
            """``penny import apple`` for one file POSTed as the raw body, which is
            what an iOS Shortcut's Get Contents of URL sends. No Content-Type
            check: a Shortcut's File body carries whatever iOS guesses, or none.
            A cross-site page still can't post here, because its Origin fails
            the check above; a Shortcut sends no Origin."""
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._refuse("bad Content-Length", 400)
            if n <= 0:
                return self._refuse("send the file as the request body", 400)
            if n > MAX_UPLOAD:
                return self._refuse("file too large", 413)
            data = self.rfile.read(n)
            try:
                lines = apple.import_upload(data, cfg.root / "data" / "apple")
            except Exception as e:  # noqa: BLE001 -- the server's boundary: logged, answered 500
                _log(f"board: POST /import/apple failed\n{traceback.format_exc()}")
                return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
            _log(f"board: /import/apple: {'; '.join(lines)}")
            ok = all(": skipped" not in line for line in lines)
            self._json({"result": lines}, 200 if ok else 400)

        def log_request(self, code="-", size="-"):
            # Every POST and every non-2xx goes to the journal; a page view that worked is noise.
            try:
                ok = 200 <= int(code) < 300
            except (TypeError, ValueError):
                ok = False
            if self.command == "POST" or not ok:
                super().log_request(code, size)

        def log_message(self, fmt, *args):
            _log(f"board: {self.address_string()} {fmt % args}")

    return Handler


def tailscale_names() -> set[str]:
    """This machine's MagicDNS names, full and short (``myhost``), when Tailscale has them."""
    try:
        r = subprocess.run(["tailscale", "status", "--self", "--json"], capture_output=True, text=True, timeout=5, check=False)
        name = json.loads(r.stdout)["Self"]["DNSName"].rstrip(".").lower()
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError, AttributeError):
        return set()
    return {name, name.split(".")[0]} if name else set()


def tailscale_ip() -> str | None:
    try:
        r = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=3, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = r.stdout.split()
    return out[0] if r.returncode == 0 and out else None


LOOPBACK = "127.0.0.1"


def bind_address(host: str | None = None, tailscale: bool = False) -> tuple[str, set[str]]:
    """Where the board listens, and the extra Host names it answers to.

    Loopback by default: the board edits config files and shows every
    transaction, and has no login. ``tailscale`` binds this machine's
    Tailscale IPv4 and answers to its MagicDNS names; ``host`` binds anything
    else, with a warning on stderr when that isn't loopback."""
    if tailscale:
        ip = tailscale_ip()
        if not ip:
            raise SystemExit("--tailscale: no Tailscale IPv4 (is tailscaled up and logged in?)")
        return ip, tailscale_names()
    if host and host not in LOCAL_HOSTS:
        print(f"warning: binding {host}. The board has no login and can edit your config; anyone who can reach "
              f"it can read every transaction. Localhost is the supported setup.", file=sys.stderr, flush=True)
    return host or LOOPBACK, set()


def serve(root: Path, export: Path | None = None, host: str | None = None, port: int = 8766, todo_file: Path | None = None, rules: Path | None = None, assumptions: Path | None = None, tailscale: bool = False) -> None:
    cfg = Config(root, rules or root / "rules.toml", assumptions or root / "assumptions.toml", todo_file, export)
    host, names = bind_address(host, tailscale)
    srv = ThreadingHTTPServer((host, port), make_handler(cfg, names=names))
    print(f"board on http://{host}:{port}/  → records into {cfg.rules.name} / {cfg.assumptions.name}, audit in {root / 'data' / 'board-decisions.jsonl'}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()

"""Plain-text report."""

from __future__ import annotations

from datetime import date, datetime

from . import cardworth
from .categorize import OTHER
from .load import Txn
from .model import (
    LEVELS,
    Verdict,
    Window,
    annualized,
    card_edges_at_points,
    evaluate_at_points,
    fee_mismatches,
    point_values,
)


def money(x: float) -> str:
    return f"${x:,.0f}"


def table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]

    def line(cells: list[str]) -> str:
        return "  ".join(c.rjust(w) if i else c.ljust(w) for i, (c, w) in enumerate(zip(cells, widths)))

    return "\n".join([line(headers), line(["-" * w for w in widths]), *(line(r) for r in rows)])


def spend_table(spend_window: dict[str, float], annualize: float, families: dict[str, str]) -> str:
    rows = []
    for fam, amt in sorted(spend_window.items(), key=lambda kv: -kv[1]):
        rows.append([families.get(fam, fam), money(amt), money(amt * annualize)])
    return table(["family", "in window", "annualised"], rows)


def verdict_table(verdicts: list[Verdict], rules: dict) -> str:
    rows = []
    for v in verdicts:
        m = rules["memberships"][v.membership]
        tier = "" if v.tier == "standard" else f" {v.tier}"
        card = " + card" if v.with_card else ""
        rows.append(
            [
                f"{m.get('label', v.membership)}{tier}{card}",
                money(v.fee),
                money(v.family_spend),
                money(v.edge),
                money(v.attributable),
                money(v.reward),
                money(v.perks["base"]),
                *(money(v.net[l]) for l in LEVELS),
                money(v.break_even) if v.break_even is not None else "n/a",
            ]
        )
    return table(
        ["option", "fee", "own spend", "card edge", "from mbrship", "reward", "perks", "net low", "net base", "net high", "break-even"],
        rows,
    )


def edge_detail(verdicts: list[Verdict], families: dict[str, str]) -> str:
    out = []
    seen = set()
    for v in verdicts:
        if not v.with_card or v.membership in seen:
            continue
        seen.add(v.membership)
        parts = ", ".join(f"{families.get(f, f)} {money(x)}" for f, x in sorted(v.edge_detail.items(), key=lambda kv: -kv[1]))
        out.append(f"  {v.membership} card edge by family: {parts or 'none'}")
        parts = ", ".join(f"{families.get(f, f)} {money(x)}" for f, x in sorted(v.attributable_detail.items(), key=lambda kv: -kv[1]))
        out.append(f"  {v.membership}   of which from the membership: {parts or 'none'}")
    return "\n".join(out)


def cents(x: float) -> str:
    return f"{round(x * 100, 2):g}¢"


def _option(v: Verdict, rules: dict) -> str:
    m = rules["memberships"][v.membership]
    tier = "" if v.tier == "standard" else f" {v.tier}"
    return f"{m.get('label', v.membership)}{tier}{' + card' if v.with_card else ''}"


def point_range_block(rules: dict, assumptions: dict, spend_window: dict[str, float], annualize: float, day: date | None = None) -> str:
    """Card edges and membership nets with the baseline card's ¢/pt at low /
    base / high, so a verdict that flips inside the range is marked. Card
    edges count rotating quarters still to come on ``day`` (default today)."""
    bkey = rules["held"]["baseline_card"]
    pv = point_values(rules, assumptions)
    src = f"assumptions.toml [point_value.{bkey}]"
    head = f"Baseline ¢/pt range ({bkey}: {' / '.join(cents(pv[l]) for l in LEVELS)} low/base/high; base = its default_rate, low/high from {src})"
    if pv["low"] == pv["base"] == pv["high"]:
        return f"{head}\n  no range set: add low/high to {src} to see where verdicts flip."
    lines = [head]
    if not pv["low"] <= pv["base"] <= pv["high"]:
        lines.append(f"  RANGE STALE: base {cents(pv['base'])} is outside {cents(pv['low'])}–{cents(pv['high'])}; re-set {src}.")
    cols = [f"@{cents(pv[l])}" for l in LEVELS]
    rows = []
    for ckey, e in card_edges_at_points(rules, assumptions, spend_window, annualize, day or datetime.now().astimezone().date()).items():
        vals = [e[l] for l in LEVELS]
        flips = max(vals) >= 0.5 and min(vals) < 0.5  # an edge is never negative; it vanishes
        rows.append([rules["cards"][ckey].get("label", ckey), *(money(x) for x in vals), "FLIPS" if flips else ""])
    lines += ["", "  Card edge over the baseline (should you carry the card), annual $; rotating quarters count only once announced", table(["card", *cols, "flips?"], rows)]
    at = evaluate_at_points(rules, assumptions, spend_window, annualize)
    rows = []
    for i, v in enumerate(at["base"]):
        vals = [at[l][i].net["base"] for l in LEVELS]
        flips = min(vals) < 0 <= max(vals)
        rows.append([_option(v, rules), *(money(x) for x in vals), "FLIPS" if flips else ""])
    lines += ["", "  Membership net (perks at base) as the ¢/pt moves, annual $", table(["option", *cols, "flips?"], rows)]
    lines.append("  A higher ¢/pt makes the baseline stronger, so every other card's edge shrinks as it rises.")
    return "\n".join(lines)


def signed(x: float) -> str:
    r = round(x)
    return f"-${-r:,}" if r < 0 else f"+${r:,}"


def credits_block(rules: dict, txns: list[Txn], *, first: date, last: date) -> str:
    """Fee charged and each credit used, per anniversary year, for every card
    with a ``[card_credits.<card>]`` table. ``txns`` must come from
    ``cardworth.load_for_credits`` so excluded categories are still read."""
    out = []
    for ckey, spec in rules.get("card_credits", {}).items():
        credits = spec.get("credits", {})
        rows = []
        own = cardworth.card_first_row(txns, rules, ckey)
        start = max(first, own) if own else first
        for y in cardworth.anniversary_years(txns, rules, ckey, first=start, last=last):
            cells = []
            for k, c in credits.items():
                used = money(y.credits[k])
                cells.append(f"{used} / {money(float(c['face']))}" if "face" in c else used)
            rows.append([f"{y.start} → {y.end}", money(y.fee), *cells, y.partial or ""])
        label = rules.get("cards", {}).get(ckey, {}).get("label", ckey)
        out += [
            f"{label}: fee and credits by anniversary year (rules.toml [card_credits.{ckey}]; matched by descriptor, excluded categories included)",
            table(["year", "fee", *(c.get("label", k) for k, c in credits.items()), "coverage"], rows),
            "  A credit is net of its clawbacks; \"used / face\" where a face value is set. In-app credits (DashPass, Lyft) leave no row.",
        ]
    return "\n".join(out)


def card_worth_block(rules: dict, assumptions: dict, spend_window: dict[str, float], annualize: float) -> str:
    """Worth of each ``[card_worth].alternatives`` card on the spend the first
    one carries, and the first one's margin over each of the rest."""
    spend = annualized(spend_window, annualize)
    ws = cardworth.card_worth(rules, assumptions, spend)
    if not ws:
        return ""
    first = ws[0]
    lines = [
        f"Card worth (annual $; separate from the membership nets): worth = earn + benefits − annual_fee, on the {money(sum(spend.values()))}/yr {first.label} carries",
        table(
            ["card", "fee", *(f"earn {l}" for l in LEVELS), *(f"benefits {l}" for l in LEVELS), *(f"worth {l}" for l in LEVELS)],
            [[w.label, money(w.fee), *(money(w.earn[l]) for l in LEVELS), *(money(w.benefits[l]) for l in LEVELS), *(signed(w.worth[l]) for l in LEVELS)] for w in ws],
        ),
    ]
    for w in ws[1:]:
        d = [first.worth[l] - w.worth[l] for l in LEVELS]
        flips = min(d) < 0 <= max(d)
        lines.append(f"  {first.label} over {w.label}: {' / '.join(signed(x) for x in d)} low/base/high{'  FLIPS' if flips else ''}")
    for w in ws:
        if not w.fee_set:
            lines.append(f"  NO FEE SET for {w.label}: add annual_fee to rules.toml [cards.{w.card}]; counted as $0.")
    lines.append("  earn: points at the card's ¢/pt range (assumptions.toml [point_value]); only rules.toml families earn bonus rates.")
    unv = cardworth.unverified_benefits(assumptions)
    if unv:
        lines.append("  UNVERIFIED benefit values in assumptions.toml [card_worth.benefits] (check them yourself): " + ", ".join(unv))
    return "\n".join(lines)


def fees_block(found: dict[str, list[Txn]]) -> str:
    if not found:
        return "  none matched — check fee_patterns in rules.toml against `penny unmatched`"
    lines = []
    for m, hits in found.items():
        lines.append(f"  {m}: " + "; ".join(f"{t.date} {money(t.amount)} ({t.name})" for t in hits))
    return "\n".join(lines)


def fee_warnings(found: dict[str, list[Txn]], rules: dict) -> list[str]:
    return [
        f"  FEE CHANGED? {m}: charged {money(t.amount)} on {t.date}, list fee {money(fee)} — update rules.toml"
        for m, t, fee in fee_mismatches(found, rules)
    ]


def render(
    w: Window,
    n_txns: int,
    spend_window: dict[str, float],
    families: dict[str, str],
    verdicts: list[Verdict],
    fees: dict[str, list[Txn]],
    rules: dict,
    unverified: list[str],
    point_range: str = "",
    extra: list[str] | None = None,
) -> str:
    a = w.annualize
    parts = [
        f"Window {w.start} → {w.end} ({w.days} days, ×{a:.2f} to annualise), {n_txns} transactions",
        *([f"Partial: the data covers {w.days} of the {w.asked} days asked for, so every annual figure is a ×{a:.2f} scale-up, not a measurement."] if w.partial else []),
        "",
        "Spend by family",
        spend_table(spend_window, a, families),
        "",
        "Membership fees seen in window",
        fees_block(fees),
        *fee_warnings(fees, rules),
        "",
        f"Verdicts (annual $; baseline card = {rules['held']['baseline_card']})",
        verdict_table(verdicts, rules),
        "",
        "  card edge = this card over the baseline card (should you carry the card).",
        "  from mbrship = the part of that edge the membership causes (the Amazon Visa still pays 3% without Prime).",
        "  net = from mbrship + reward + perks − fee. Perks low/base/high from assumptions.toml.",
        "  break-even = annual spend on the membership's own families at which net(base) = 0, current mix.",
        edge_detail(verdicts, families),
    ]
    if point_range:
        parts += ["", point_range]
    for block in extra or []:
        if block:
            parts += ["", block]
    if unverified:
        parts += ["", "UNVERIFIED rates/fees in rules.toml (confirm before trusting): " + ", ".join(unverified)]
    other = spend_window.get(OTHER, 0.0)
    total = sum(spend_window.values()) or 1.0
    parts += ["", f"  {other / total:.0%} of spend is outside every family (rent, bills, everything else). Fine unless `penny unmatched` shows gas, dining or a store in it."]
    return "\n".join(parts)

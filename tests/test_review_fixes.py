"""Review fixes, 2026-09-27: rotating cap part-way through a quarter, break-even
past the reward cap, Costco fee refunds and prorated upgrades, anchored
merchant patterns, statement credits in cash flow, rules validation, fsync on
save, and the shared helpers. Synthetic rows only; rules.toml where named."""

import os
import tomllib
from datetime import date
from pathlib import Path

import pytest

from penny import cardworth, cashflow, home, model
from penny import categorize as cat
from penny import tomledit as te
from penny.load import Txn

INSTANCE = Path(__file__).parent / "fixtures" / "home"  # a test instance, read over penny/defaults


def _toml(p):
    with open(p, "rb") as f:
        return tomllib.load(f)


def _real():
    return home.load_rules(INSTANCE / "rules.toml")


# --- 1. rotating quarter: what is left of the cap --------------------------

def _flex():
    r = model.resolve_points({
        "held": {"baseline_card": "pts"},
        "cards": {
            "pts": {"default_rate": 0.02, "rates": {"restaurants": 0.06}},
            "flex": {"points_of": "pts", "points": {"default": 1, "restaurants": 3},
                     "quarters": [{"start": "2026-10-01", "end": "2026-12-31",
                                   "families": ["restaurants", "groceries"], "points": 5, "cap": 1500}]},
        },
    })
    return r["cards"]["flex"], r["cards"]["pts"]


def test_capped_quarter_part_way_counts_only_the_cap_left():
    flex, pts = _flex()
    # $3,000 a quarter against a $1,500 cap: by Nov 15, 45 of 92 days are gone
    # and at an even pace $1,467 of the cap is used, leaving $33, not the 47/92
    # of a full cap (~$766) the old formula gave.
    spend = {"restaurants": 8000.0, "groceries": 4000.0}
    left = 47 / 92
    remaining = min(3000 * left, 1500 - 3000 * (1 - left))
    e, per = model.rotating_edge(flex, pts, spend, date(2026, 11, 15))
    k = remaining / 3000
    assert per["restaurants"] == pytest.approx(0.04 * 2000 * k)
    assert per["groceries"] == pytest.approx(0.08 * 1000 * k)
    assert e == pytest.approx((0.04 * 2000 + 0.08 * 1000) * k)
    # Late in the quarter the cap is gone.
    assert model.rotating_edge(flex, pts, spend, date(2026, 12, 20)) == (0.0, {})


def test_uncapped_share_part_way_is_still_the_days_left():
    flex, pts = _flex()
    spend = {"restaurants": 2000.0, "groceries": 1000.0}  # $750 a quarter, under the cap
    full = model.rotating_edge(flex, pts, spend, date(2026, 10, 1))[0]
    assert model.rotating_edge(flex, pts, spend, date(2026, 11, 15))[0] == pytest.approx(full * 47 / 92)


# --- 4. break-even past the reward cap -------------------------------------

def _club(cap):
    return {
        "held": {"baseline_card": "flat"},
        "cards": {
            "flat": {"default_rate": 0.01},
            "club_card": {"default_rate": 0.01, "requires_membership": True, "rates": {"club": 0.03}},
        },
        "memberships": {"club": {"families": ["club"], "card": "club_card",
                                 "tiers": {"exec": {"fee": 100, "reward_rate": 0.02, "reward_cap": cap}}}},
    }


def test_break_even_past_the_cap_rides_the_card_edge():
    vs = {v.with_card: v for v in model.evaluate(_club(10), {}, {"club": 1000.0}, 1.0)}
    # Reward caps at $10 ($500 of spend); past that only the card's 2 points grow:
    # 0.02 * S + 10 = 100 -> S = 4,500.
    assert vs[True].break_even == pytest.approx(4500.0)
    at = next(v for v in model.evaluate(_club(10), {}, {"club": 4500.0}, 1.0) if v.with_card)
    assert at.net["base"] == pytest.approx(0.0)
    # Without the card nothing grows past the cap: no break-even.
    assert vs[False].break_even is None


def test_break_even_under_the_cap_is_unchanged():
    v = next(v for v in model.evaluate(_club(1000), {}, {"club": 1000.0}, 1.0) if v.with_card)
    assert v.break_even == pytest.approx(100 / 0.04)


# --- 2. Costco fees: refunds and the prorated upgrade ----------------------

def _costco(amount, name="Costco", desc="Costco"):
    return Txn(date(2026, 9, 23), name, amount, "GENERAL_MERCHANDISE_SUPERSTORES", description=desc, mcc="5300")


def test_prorated_upgrades_are_twelfths_of_the_tier_gap():
    m = _real()["memberships"]["costco"]
    ups = cat.prorated_upgrades(m)
    assert 37.92 in ups and 5.42 in ups and 59.58 in ups  # 7, 1 and 11 twelfths of $65
    assert len(ups) == 11 and 65.0 not in ups
    assert cat.prorated_upgrades({**m, "upgrade_prorated": False}) == []


def test_upgrade_charge_and_fee_refunds_are_fees():
    fams = cat.build_families(_real())
    tx = [_costco(37.92), _costco(-65.0), _costco(-37.92), _costco(37.93), _costco(250.0), _costco(-12.0)]
    got = [t.family for t in cat.categorize(tx, fams)]
    assert got == [cat.FEES, cat.FEES, cat.FEES, "costco", "costco", "costco"]


def test_upgrade_charge_is_an_observed_fee_and_not_a_mismatch():
    rules = _real()
    found = model.observed_fees([_costco(37.92), _costco(-65.0)], rules)
    assert [t.amount for t in found["costco"]] == [37.92]  # refunds are not charges
    assert model.fee_mismatches(found, rules) == []
    # A near miss is still flagged against the list fee.
    assert [f for _, _, f in model.fee_mismatches({"costco": [_costco(37.0)]}, rules)] == [65.0]


# --- 3. anchored merchant patterns -----------------------------------------

@pytest.mark.parametrize("name,family", [
    ("GitHub Copilot", "other"),
    ("PILOT_00123", "gas"),
    ("Pilot Travel Center", "gas"),
    ("Gloves Outlet", "other"),
    ("LOVE'S #0421", "gas"),
    ("CHEVRON0301234", "gas"),
    ("EXXONMOBIL 1234", "gas"),
    ("Circle Kitchen", "other"),
    ("CIRCLE K #2710", "gas"),
    ("Marathonfoto", "other"),
    ("Cheba Hut", "other"),
    ("H-E-B #44", "groceries"),
    ("HEB ONLINE", "groceries"),
    ("Rinaldi's Deli", "other"),
    ("ALDI 72015", "groceries"),
    ("WAL-MART #1234", "walmart_store"),
    ("COSTCO WHSE #123 LAS VEGAS", "costco"),
    ("COSTCO GAS #123", "costco_gas"),
    ("Murphyville Books", "other"),
    ("MURPHY USA 7123", "gas"),
])
def test_patterns_are_anchored(name, family):
    t = Txn(date(2026, 9, 1), name, 20.0, "")
    assert cat.categorize([t], cat.build_families(_real()))[0].family == family


def test_an_unanchored_miss_falls_back_to_the_mcc():
    t = Txn(date(2026, 9, 1), "GitHub Copilot", 10.0, "", mcc="5734")
    assert cat.categorize([t], cat.build_families(_real()))[0].family == "other"


# --- 5. cash flow: only statement credits into a card are earned -----------

def _cf(amount, pfc, name="x", kind="transfer"):
    return Txn(date=date(2026, 8, 7), name=name, amount=amount, category=pfc, kind=kind,
               budget_category="transfer", account_type="credit", transfer=True)


def test_payments_and_account_transfers_into_a_card_are_not_earned():
    rows = [
        _cf(-300, "TRANSFER_IN_ACCOUNT_TRANSFER"),  # from checking
        _cf(-200, "TRANSFER_IN_OTHER_TRANSFER_IN", "ONLINE TRANSFER FROM CHK ...1234"),
        _cf(-150, "TRANSFER_IN_OTHER_TRANSFER_IN", "AUTOPAY 260807"),
        _cf(-50, "TRANSFER_IN_OTHER_TRANSFER_IN", "THE EDIT $500/YEAR"),  # a statement credit
        _cf(-25, "TRANSFER_IN_OTHER_TRANSFER_IN", "CL *Chase Travel CREDIT"),
    ]
    (aug,) = cashflow.build(rows)
    assert aug.earned == 75
    assert [cashflow.statement_credit(t) for t in rows] == [False, False, False, True, True]


# --- 6. rules validation ---------------------------------------------------

def test_real_rules_and_assumptions_validate():
    model.validate_rules(_real(), _toml(INSTANCE / "assumptions.toml"))


@pytest.mark.parametrize("mutate,needle", [
    (lambda r: r["cards"]["prime_visa"]["rates"].update(amazonn=0.05), "amazonn"),
    (lambda r: r["held"].update(baseline_card="nope"), "baseline_card"),
    (lambda r: r["memberships"]["costco"]["families"].append("costcoo"), "costcoo"),
    (lambda r: r["memberships"]["costco"]["tiers"]["executive"].update(reward_families=["gass"]), "gass"),
    (lambda r: r["memberships"]["prime"].update(card="prime_vsa"), "prime_vsa"),
    (lambda r: r["cards"]["freedom_flex"]["quarters"][0]["families"].append("dinning"), "dinning"),
    (lambda r: r["cards"]["freedom_flex"].update(points_of="sapphire"), "sapphire"),
    (lambda r: r["cards"]["freedom_flex"]["points"].update(drugstore=3), "drugstore"),
    (lambda r: r["accounts"].update({"9999": {"card": "ghost"}}), "ghost"),
    (lambda r: r["categories"]["families"].update(resturants="dining"), "resturants"),
    (lambda r: r["families"]["amazon"].update(membership="primee"), "primee"),
    (lambda r: r["held"].update(costco_tier="platinum"), "platinum"),
])
def test_a_bad_reference_fails_at_load(mutate, needle):
    r = _real()
    mutate(r)
    with pytest.raises(ValueError, match=needle):
        model.resolve_points(r)


def test_alternatives_must_be_cards():
    with pytest.raises(ValueError, match="nocard"):
        model.validate_rules(_real(), {"card_worth": {"alternatives": ["sapphire_preferred", "nocard"]}})


# --- 8. durable save ---------------------------------------------------------

def test_write_edits_fsyncs_file_and_directory(tmp_path, monkeypatch):
    p = tmp_path / "rules.toml"
    p.write_text("[held]\nbaseline_card = \"a\"\n")
    synced = []
    real = os.fsync

    def spy(fd):
        synced.append(os.path.realpath(f"/proc/self/fd/{fd}"))
        real(fd)

    monkeypatch.setattr(te.fsio.os, "fsync", spy)
    te.write_edits(p, lambda t: te.set_key(t, "held", "baseline_card", "b"))
    assert len(synced) == 2
    assert synced[0].startswith(str(tmp_path / ".rules.toml."))  # the temp file, before the rename
    assert synced[1] == str(tmp_path)


# --- 9. shared helpers -------------------------------------------------------

def test_annualized_scales_every_family():
    assert model.annualized({"a": 10.0, "b": -2.0}, 3.0) == {"a": 30.0, "b": -6.0}


def test_card_accounts_helper():
    rules = {"cards": {"a": {"accounts": [1234, "5678"]}, "b": {}}}
    assert cardworth.card_accounts(rules, "a") == ["1234", "5678"]
    assert cardworth.card_accounts(rules, "b") is None
    assert cardworth.card_accounts(rules, "missing") is None


def test_credit_patterns_compile_once():
    spec = {"fee_patterns": ["annual membership fee"], "credits": {"t": {"patterns": ["travel credit"]}}}
    a, b = cardworth.credit_patterns(spec), cardworth.credit_patterns(spec)
    assert all(x is y for x, y in zip(a, b))
    t = Txn(date(2026, 1, 1), "TRAVEL CREDIT", -5.0, "")
    assert cardworth.is_credit_row(t, spec) and cardworth.is_credit_row(t, spec, a)


def test_category_of_with_precomputed_primaries_agrees():
    rules = _real()
    crules = cat.category_rules(rules)
    prim = cat.plaid_primaries(rules)
    assert prim.index("FOOD_AND_DRINK_GROCERIES") < prim.index("FOOD_AND_DRINK")
    for c in ("FOOD_AND_DRINK_GROCERIES_X", "FOOD_AND_DRINK_FAST_FOOD", "RENT_AND_UTILITIES_RENT", "NOPE"):
        t = Txn(date(2026, 1, 1), "x", 1.0, c)
        assert cat.category_of(t, rules, crules, prim) == cat.category_of(t, rules, crules)

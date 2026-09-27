import tomllib
from datetime import date
from pathlib import Path

import pytest
from feedfix import install

from penny import categorize as cat
from penny import feed, home, model
from penny import load as ld

INSTANCE = Path(__file__).parent / "fixtures" / "home"  # a test instance, read over penny/defaults


def _toml(p):
    with open(p, "rb") as f:
        return tomllib.load(f)


def _rules():
    r = home.load_rules(INSTANCE / "rules.toml")
    r["held"]["baseline_card"] = "baseline"
    return r


def test_card_edge_counts_only_where_card_wins():
    rules = _rules()
    spend = {"amazon": 1000.0, "gas": 500.0, "other": 2000.0}
    edge, detail = model.card_edge(rules["cards"]["prime_visa"], rules["cards"]["baseline"], spend)
    assert detail == {"amazon": pytest.approx(30.0)}  # 5% - 2% on amazon only
    assert edge == pytest.approx(30.0)


def test_evaluate_prime_net_and_break_even():
    rules = _rules()
    assumptions = {"prime": {"shipping": {"low": 0, "base": 50, "high": 100}}}
    spend = {"amazon": 2000.0, "other": 1000.0}
    verdicts = {(v.membership, v.tier, v.with_card): v for v in model.evaluate(rules, assumptions, spend, 1.0)}
    v = verdicts[("prime", "standard", True)]
    assert v.edge == pytest.approx(60.0)  # card: 5% vs 2% baseline on $2000
    assert v.attributable == pytest.approx(40.0)  # membership: 5% vs the 3% the card pays without Prime
    assert v.net["base"] == pytest.approx(40 + 50 - 139)
    assert v.net["high"] == pytest.approx(40 + 100 - 139)
    # net(base)=0 when 0.02*x + 50 = 139
    assert v.break_even == pytest.approx(89 / 0.02)
    nocard = verdicts[("prime", "standard", False)]
    assert nocard.edge == 0 and nocard.break_even is None


def test_costco_executive_reward_skips_gas_and_caps():
    """Costco's 2% excludes gasoline, so `reward_families` is ["costco"] only.
    Was 2% of warehouse + gas until 2026-09-20; see rules.toml for the source."""
    rules = _rules()
    spend = {"costco": 5000.0, "costco_gas": 1000.0}
    vs = {(v.tier, v.with_card): v for v in model.evaluate(rules, {}, spend, 1.0) if v.membership == "costco"}
    ex = vs[("executive", False)]
    assert ex.family_spend == pytest.approx(6000.0)  # gas is still the membership's own spend
    assert ex.reward == pytest.approx(100.0)  # but only the warehouse $5000 earns 2%
    assert ex.net["base"] == pytest.approx(100 - 130)
    # Break-even scales both families; only 5/6 of the scaled spend earns 2%.
    assert ex.break_even == pytest.approx(130 / (0.02 * 5 / 6))
    big = {(v.tier, v.with_card): v for v in model.evaluate(rules, {}, {"costco": 100_000.0}, 1.0) if v.membership == "costco"}
    assert big[("executive", False)].reward == 1250.0


def test_reward_families_defaults_to_all_families():
    rules = _rules()
    rules["memberships"]["costco"]["tiers"]["executive"].pop("reward_families")
    spend = {"costco": 5000.0, "costco_gas": 1000.0}
    vs = {(v.tier, v.with_card): v for v in model.evaluate(rules, {}, spend, 1.0) if v.membership == "costco"}
    assert vs[("executive", False)].reward == pytest.approx(120.0)


def test_window_and_annualise(tmp_path):
    install(tmp_path)
    txns = feed.load(tmp_path)
    w = model.pick_window(txns, days=30)
    assert w.end == date(2026, 1, 22) and w.asked == 30  # the rows start 2026-01-05, so 18 days are covered
    assert w.days == 18 and w.partial and w.annualize == pytest.approx(365 / 18)
    y = model.pick_window(txns, year=2026)  # clamped to the January rows the fixture holds
    assert (y.start, y.end) == (min(t.date for t in txns), date(2026, 1, 22)) and y.asked == 365 and y.partial


def test_end_to_end_on_fixture(tmp_path):
    rules = _rules()
    install(tmp_path)
    txns = feed.load(tmp_path)  # transfers, payments and income left out
    cat.categorize(txns, cat.build_families(rules))
    w = model.pick_window(txns, year=2026)
    spend = model.spend_by_family(model.in_window(txns, w))
    assert spend["amazon"] == pytest.approx(120 - 20)  # refund nets; the Prime fee is not Amazon spend
    assert spend[cat.FEES] == pytest.approx(139 + 98)
    assert spend["other"] == pytest.approx(75)  # paycheck excluded, so it cannot net this negative
    fees = model.observed_fees(txns, rules)
    assert set(fees) == {"prime", "walmart_plus"}
    verdicts = model.evaluate(rules, _toml(INSTANCE / "assumptions.toml"), spend, w.annualize)
    assert {v.membership for v in verdicts} == {"prime", "walmart_plus", "costco"}


def test_requires_membership_attributes_whole_edge():
    rules = _rules()
    spend = {"gas": 1000.0, "costco": 1000.0}
    vs = {(v.tier, v.with_card): v for v in model.evaluate(rules, {}, spend, 1.0) if v.membership == "costco"}
    v = vs[("gold_star", True)]
    assert v.edge == pytest.approx(20.0)  # gas 4% vs 2%; costco 2% vs 2% is nothing
    assert v.attributable == pytest.approx(v.edge)


def test_card_without_membership_gets_no_attribution():
    # A card that pays the same with or without the membership (no
    # without_membership_rates) gives the membership no edge. Inline card:
    # the real OnePay rule gained without-rates once its terms were verified.
    rules = _rules()
    rules["cards"]["walmart_card"] = {"requires_membership": False, "default_rate": 0.01, "rates": {"walmart_delivery": 0.05}}
    spend = {"walmart_delivery": 2000.0}
    v = next(v for v in model.evaluate(rules, {}, spend, 1.0) if v.membership == "walmart_plus" and v.with_card)
    assert v.edge == pytest.approx(60.0)
    assert v.attributable == 0.0


def test_onepay_rule_attributes_two_points_to_walmart_plus():
    rules = _rules()
    spend = {"walmart_delivery": 1000.0, "walmart_store": 1000.0}
    v = next(v for v in model.evaluate(rules, {}, spend, 1.0) if v.membership == "walmart_plus" and v.with_card)
    assert v.edge == pytest.approx(60.0)  # 5% vs the flat-2% test baseline on $2,000
    assert v.attributable == pytest.approx(40.0)  # 5% vs 3% without Walmart+


def _fee(amount):
    return ld.Txn(date(2026, 5, 4), "Fee", amount, "Shopping")


def test_fee_mismatch_flags_stale_list_fee():
    rules = _rules()
    found = {"prime": [_fee(139.0), _fee(149.0), _fee(14.99)], "costco": [_fee(65.0), _fee(130.0), _fee(60.0)]}
    hits = [(m, t.amount, fee) for m, t, fee in model.fee_mismatches(found, rules)]
    # Prime $149 (a rise) and $14.99 (monthly billing) flag; Costco $65 and $130
    # match a tier each; the pre-2024 $60 is $5 off $65, over 5%, so it flags.
    assert hits == [("prime", 149.0, 139.0), ("prime", 14.99, 139.0), ("costco", 60.0, 65.0)]


def test_fee_mismatch_tolerance_is_the_larger_of_one_dollar_and_five_percent():
    rules = _rules()
    ok = model.fee_mismatches({"walmart_plus": [_fee(98 + 4.8)]}, rules)
    bad = model.fee_mismatches({"walmart_plus": [_fee(98 + 5.0)]}, rules)
    assert ok == [] and len(bad) == 1


def test_observed_fees_respect_fee_amounts():
    # costco.com orders post under the same descriptor as the app-paid fee;
    # only the exact fee amounts count, in the report scan as in the categoriser.
    rules = _rules()
    order = ld.Txn(date(2026, 5, 4), "WWW COSTCO COM", 212.37, "Shopping")
    fee = ld.Txn(date(2026, 5, 4), "WWW COSTCO COM", 65.0, "Shopping")
    found = model.observed_fees([order, fee], rules)
    assert [t.amount for t in found["costco"]] == [65.0]
    assert model.fee_mismatches(found, rules) == []


def test_window_is_clamped_to_the_data(tmp_path):
    install(tmp_path)
    txns = feed.load(tmp_path)
    full = model.pick_window(txns, days=10)  # inside the data: not clamped
    assert full.days == 10 and not full.partial

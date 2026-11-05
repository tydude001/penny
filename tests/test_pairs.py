"""penny pairs: what Costco saves on what you buy there, against Walmart's
prices, from both stores' captures and a pairing file. Every item, id and
price here is invented."""

import json
import shutil
from datetime import date

import pytest
from test_board import INSTANCE
from test_costco import _line, _receipt
from test_walmart import _item, _order

from penny import __main__ as cli
from penny import board, home, model, pairs, worth

RULES = {
    "held": {"baseline_card": "flat", "cards": ["flat", "store"]},
    "families": {"costco": {"networks": ["visa"]}},
    "cards": {
        "flat": {"default_rate": 0.02, "network": "visa"},
        "store": {"default_rate": 0.01, "rates": {"walmart_delivery": 0.05}, "network": "mastercard"},
    },
}

PAIRS = """\
perk = "costco.prices"

[[pair]]
name = "Beef"
unit = "lb"
costco = { 10 = 10 }
walmart = ["w10"]

[[pair]]
name = "Paper"
unit = "sheet"
costco = { 20 = 6000 }
walmart = { w20 = 2000 }

[[pair]]
name = "Soda"
unit = "can"
costco = { 30 = 30 }
walmart_price = 0.60
walmart_price_on = "2026-09-01"

[[pair]]
name = "Seltzer"
unit = "can"
costco = { 40 = 24 }
"""

RECEIPTS = [
    _receipt("old", "2025-01-02", [_line("10", "BEEF CHUBS", 30.0)]),  # over a year before the last: not counted
    _receipt("A", "2025-06-01", [_line("10", "BEEF CHUBS", 30.0), _line("20", "PAPER", 24.0)]),
    _receipt("B", "2026-03-01", [_line("10", "BEEF CHUBS", 30.0), _line("0", "/10", -5.0, unit=-1, price=0),
                                 _line("30", "SODA", 15.0), _line("40", "SELTZER", 9.0)]),
    _receipt("C", "2026-05-31", [_line("20", "PAPER", 24.0)]),
]

ORDERS = [
    _order("o1", "2025-08-01", [{"type": "REGULAR", "items": [_item("w10", "Beef 1 lb", 7.0, unit="$7.00/lb")]}]),
    _order("o2", "2026-02-01", [{"type": "REGULAR", "items": [_item("w10", "Beef 1 lb", 8.0, unit="$8.00/lb"),
                                                              _item("w20", "Paper 2000", 20.0, qty=2)]}]),
]


def _measure(text=PAIRS, receipts=RECEIPTS, orders=ORDERS, tmp_path=None):
    p = tmp_path / pairs.FILE
    p.write_text(text)
    from penny import costco, walmart
    return pairs.measure(pairs.load(p), costco.receipts(receipts), walmart.lines_from_capture(orders), RULES)


def test_each_pair_is_priced_from_the_newest_order_or_the_fallback(tmp_path):
    m = _measure(tmp_path=tmp_path)
    assert (m.first, m.last, m.scale) == (date(2025, 6, 1), date(2026, 5, 31), 1.0)
    assert (m.walmart_rate, m.costco_rate) == (0.05, 0.02)  # the store card at Walmart; Costco takes only the Visa
    by = {r.name: r for r in m.rows}
    beef = by["Beef"]  # two packs in the year, one with a $5 coupon; the 2025-01 one is out
    assert (beef.bought, beef.paid, beef.walmart_unit) == (20, 55.0, 8.0)  # the newer order's label
    assert beef.walmart_from == "order 2026-02-01"
    assert beef.saving == pytest.approx(20 * 8.0 * 0.95 - 55.0 * 0.98)
    paper = by["Paper"]  # sized by id: $20 for 2 x 2000 sheets
    assert paper.walmart_unit == pytest.approx(0.005) and paper.bought == 12000
    assert paper.saving == pytest.approx(12000 * 0.005 * 0.95 - 48.0 * 0.98)
    soda = by["Soda"]
    assert soda.walmart_unit == 0.60 and soda.walmart_from == "walmart.com, 2026-09-01"
    assert soda.saving == pytest.approx(30 * 0.60 * 0.95 - 15.0 * 0.98)
    assert by["Seltzer"].saving is None and "no walmart_price" in by["Seltzer"].walmart_from
    assert m.saving == pytest.approx(beef.saving + paper.saving + soda.saving)
    assert len(m.priced) == 3


def test_a_line_costco_loses_on_counts_against_it(tmp_path):
    text = PAIRS.replace("walmart_price = 0.60", "walmart_price = 0.40")
    soda = next(r for r in _measure(text, tmp_path=tmp_path).rows if r.name == "Soda")
    assert soda.saving == pytest.approx(30 * 0.40 * 0.95 - 15.0 * 0.98) and soda.saving < 0


def test_under_a_year_of_receipts_scales_and_says_so(tmp_path):
    m = _measure(receipts=RECEIPTS[2:], tmp_path=tmp_path)  # 2026-03-01 -> 05-31: 92 days
    assert m.partial and m.scale == pytest.approx(365 / 92)
    beef = next(r for r in m.rows if r.name == "Beef")
    assert beef.bought == pytest.approx(10 * 365 / 92)
    assert "a guess" in m.summary()


def test_a_label_in_another_unit_leaves_the_pair_unpriced(tmp_path):
    orders = [_order("o1", "2026-02-01", [{"type": "REGULAR", "items": [_item("w10", "Beef", 8.0, unit="50.0¢/oz")]}])]
    beef = next(r for r in _measure(orders=orders, tmp_path=tmp_path).rows if r.name == "Beef")
    assert beef.saving is None and "'50.0¢/oz'" in beef.walmart_from


def test_label_price_reads_dollars_and_cents():
    assert pairs.label_price("$8.47/lb", "lb") == 8.47
    assert pairs.label_price("42.7¢/oz", "oz") == pytest.approx(0.427)
    assert pairs.label_price("42.7¢/oz", "lb") is None
    assert pairs.label_price("", "oz") is None


@pytest.mark.parametrize("text, msg", [
    ("[[pair]]\nname='x'\nunit='lb'\ncostco={1=1}\n", "perk"),
    ("perk='costco.prices'\n[[pair]]\nname='x'\nunit='lb'\n", "no costco"),
    ("perk='costco.prices'\n[[pair]]\nname='x'\nunit='lb'\ncostco=['1']\n", "table of item number"),
    ("perk='costco.prices'\n[[pair]]\nname='x'\nunit='lb'\ncostco={1=0}\n", "above 0"),
    ("perk='costco.prices'\n[[pair]]\nname='x'\nunit='lb'\ncostco={1=1}\nwalmart_price=-1\n", "walmart_price"),
])
def test_a_bad_pairing_file_is_refused_by_name(tmp_path, text, msg):
    (tmp_path / pairs.FILE).write_text(text)
    with pytest.raises(ValueError, match=msg):
        pairs.load(tmp_path / pairs.FILE)


def test_no_pairing_file_measures_nothing(tmp_path):
    assert pairs.load(tmp_path / pairs.FILE) is None
    assert pairs.for_instance(tmp_path, RULES) is None


# -- the board ---------------------------------------------------------------

ASSUMPTIONS = {
    "costco": {
        "prices": {"low": 0, "base": 0, "high": 0, "note": "", "measured": 5, "measured_if": "hand-typed"},
        "gas": {"low": 0, "base": 0, "high": 0, "note": ""},
    }
}


def _instance_rules():
    return model.resolve_points(home.load_rules(INSTANCE / "rules.toml"))


def test_a_measurement_counts_as_the_perk_with_nothing_to_answer(tmp_path):
    m = _measure(tmp_path=tmp_path)
    applied = worth.apply_measures(ASSUMPTIONS, [m])
    prices = applied["costco"]["prices"]
    x = float(round(m.saving))
    assert (prices["low"], prices["base"], prices["high"]) == (x, x, x)
    assert prices["measured_by"]["items"] == 3 and prices["measured_by"]["last"] == "2026-05-31"
    assert ASSUMPTIONS["costco"]["prices"]["base"] == 0  # the file's values are untouched
    ds = worth.derive(_instance_rules(), applied, {"costco": 3000.0}, 1.0, {}, [], date(2026, 6, 1), None, [m])
    asked = {p["section"] for d in ds if d.key == "perks:zero" for p in d.data["perks"]} | {d.key for d in ds}
    assert "costco.prices" not in asked and "perk:costco.prices" not in asked
    assert "costco.gas" in asked  # an unmeasured perk is still a question


def test_your_recorded_answer_beats_the_measurement(tmp_path):
    m = _measure(tmp_path=tmp_path)
    mine = {"costco": {"prices": {"low": 40, "base": 40, "high": 40, "note": "Recorded 2026-06-01: my own number"}}}
    assert worth.apply_measures(mine, [m]) == mine
    assert worth.apply_measures({}, [m])["costco"]["prices"]["base"] == float(round(m.saving))  # no section: added


def _verdict(fee=130.0, reward=69.0, attributable=0.0):
    return model.Verdict("costco", "executive", False, fee, 3000.0, 0.0, {}, attributable, {}, reward, {"low": 0, "base": 0, "high": 0})


def _perk(section, **kw):
    return {"section": section, "membership": "costco", "mlabel": "Costco", "label": section, "net": -61.0, "fee": 130.0, **kw}


def test_the_zero_perks_ask_counts_the_reward_not_just_the_fee():
    title, why, _, line, badge, quick = board._zero_perks_text(_instance_rules(), _verdict(), [_perk("costco.prices"), _perk("costco.gas")])
    assert title == "Does Costco save you $61 or more a year?"
    assert (line, badge, quick) == ("$61 or more a year makes Costco a keep; less, a drop.", "needs you", "")
    assert "Counting the $130 fee and the $69 Executive reward, Costco nets −$61 a year" in why
    assert "Nothing is counted" not in why


def _mb(saving=250.0, stale=(), coverage=0.6):
    return {"items": 11, "last": "2026-10-29", "partial": False, "coverage": coverage, "stale": list(stale),
            "unpriced": [], "summary": "", "saving": saving}


def test_a_measured_perk_is_checked_only_when_old_prices_carry_it():
    rules = _instance_rules()
    assert worth.freshness_decision(rules, "costco", "prices", _mb()) is None
    assert worth.freshness_decision(rules, "costco", "prices", _mb(stale=[("Soda", "2026-01-03", -14.0)])) is None  # $14 of $250
    d = worth.freshness_decision(rules, "costco", "prices", _mb(stale=[("Beef", "2026-06-09", 150.0), ("Water", "2025-11-02", 39.0)]))
    assert d.kind == "measured" and d.title == "Check 2 old Walmart prices behind the Costco saving"
    assert d.sub == "$189 of the $250 rides on them"
    assert "Beef (Jun 9, +$150), Water (Nov 2, 2025, +$39)" in " ".join(d.explain)
    low = worth.freshness_decision(rules, "costco", "prices", _mb(coverage=0.3))
    assert low.title == "Pair more Costco items" and "30%" in low.sub


def test_a_measured_perk_says_where_its_number_came_from():
    rules = _instance_rules()
    applied = {"costco": {"prices": {"low": 250.0, "base": 250.0, "high": 250.0, "measured_by": _mb()}}}
    v = model.Verdict("costco", "executive", False, 130.0, 3000.0, 0.0, {}, 0.0, {}, 69.0, model.perks(applied, "costco"))
    lines = worth.net_lines(rules, applied, v)
    assert ("Prices, from your receipts", 250.0, "receipts to Oct 29") in lines
    assert v.net["base"] == 189.0 and worth.verdict_line(rules, applied, v)[0] == "keep"


# -- the CLI -----------------------------------------------------------------

def test_penny_pairs_prints_each_line_and_the_total(tmp_path, capsys):
    shutil.copy(INSTANCE / "rules.toml", tmp_path / "rules.toml")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "costco-receipts-2026-06-01.json").write_text(json.dumps(RECEIPTS))
    (tmp_path / "data" / "walmart-orders-2026-06-01.json").write_text(json.dumps(ORDERS))
    (tmp_path / pairs.FILE).write_text(PAIRS)
    cli.main(["--home", str(tmp_path), "pairs"])
    out = capsys.readouterr().out
    assert "costco.prices: receipts 2025-06-01 -> 2026-05-31 (365 days)" in out
    assert "Beef" in out and "unpriced" in out and "over 3 priced pairs" in out


def test_the_board_and_the_report_both_count_the_measured_saving(tmp_path, capsys):
    from feedfix import install
    install(tmp_path)
    shutil.copy(INSTANCE / "rules.toml", tmp_path / "rules.toml")
    (tmp_path / "assumptions.toml").write_text("[costco.prices]\nlow = 0\nbase = 0\nhigh = 0\n")
    (tmp_path / "data" / "costco-receipts-2026-06-01.json").write_text(json.dumps(RECEIPTS))
    (tmp_path / "data" / "walmart-orders-2026-06-01.json").write_text(json.dumps(ORDERS))
    (tmp_path / pairs.FILE).write_text(PAIRS)
    s = board.load_state(board.Config(tmp_path, tmp_path / "rules.toml", tmp_path / "assumptions.toml"), date(2026, 6, 1))
    saving = s.assumptions["costco"]["prices"]["base"]
    assert saving and s.assumptions["costco"]["prices"]["measured_by"]
    v = s.analysis.verdicts[worth.headline_key(s.rules, "costco")]
    assert v.perks["base"] == saving
    cli.main(["--home", str(tmp_path), "report"])
    out = capsys.readouterr().out
    assert any(line.startswith("Costco") and f"${saving:,.0f}" in line for line in out.splitlines())
    assert "base = 0" in (tmp_path / "assumptions.toml").read_text()  # nothing written


def test_penny_pairs_without_a_pairing_file_says_what_it_needs(tmp_path):
    (tmp_path / "rules.toml").write_text((INSTANCE / "rules.toml").read_text())
    with pytest.raises(SystemExit, match="costco-pairs.toml"):
        cli.main(["--home", str(tmp_path), "pairs"])


# -- coverage, price age, by weight -------------------------------------------

def test_coverage_counts_priced_pairs_against_the_year_skipped_items_aside(tmp_path):
    receipts = RECEIPTS + [_receipt("D", "2026-05-01", [_line("50", "MONITOR", 200.0), _line("60", "CHICKEN BITES", 40.0)])]
    m = _measure('skip = ["50"]\n' + PAIRS, receipts=receipts, tmp_path=tmp_path)  # top level: before the first [[pair]]
    # Year: beef 55, paper 48, soda 15, seltzer 9, chicken bites 40; the monitor skipped.
    assert m.total == pytest.approx(55 + 48 + 15 + 9 + 40)
    assert m.coverage == pytest.approx((55 + 48 + 15) / m.total)  # seltzer is paired but unpriced
    assert [(it.item_number, a) for it, a in m.unpaired] == [("60", 40.0)]
    assert "cover 71% of your $167 a year at Costco; biggest unpaired: Chicken Bites $40." in m.summary()


def test_the_walmart_price_is_the_median_of_recent_orders(tmp_path):
    orders = [_order(f"o{i}", d, [{"type": "REGULAR", "items": [_item("w10", "Beef", p, unit=f"${p:.2f}/lb")]}])
              for i, (d, p) in enumerate([("2025-06-01", 2.0), ("2026-01-01", 9.0), ("2026-02-01", 7.0), ("2026-03-01", 8.0)])]
    beef = next(r for r in _measure(orders=orders, tmp_path=tmp_path).rows if r.name == "Beef")
    assert beef.walmart_unit == 8.0  # 7, 8, 9 within 180 days of 03-01; June's $2 is too old to count
    assert beef.walmart_from == "median of 3 orders to 2026-03-01" and beef.price_on == date(2026, 3, 1)
    assert beef.stale  # 91 days before the last receipt, 05-31
    orders = orders[:2] + [_order("o9", "2026-04-01", [{"type": "REGULAR", "items": [_item("w10", "Beef", 6.0, unit="$6.00/lb")]}])]
    beef = next(r for r in _measure(orders=orders, tmp_path=tmp_path).rows if r.name == "Beef")
    assert not beef.stale and beef.walmart_unit == 7.5  # 9 and 6: an even count takes the middle two


def test_a_newer_checked_price_replaces_a_stale_order(tmp_path):
    text = PAIRS.replace('walmart = ["w10"]', 'walmart = ["w10"]\nwalmart_price = 9.5\nwalmart_price_on = "2026-05-01"')
    beef = next(r for r in _measure(text, tmp_path=tmp_path).rows if r.name == "Beef")
    assert (beef.walmart_unit, beef.walmart_from, beef.stale) == (9.5, "walmart.com, 2026-05-01", False)
    older = text.replace('"2026-05-01"', '"2025-12-01"')  # older than the 2026-02-01 order: the order wins
    beef = next(r for r in _measure(older, tmp_path=tmp_path).rows if r.name == "Beef")
    assert beef.walmart_unit == 8.0


def test_an_item_sold_by_weight_counts_pounds_off_the_shelf_price(tmp_path):
    receipts = [_receipt("A", "2026-05-31", [_line("70", "BNLS/SL BRST", 29.90, price=2.99), _line("70", "BNLS/SL BRST", 14.95, price=2.99)])]
    text = 'perk = "costco.prices"\n[[pair]]\nname = "Chicken"\nunit = "lb"\ncostco = { 70 = "by weight" }\nwalmart_price = 4.0\n'
    m = _measure(text, receipts=receipts, tmp_path=tmp_path)
    r = m.rows[0]
    assert r.bought / m.scale == pytest.approx(15.0)  # 10 lb + 5 lb
    from penny import costco
    it = costco.rank(costco.receipts(receipts))[0]
    assert it.by_weight and it.weight == pytest.approx(15.0)
    assert not costco.rank(costco.receipts(RECEIPTS))[0].by_weight


# -- suggestions -------------------------------------------------------------

def test_abbreviations_match_but_a_word_with_vowels_must_match_its_start():
    assert pairs._same("brst", "breast") and pairs._same("bnls", "boneless") and pairs._same("choc", "chocolate")
    assert not pairs._same("cut", "crust") and not pairs._same("gel", "ghirardelli")
    assert pairs._words("SUNCHIPS VTY VTY P48 T12H4") == ["sunchips", "vty"]


def _suggest(tmp_path, receipts, orders, text=PAIRS):
    from penny import walmart
    m = _measure(text, receipts=receipts, orders=orders, tmp_path=tmp_path)
    return pairs.suggest(m, pairs.load(tmp_path / pairs.FILE), walmart.lines_from_capture(orders))


def test_suggest_matches_by_name_and_merges_item_numbers_of_one_product(tmp_path):
    receipts = RECEIPTS + [_receipt("E", "2026-05-01", [
        _line("80", "BNLS/SL BRST", 29.90, price=2.99), _line("81", "BNLS BREAST", 20.0, price=2.99),
        _line("90", "KS SPARKLING 35/12OZ LIME", 12.0), _line("95", "THICK CUT", 9.0)])]
    orders = ORDERS + [_order("o3", "2026-04-01", [{"type": "REGULAR", "items": [
        _item("w80", "Fresh Boneless Chicken Breast", 9.0, unit="$4.50/lb"),
        _item("w90", "Lime Sparkling Water, 12 fl oz Cans, 8 / Pack", 4.0),
        _item("w95", "Classic Pizza Crust, Thin Cut Style", 3.0)]}])]
    found = _suggest(tmp_path, receipts, orders)
    by = {sg.candidates[0].item_id: sg for sg in found}
    assert set(by) == {"w80", "w90"}  # "THICK CUT" shares one word with the crust: not enough
    chicken = by["w80"]
    assert [it.item_number for it, _ in chicken.items] == ["80", "81"] and chicken.annual == pytest.approx(49.90)
    assert 'costco = { 80 = "by weight", 81 = "by weight" }' in chicken.toml() and 'walmart = ["w80"]' in chicken.toml()
    water = by["w90"].toml()
    assert "costco = { 90 = 35 }" in water and "walmart = { w90 = 8 }" in water
    (tmp_path / "check.toml").write_text('perk = "costco.prices"\n' + chicken.toml() + "\n" + by["w90"].toml() + "\n")
    assert len(pairs.load(tmp_path / "check.toml")["pairs"]) == 2  # what it prints pastes as is


def test_a_size_it_cannot_read_is_zero_so_the_file_refuses_it(tmp_path):
    receipts = RECEIPTS + [_receipt("E", "2026-05-01", [_line("85", "GRANOLA BARS VTY", 12.0)])]
    orders = ORDERS + [_order("o3", "2026-04-01", [{"type": "REGULAR", "items": [_item("w85", "Granola Bars Variety", 4.0)]}])]
    (sg,) = _suggest(tmp_path, receipts, orders)
    assert "85 = 0 }" in sg.toml() and "fill in" in sg.toml()
    (tmp_path / "check.toml").write_text('perk = "costco.prices"\n' + sg.toml() + "\n")
    with pytest.raises(ValueError, match="above 0"):
        pairs.load(tmp_path / "check.toml")


def test_penny_pairs_suggest_prints_blocks_and_the_misses(tmp_path, capsys):
    shutil.copy(INSTANCE / "rules.toml", tmp_path / "rules.toml")
    (tmp_path / "data").mkdir()
    receipts = RECEIPTS + [_receipt("E", "2026-05-01", [_line("80", "BNLS/SL BRST", 29.90, price=2.99), _line("99", "MYSTERY", 5.0)])]
    orders = ORDERS + [_order("o3", "2026-04-01", [{"type": "REGULAR", "items": [_item("w80", "Boneless Chicken Breast", 9.0, unit="$4.50/lb")]}])]
    (tmp_path / "data" / "costco-receipts-2026-06-01.json").write_text(json.dumps(receipts))
    (tmp_path / "data" / "walmart-orders-2026-06-01.json").write_text(json.dumps(orders))
    (tmp_path / pairs.FILE).write_text(PAIRS)
    cli.main(["--home", str(tmp_path), "pairs", "--suggest"])
    out = capsys.readouterr().out
    assert "coverage: the priced pairs are" in out
    assert "# BNLS/SL BRST: $30 a year at Costco" in out and 'walmart = ["w80"]' in out
    assert "no Walmart match: MYSTERY [99]" in out


def test_a_pair_can_name_the_store_you_would_buy_it_at_instead(tmp_path):
    rules = {**RULES, "families": {**RULES["families"], "amazon": {"label": "Amazon"}},
             "cards": {**RULES["cards"], "store": {**RULES["cards"]["store"], "rates": {"walmart_delivery": 0.05, "amazon": 0.03}}}}
    text = PAIRS.replace('walmart = { w20 = 2000 }', 'walmart = { w20 = 2000 }\nstore = "amazon"\nprice = 0.004\nprice_on = "2026-05-01"')
    p = tmp_path / pairs.FILE
    p.write_text(text)
    from penny import costco, walmart
    m = pairs.measure(pairs.load(p), costco.receipts(RECEIPTS), walmart.lines_from_capture(ORDERS), rules)
    paper = next(r for r in m.rows if r.name == "Paper")
    assert (paper.store, paper.walmart_unit, paper.walmart_from) == ("Amazon", 0.004, "Amazon, 2026-05-01")  # not the Walmart order
    assert paper.saving == pytest.approx(12000 * 0.004 * 0.97 - 48.0 * 0.98)  # at the Amazon card's 3%
    assert "your Walmart prices (Amazon's for Paper)" in m.summary()
    p.write_text(text.replace('store = "amazon"', 'store = "nowhere"'))
    with pytest.raises(ValueError, match="not a family"):
        pairs.measure(pairs.load(p), costco.receipts(RECEIPTS), walmart.lines_from_capture(ORDERS), rules)

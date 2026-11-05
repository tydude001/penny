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
from penny import board, home, model, pairs

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


def test_derive_offers_the_measurement_over_a_hand_typed_one(tmp_path):
    m = _measure(tmp_path=tmp_path)
    ds = board.derive(_instance_rules(), ASSUMPTIONS, {"costco": 3000.0}, 1.0, {}, [], date(2026, 6, 1), None, [m])
    perks = {p["section"]: p for d in ds if d.key == "perks:zero" for p in d.data["perks"]}
    prices = perks["costco.prices"]
    assert prices["measured"] == round(m.saving) and prices["measured_if"] == "What your receipts show"
    assert prices["receipts"] == {"items": 3, "last": "2026-05-31", "partial": False}
    assert "Not priced: Seltzer" in prices["how"]
    assert "receipts" not in perks["costco.gas"]


def _verdict(fee=130.0, reward=69.0, attributable=0.0):
    return model.Verdict("costco", "executive", False, fee, 3000.0, 0.0, {}, attributable, {}, reward, {"low": 0, "base": 0, "high": 0})


def _perk(section, **kw):
    return {"section": section, "membership": "costco", "mlabel": "Costco", "label": section, "net": -61.0, "fee": 130.0, **kw}


def test_the_zero_perks_ask_counts_the_reward_not_just_the_fee():
    title, why, _ = board._zero_perks_text(_instance_rules(), _verdict(), [_perk("costco.prices"), _perk("costco.gas")])
    assert title == "Does Costco save you $61 or more a year?"
    assert "Counting the $130 fee and the $69 Executive reward, Costco nets −$61 a year" in why
    assert "Nothing is counted" not in why


def test_a_measured_perk_turns_the_ask_into_the_answer():
    measured = _perk("costco.prices", measured=80.0, receipts={"items": 5, "last": "2026-10-29", "partial": False})
    title, why, how = board._zero_perks_text(_instance_rules(), _verdict(), [measured, _perk("costco.gas")])
    assert title == "Costco: keep, +$19 a year"
    assert why.startswith("Your receipts say it saves you about $80 a year on what you buy there (5 items")
    assert "through Oct 29" in why and why.endswith("makes it a keep.")
    assert how.startswith("Pick what your receipts show")
    short = _perk("costco.prices", measured=30.0, receipts={"items": 2, "last": "2026-10-29", "partial": True})
    title, why, _ = board._zero_perks_text(_instance_rules(), _verdict(), [short])
    assert title == "Costco: drop, −$31 a year" and "a scaled guess" in why


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


def test_penny_pairs_without_a_pairing_file_says_what_it_needs(tmp_path):
    (tmp_path / "rules.toml").write_text((INSTANCE / "rules.toml").read_text())
    with pytest.raises(SystemExit, match="costco-pairs.toml"):
        cli.main(["--home", str(tmp_path), "pairs"])

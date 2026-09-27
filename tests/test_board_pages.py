"""The five pages: card rows, where to swipe, the overview tiles, the flip
point, and the routes. Synthetic rules; the route tests run on the fixture feed (feedfix.py)."""

import json
import re
import shutil
import threading
from datetime import date
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from feedfix import install
from test_board import ASSUMPTIONS

from penny import board, cardworth, check, home, model, plaid
from penny import categorize as cat

INSTANCE = Path(__file__).parent / "fixtures" / "home"  # a test instance, read over penny/defaults
TODAY = date(2026, 9, 13)


def _wallet():
    """A baseline, a held store card, a considered store card, a category card,
    a dormant card, a card whose only edge is its default rate, and a flat 2%
    that exists only for card worth."""
    rules = {
        "held": {"baseline_card": "pts", "cards": ["pts", "shop_card", "cat_card", "dorm", "pay"]},
        "families": {"shop": {"label": "Shop", "membership": "shop"}, "warehouse": {"label": "Warehouse", "membership": "warehouse"}, "restaurants": {"label": "Restaurants"}, "groceries": {"label": "Groceries"}, "gas": {"label": "Gas"}},
        "cards": {
            "flat": {"label": "Flat 2%", "verified": True, "default_rate": 0.02, "annual_fee": 0},
            "pts": {"label": "Points", "verified": False, "default_rate": 0.02, "rates": {"restaurants": 0.06}, "annual_fee": 95},
            "shop_card": {"label": "Shop Card", "verified": True, "verified_on": "2026-09-01", "requires_membership": False, "default_rate": 0.01, "rates": {"shop": 0.05}, "without_membership_rates": {"shop": 0.03}},
            "wh_card": {"label": "Warehouse Card", "verified": False, "requires_membership": True, "default_rate": 0.01, "rates": {"gas": 0.04}},
            "cat_card": {"label": "Grocery Card", "verified": True, "default_rate": 0.01, "rates": {"groceries": 0.05}},
            "dorm": {"label": "Dormant", "verified": True, "default_rate": 0.01, "rates": {"restaurants": 0.06}},
            "pay": {"label": "Pay Card", "verified": True, "default_rate": 0.025, "default_via": "Phone Pay"},
        },
        "memberships": {
            "shop": {"label": "Shop+", "fee": 100, "families": ["shop"], "card": "shop_card"},
            "warehouse": {"label": "Warehouse", "fee": 60, "families": ["warehouse"], "card": "wh_card"},
        },
    }
    assumptions = {"card_worth": {"alternatives": ["pts", "flat"]}, "point_value": {"pts": {"low": 0.01, "high": 0.03}}}
    spend = {"shop": 2000.0, "restaurants": 1000.0, "warehouse": 1000.0, "groceries": 500.0, "gas": 400.0, cat.OTHER: 3000.0}
    return rules, assumptions, spend


def _state(rules, assumptions, spend):
    ds = board.rank(board.derive(rules, assumptions, spend, 1.0, {}, [], TODAY))
    rows = board.verdict_rows(rules, assumptions, spend, 1.0)
    w = model.Window(date(2025, 9, 14), TODAY)
    worth = cardworth.card_worth(rules, assumptions, spend)
    return board.State(TODAY, Path("x.csv"), rules, assumptions, w, w.start, 0, spend, {}, ds, rows, None, worth=worth, carried={"pts": spend})


def test_card_rows_role_edge_split_and_badge():
    rules, assumptions, spend = _wallet()
    rows = {r.key: r for r in board.card_rows(rules, assumptions, spend, 1.0, cardworth.card_worth(rules, assumptions, spend))}
    assert "flat" not in rows  # only a card-worth alternative
    got = {k: (r.role, round(r.edge), round(r.bonus), round(r.default_part), r.badge) for k, r in rows.items()}
    assert got == {
        "pts": ("baseline", 0, 0, 0, "not worth the fee"),  # earns $198 − $95 against the flat card's $158
        "shop_card": ("store card", 60, 60, 0, "keep"),
        "wh_card": ("considered", 8, 8, 0, "skip"),
        "cat_card": ("category card", 15, 15, 0, "keep"),
        "dorm": ("dormant", 0, 0, 0, "no reason to swipe"),
        "pay": ("category card", 35, 0, 35, "keep"),  # ½ point over the baseline wherever it pays 1x: $34.50
    }
    assert rows["pay"].default_part == pytest.approx(34.5)
    rules["cards"]["pts"]["annual_fee"] = 0
    assert board.card_rows(rules, assumptions, spend, 1.0, cardworth.card_worth(rules, assumptions, spend))[0].badge == "worth it"
    rules["cards"]["pts"]["annual_fee"] = 95
    assert rows["shop_card"].attributable == {"shop": pytest.approx(40.0)}
    assert rows["shop_card"].without.startswith("3% Shop")
    assert rows["shop_card"].verified_on == "2026-09-01" and rows["pay"].fee is None
    assert next(iter(rows)) == "pts" and list(rows)[-1] == "wh_card"  # baseline first, considered last
    spend["gas"] = 800.0
    wh = next(r for r in board.card_rows(rules, assumptions, spend, 1.0) if r.key == "wh_card")
    assert wh.badge == "worth applying?"


def test_where_to_swipe_takes_the_top_held_card_ties_to_the_baseline():
    rules, _, spend = _wallet()
    rows = board.swipe_rows(rules, spend, 1.0)
    got = [(w.family, w.card, w.via, w.rate) for w in rows]
    assert got == [
        ("shop", "shop_card", "Shop Card", 0.05),
        ("restaurants", "pts", "Points", 0.06),  # the dormant card ties at 6%: the baseline keeps it
        ("warehouse", "pay", "Phone Pay", 0.025),  # won by the default rate, so named by default_via
        ("groceries", "cat_card", "Grocery Card", 0.05),
        ("gas", "pay", "Phone Pay", 0.025),  # the Warehouse Card pays 4% but is not held
        (cat.OTHER, "pay", "Phone Pay", 0.025),  # everything else comes last
    ]
    assert rows[-1].gain == pytest.approx(15.0)
    del rules["cards"]["pay"]["default_via"]
    assert board.swipe_rows(rules, spend, 1.0)[-1].via == "Pay Card"


def test_overview_tiles_sum_headline_nets_and_bonus_edges_only():
    rules, assumptions, spend = _wallet()
    s = _state(rules, assumptions, spend)
    v = board.view(s)
    net, fees = board.membership_totals(rules, v.verdicts, s.verdicts)
    assert net == pytest.approx(sum(r.base for r in s.verdicts)) and net == pytest.approx(40 - 100 - 60)
    assert fees == 160
    page = board.render_page(s, v)
    assert "−$120" in page and "net of $160 in fees" in page
    assert "+$75" in page  # 60 + 15; the Pay Card's $34 is its default rate, not a bonus
    assert "Phone Pay" in page


def test_flip_point_bisects_inside_the_range():
    rules, assumptions, _ = _wallet()
    spend = {"restaurants": 1000.0, cat.OTHER: 3000.0}  # 6000c − 95 against 80: zero at 2.917¢
    c = board.flip_point(rules, assumptions, spend)
    assert 0.01 < c < 0.03 and c == pytest.approx(175 / 6000, abs=1e-6)
    assert round(board.margin_at(rules, assumptions, spend, c)) == 0
    assumptions["point_value"]["pts"]["high"] = 0.025
    assert board.flip_point(rules, assumptions, spend) is None


def test_verified_on_is_allowed_only_as_an_iso_date():
    rules = home.load_rules(INSTANCE / "rules.toml")
    ok = board.check_write({"file": "rules", "section": "cards.walmart_card", "key": "verified_on", "value": "2026-09-22"}, rules)
    assert ok.value == "2026-09-22"
    for bad in ("2026-13-01", "22 Sep", 20260922, True):
        with pytest.raises(board.Refused):
            board.check_write({"file": "rules", "section": "cards.walmart_card", "key": "verified_on", "value": bad}, rules)


CARD_WORTH = """
[point_value.sapphire_preferred]
low = 0.0105
high = 0.0198

[card_worth]
alternatives = ["sapphire_preferred", "baseline"]
"""


@pytest.fixture
def server(tmp_path):
    shutil.copy(INSTANCE / "rules.toml", tmp_path / "rules.toml")
    (tmp_path / "assumptions.toml").write_text(ASSUMPTIONS + CARD_WORTH)
    install(tmp_path)
    cfg = board.Config(tmp_path, tmp_path / "rules.toml", tmp_path / "assumptions.toml", None, None)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), board.make_handler(cfg, today=lambda: TODAY))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield tmp_path, cfg, HTTPConnection("127.0.0.1", srv.server_address[1])
    finally:
        srv.shutdown()
        srv.server_close()


def _get(c, path):
    c.request("GET", path)
    r = c.getresponse()
    return r.status, r.read().decode()


@pytest.mark.parametrize("path", ["/", "/cashflow", "/cards", "/memberships", "/todo", "/data"])
def test_each_page_is_200_and_marks_its_own_nav_entry(server, path):
    _, _, c = server
    status, page = _get(c, path)
    assert status == 200
    marked = re.findall(r"<a href='([^']*)' class=on aria-current=page>", page)
    assert marked == [path] * (3 if path == "/cards" else 2)  # the sidebar, and the phone's tab or section switcher; Cards is both


def test_cash_flow_picks_a_month_and_counts_the_fixture_paycheck(server):
    _, _, c = server
    status, page = _get(c, "/cashflow")
    assert status == 200 and "January 2026" in page and "$3,000" in page  # the last full month on file
    assert "the 1 full month on file, under a year" in page  # the fixture's rows are all in 2026-01
    status, page = _get(c, "/cashflow?m=1999-01")  # a month with no rows falls back
    assert status == 200 and "January 2026" in page


def test_checks_page_without_balances_and_a_miss_on_every_page(server):
    _, cfg, c = server
    status, page = _get(c, "/checks")
    assert status == 200 and "No balances yet" in page
    assert re.findall(r"<a href='([^']*)' class=on aria-current=page>", page) == ["/checks"] * 2  # the sidebar and the More switcher, like Data
    s = board.load_state(cfg, TODAY)
    s.checks = check.Checks([check.Result("Card …1", False, "d", 4.0, through=TODAY)], [], [])
    assert "Card …1 misses by +4.00" in board.render_page(s) and "Card …1 misses by +4.00" in board.render_cards(s)
    assert "<span class='badge crit'>miss</span>" in board.render_checks(s)


def test_accounts_page_without_balances_then_with(server):
    root, _cfg, c = server
    status, page = _get(c, "/accounts")
    assert status == 200 and "No balances yet" in page
    assert re.findall(r"<a href='([^']*)' class=on aria-current=page>", page) == ["/accounts"]  # the sidebar; the phone reaches it from the overview
    plaid.Store(root, "production").append_balances([
        {"at": "2026-09-13T12:00:00+00:00", "item_id": "fix", "account_id": "checking", "name": "Checking", "mask": "1111",
         "type": "depository", "current": 2000.0},
        {"at": "2026-09-13T12:00:00+00:00", "item_id": "fix", "account_id": "visa", "name": "Visa", "mask": "9999",
         "type": "credit", "current": 500.0, "limit": 2000.0}])
    status, page = _get(c, "/accounts")
    assert status == 200 and "$1,500" in page and "id=g-depository" in page and "id=g-credit" in page
    assert "href='/transactions?show=all&amp;acct=Visa'" in page and "25% of $2,000 limit" in page
    status, page = _get(c, "/")
    assert "<a class=tile href='/accounts'><span class=ov>Net worth</span>" in page


def test_card_pages_and_a_missing_card(server):
    _, _, c = server
    status, page = _get(c, "/cards/sapphire_preferred")
    assert status == 200 and "id=cpp" in page and "Fee and credits by anniversary year" in page and "projected" in page
    status, page = _get(c, "/cards/prime_visa")
    assert status == 200 and "Prime Visa" in page
    status, _ = _get(c, "/cards/nope")
    assert status == 404
    status, _ = _get(c, "/cards/baseline")  # the card-worth-only flat 2% has no page
    assert status == 404


def test_todo_carries_every_plan_anchor_and_a_record_shows_on_memberships(server):
    _, cfg, c = server
    s = board.load_state(cfg, TODAY)
    _, page = _get(c, "/todo")
    for it in board.view(s).items:
        assert f"id='{it.key}'" in page
    c.request("POST", "/record", json.dumps({"file": "assumptions", "section": "prime.rxpass", "key": "base", "value": 240}), {"Content-Type": "application/json"})
    r = c.getresponse()
    assert r.status == 200 and json.loads(r.read())["recorded"]
    _, page = _get(c, "/memberships")
    assert "RxPass <i>placeholder</i></span><span class=mono>+$240" in page


def test_verify_writes_the_date_with_the_flag(server):
    root, _, c = server
    body = [{"file": "rules", "section": "cards.walmart_card", "key": "verified", "value": True}, {"file": "rules", "section": "cards.walmart_card", "key": "verified_on", "value": "2026-09-13"}]
    c.request("POST", "/record", json.dumps(body), {"Content-Type": "application/json"})
    r = c.getresponse()
    assert r.status == 200 and json.loads(r.read())["recorded"] == []  # the file already says both
    body[1]["value"] = "2026-09-30"
    c.request("POST", "/record", json.dumps(body), {"Content-Type": "application/json"})
    r = c.getresponse()
    assert r.status == 200 and [x["key"] for x in json.loads(r.read())["recorded"]] == ["verified_on"]
    assert 'verified_on = "2026-09-30"' in (root / "rules.toml").read_text()


def test_a_store_that_refuses_a_cards_network_takes_it_off_the_sheet_and_the_edge():
    rules, assumptions, spend = _wallet()
    rules["families"]["warehouse"]["networks"] = ["visa"]
    rules["cards"]["pts"]["network"] = "visa"
    rules["cards"]["pay"]["network"] = "mastercard"
    rows = {w.family: w for w in board.swipe_rows(rules, spend, 1.0)}
    assert rows["warehouse"].card == "pts"  # the Pay Card pays more but the store won't take it
    pay = next(r for r in board.card_rows(rules, assumptions, spend, 1.0) if r.key == "pay")
    assert pay.edge == pytest.approx(34.5 - 5.0) and "warehouse" not in pay.detail
    for c in rules["cards"].values():  # a card with no network key counts as taken; give every one a network
        c["network"] = "mastercard"
    rows = {w.family: w for w in board.swipe_rows(rules, spend, 1.0)}
    assert rows["warehouse"].card == "" and rows["warehouse"].via == "no card you hold"


def test_a_credit_past_its_until_is_left_out_of_the_projected_year():
    rules, assumptions, spend = _wallet()
    rules["card_credits"] = {"pts": {"year_starts": "09-13", "credits": {
        "old": {"label": "Old", "face": 300, "until": "2026-09-01"},
        "new": {"label": "New", "face": 100}}}}
    s = _state(rules, assumptions, spend)
    s.credit_years = {"pts": []}
    (row,) = board.credit_rows(s, "pts")
    assert row[0].startswith("2026-09-13 → 2027-09-12") and row[1].startswith("$95")
    assert row[2:4] == ["—", "$0 / $100"]


def test_logo_and_home_screen_icons(server):
    _, _, c = server
    status, page = _get(c, "/")
    assert status == 200 and "<title>penny</title>" in page and "<svg class=logo" in page
    for link in ("href='/favicon.ico'", "href='/apple-touch-icon.png'", "href='/site.webmanifest'"):
        assert link in page
    for path, kind in board.STATIC_FILES.items():
        c.request("GET", path)
        r = c.getresponse()
        body = r.read()
        assert r.status == 200 and r.getheader("Content-Type") == kind and body, path
    c.request("GET", "/site.webmanifest")
    manifest = json.loads(c.getresponse().read())
    assert manifest["name"] == "penny" and all(i["src"] in board.STATIC_FILES for i in manifest["icons"])


def test_phone_tabs_are_sections_and_more_lists_the_rest(server):
    _, _, c = server
    _, page = _get(c, "/budget")
    tabs = page.split("<nav class=tabs", 1)[1].split("</nav>", 1)[0]
    assert re.findall(r"<a href='([^']*)'", tabs) == ["/", "/transactions", "/cards", "/todo", "/more"]
    assert "<a href='/transactions' class=on aria-current=true>" in tabs  # Budget is in Spending
    assert re.findall(r"<a href='([^']*)'", page.split("<nav class=sectabs", 1)[1].split("</nav>", 1)[0]) == ["/transactions", "/budget", "/recurring", "/cashflow"]
    _, page = _get(c, "/")
    assert "<nav class=sectabs" not in page  # Home is one page
    status, page = _get(c, "/more")
    assert status == 200 and "<a href='/more' class=on aria-current=page>" in page
    assert "<a class=lrow href='/checks'>" in page and "<a class=lrow href='/data'>" in page
    _, page = _get(c, "/data")
    assert "<a href='/more' class=on aria-current=true>" in page


def test_overview_leads_with_the_month_then_the_rest(server):
    root, _, c = server
    status, page = _get(c, "/")
    assert status == 200
    assert "Spent in January" in page and "Set budgets ›" in page  # no rows yet in September; no budget set
    order = [page.index(h) for h in ("<h1 class=ov>", *(f"<h2 class=h>{h}</h2>" for h in ("Coming up", "Where it went", "Recent", "Cash flow", "Worth it?", "Where to swipe")))]
    assert order == sorted(order)
    recent = page.split("<h2 class=h>Recent</h2>", 1)[1].split("</section>", 1)[0]
    assert "SOME MERCHANT" in recent and "ACME PAYROLL" in recent and "TO SAVINGS" not in recent  # income stays, transfers don't
    with (root / "assumptions.toml").open("a") as f:
        f.write("\n[budget]\ngroceries = 1\n")
    _, page = _get(c, "/")
    assert "Over budget in January" in page and "spent of $1" in page
    assert re.search(r"<b>Groceries is \$[\d,]+ over budget\.</b>", page)


def test_overview_on_an_export_keeps_the_verdicts():
    rules, assumptions, spend = _wallet()
    page = board.render_page(_state(rules, assumptions, spend))
    assert "needs the card feed" in page and "Worth it?" in page and "Where it went" not in page


def test_spending_charts_and_the_editor_sheet(server):
    root, _, c = server
    _, page = _get(c, "/cashflow")
    plot = page.split("<div class=plot", 1)[1].split("<div class=xl>", 1)[0]
    assert "<a href='/cashflow?m=2026-01' class='c on'" in plot and "<i class='in'" in plot and "<i class='out'" in plot
    _, page = _get(c, "/budget")
    assert "<i class=ref" in page and "average $" in page and "over'" not in page  # no budget yet: the monthly average, nothing red
    with (root / "assumptions.toml").open("a") as f:
        f.write("\n[budget]\ngroceries = 1\n")
    _, page = _get(c, "/budget")
    assert "budget $1</span>" in page and "<i class='amt over'" in page
    _, page = _get(c, "/transactions?id=fix0")
    assert "<a class=scrim href='/transactions'" in page and "<section class='block sheet' id=edit>" in page


def test_the_section_switcher_does_not_hide_the_tier_picker(server):
    _, _, c = server
    _, page = _get(c, "/memberships")
    assert "<div class='choices seg'" in page  # the tier picker's buttons
    assert ".seg{display:none}" not in board.CSS and ",.seg{display:none}" not in board.CSS  # a class hidden on desktop once hid them


def test_linked_accounts_show_their_card_label():
    rules = {"cards": {"sp": {"label": "Sapphire Preferred"}, "bare": {}},
             "accounts": {"2002": {"card": "sp"}, "1111": {"card": "bare"}, "2001": {"card": "sp", "feed_lacks_rewards": True}}}
    names = board.card_names(rules)
    assert names == {"2002": "Sapphire Preferred", "2001": "Sapphire Preferred"}  # a card with no label keeps the feed's name
    assert board._named_check("CREDIT CARD …2002", names) == "Sapphire Preferred …2002"
    assert board._named_check("Apple Card", names) == "Apple Card" and board._named_check("CREDIT CARD …2222", names) == "CREDIT CARD …2222"

"""The Transactions page (budget-plan M5): filter, recategorise by hand, and
the categoriser checks. Runs on the fixture feed (feedfix.py)."""

import json
import re
import tomllib
from datetime import date

from test_board_pages import TODAY, _get, server  # noqa: F401  (the fixture)

from penny import board
from penny import categorize as cat
from penny.load import Txn


def _post(c, path, body):
    c.request("POST", path, json.dumps(body), {"Content-Type": "application/json"})
    r = c.getresponse()
    return r.status, json.loads(r.read())


def test_page_lists_the_spend_rows_and_marks_its_nav_entry(server):  # noqa: F811
    _, _, c = server
    status, page = _get(c, "/transactions")
    assert status == 200
    assert re.findall(r"<a href='([^']*)' class=on aria-current=page>", page) == ["/transactions"] * 3  # the sidebar, the Spending switcher, and the Spending tab
    assert "COSTCO WHSE #0123" in page and "SHELL OIL" in page
    assert "ACME PAYROLL" not in page and "TO SAVINGS" not in page  # income and transfers aren't spend
    assert "Top merchants per family" in page and "Biggest uncategorised merchants" in page


def _rows(page):
    """The Rows block only: the checks below it name merchants too."""
    return page.split("<span class=ov>Rows</span>", 1)[1].split("</section>", 1)[0]


def test_filters_narrow_the_rows(server):  # noqa: F811
    _, _, c = server
    get = lambda path: _rows(_get(c, path)[1])
    page = get("/transactions?q=costco")
    assert "COSTCO WHSE #0123" in page and "COSTCO WHSE GAS" in page and "SHELL OIL" not in page
    page = get("/transactions?fam=gas")
    assert "SHELL OIL" in page and "COSTCO WHSE #0123" not in page
    page = get("/transactions?show=other")
    assert "ACME PAYROLL" in page and "TO SAVINGS" in page and "SHELL OIL" not in page
    page = get("/transactions?m=1999-01")
    assert "No rows match." in page


def test_save_then_clear_a_hand_override(server):  # noqa: F811
    root, _, c = server
    status, page = _get(c, "/transactions?id=fix14")  # Some Merchant
    assert status == 200 and "id=edit" in page and "data-id='fix14'" in page
    status, res = _post(c, "/override", {"id": "fix14", "family": "restaurants", "category": "Dining"})
    assert status == 200 and res == {"id": "fix14", "override": {"family": "restaurants", "category": "dining"}, "changed": True}
    assert json.loads((root / "data" / "overrides.json").read_text()) == {"fix14": {"family": "restaurants", "category": "dining"}}
    page = _rows(_get(c, "/transactions?fam=restaurants")[1])
    assert "SOME MERCHANT" in page and "title='set by hand'" in page
    _, res = _post(c, "/override", {"id": "fix14", "family": "restaurants"})
    assert res["changed"] is False
    status, res = _post(c, "/override", {"id": "fix14", "clear": True})
    assert status == 200 and res["override"] == {}
    assert json.loads((root / "data" / "overrides.json").read_text()) == {}
    audit = [json.loads(line) for line in (root / "data" / "board-decisions.jsonl").read_text().splitlines()]
    assert [a["after"] for a in audit] == [{"family": "restaurants", "category": "dining"}, {}]


def test_save_then_clear_a_merchant_override(server):  # noqa: F811
    root, _, c = server
    status, page = _get(c, "/transactions?id=fix14")
    assert "data-scope=merchant" in page and "Save for every" in page
    status, res = _post(c, "/override", {"id": "fix14", "scope": "merchant", "category": "Fun"})
    assert status == 200 and res["changed"] is True and res["override"] == {"category": "fun"}
    merchants = json.loads((root / "data" / "merchants.json").read_text())
    assert merchants == {res["merchant"]: {"category": "fun"}}
    assert not (root / "data" / "overrides.json").exists()  # the row's own file is untouched
    page = _get(c, "/transactions?id=fix14")[1]
    assert "Set for every" in page and "data-clear=merchant" in page
    status, res = _post(c, "/override", {"id": "fix14", "scope": "merchant", "clear": True})
    assert status == 200 and res["override"] == {}
    assert json.loads((root / "data" / "merchants.json").read_text()) == {}
    status, res = _post(c, "/override", {"id": "fix14", "scope": "shop", "category": "fun"})
    assert status == 400 and "scope" in res["error"]


def test_override_refusals(server):  # noqa: F811
    root, _, c = server
    for body, why in [
        ({"id": "nope", "family": "gas"}, "no spend row"),
        ({"id": "fix13", "family": "gas"}, "no spend row"),  # a transfer
        ({"id": "fix14", "family": "made_up"}, "unknown family"),
        ({"id": "fix14", "category": "<b>"}, "lower-case words"),
        ({"id": "fix14"}, "nothing to save"),
        (["fix14"], "send {id"),
    ]:
        status, res = _post(c, "/override", body)
        assert status == 400 and why in res["error"], body
    assert not (root / "data" / "overrides.json").exists()


def test_merchant_checks_rank_uncategorised_and_families():
    t = lambda name, amt, fam, bc: Txn(date(2026, 1, 1), name, amt, "", family=fam, budget_category=bc)
    rows = [t("A", 10, "other", "uncategorised"), t("B", 50, "other", "uncategorised"), t("A", 5, "other", "uncategorised"),
            t("C", 100, "gas", "transport"), t("D", 20, "gas", "transport")]
    worst, per = board.merchant_checks(rows, top=1)
    assert worst == [("B", 50, 1), ("A", 15, 2)]
    assert list(per) == ["gas", "other"] and per["gas"] == (120, [("C", 100)])


def test_rule_hint_is_valid_toml_for_a_quoted_merchant():
    t = Txn(date(2026, 1, 1), "Torchy's", 25.0, "", description="TORCHY'S TACOS (ATX)")
    hint = board._rule_hint(t, "restaurants", "dining", {"families": {"restaurants": {}}})
    doc = tomllib.loads(hint.split("\n# ")[0])
    pat = doc["categories"]["rules"][0]["patterns"][0]
    assert re.search(pat, t.match_text) and doc["categories"]["rules"][0]["category"] == "dining"


def test_ruled_ignores_the_hand_override():
    rules = {"families": {"gas": {"patterns": ["shell"]}}, "categories": {"families": {"gas": "transport"}}}
    t = Txn(date(2026, 1, 1), "Shell", 40.0, "")
    cat.categorize([t], cat.build_families(rules))
    cat.assign_categories([t], rules)
    t.family, t.budget_category = "other", "fun"  # as a hand override leaves it
    assert board.ruled(t, rules) == ("gas", "transport")


def test_payments_off_the_feed_lists_the_payments_cash_flow_counts():
    """Home's Where it went links this row here; its rows are transfers, and
    no row carries the name as its category."""
    t = lambda name, amt, kind, acct, bc="transfer": Txn(date(2026, 8, 1), name, amt, "", kind=kind, account_type=acct,
                                                         budget_category=bc, transfer=kind != "purchase")
    rows = [t("DEPT EDUCATION", 90, "payment", "depository"), t("CHASE CREDIT CRD", 500, "payment", "depository"),
            t("SHELL OIL", 40, "purchase", "credit", "transport")]
    payees = [re.compile("chase credit crd", re.IGNORECASE)]
    kept = board.txn_filter(rows, {"cat": "payments off the feed"}, {}, {}, payees)
    assert [x.name for x in kept] == ["DEPT EDUCATION"]

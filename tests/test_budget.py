"""Budget vs actual (budget-plan M5): lines per category, the month's pace,
the [budget] write, and the page on the fixture feed."""

import json
import re
import tomllib
from datetime import date

import pytest
from test_board_pages import TODAY, _get, server  # noqa: F401  (the fixture)

from penny import board, budget
from penny.cashflow import Month


def test_lines_put_budgeted_first_and_average_the_full_months():
    full = [Month("2026-06", spent={"dining": 300, "travel": 90}), Month("2026-07", spent={"dining": 100})]
    sel = Month("2026-08", spent={"dining": 250, "personal care": 40})
    ls = budget.lines(sel, full, {"dining": 200, "personal_care": 0, "giving": 0})
    assert [ln.category for ln in ls] == ["dining", "personal care", "travel", "giving"]  # giving has a key, so it stays
    d = ls[0]
    assert (d.key, d.budget, d.actual, d.average, d.left) == ("dining", 200, 250, 200, -50)
    assert ls[1].key == "personal_care" and ls[1].budget == 0
    assert ls[2].key is None and ls[2].average == 45


def test_elapsed_only_in_the_current_month():
    assert budget.elapsed("2026-09", date(2026, 9, 15)) == 0.5
    assert budget.elapsed("2026-08", date(2026, 9, 15)) is None


@pytest.mark.parametrize("value,ok", [(250, True), (0, True), (12.5, True), (-1, False), ("250", False), (True, False)])
def test_budget_write_is_a_number_zero_or_more(value, ok):
    w = {"file": "assumptions", "section": "budget", "key": "dining", "value": value}
    if ok:
        assert board.check_write(w, {}).value == value
    else:
        with pytest.raises(board.Refused):
            board.check_write(w, {})


def _post(c, body):
    c.request("POST", "/record", json.dumps(body), {"Content-Type": "application/json"})
    r = c.getresponse()
    return r.status, json.loads(r.read())


def test_page_and_saving_a_budget(server):  # noqa: F811
    root, _, c = server
    with open(root / "assumptions.toml", "a") as f:
        f.write("\n[budget]\ngroceries = 0 # kept\ntransport = 0\n")
    status, page = _get(c, "/budget")
    assert status == 200 and "January 2026" in page and "No budgets yet" in page
    assert re.findall(r"<a href='([^']*)' class=on aria-current=page>", page) == ["/budget"] * 2  # the sidebar and the Spending switcher
    assert "data-key='groceries'" in page and "data-key='transport'" in page
    status, res = _post(c, [{"file": "assumptions", "section": "budget", "key": "groceries", "value": 300}])
    assert status == 200 and res["recorded"][0]["after"] == 300
    text = (root / "assumptions.toml").read_text()
    assert "groceries = 300 # kept" in text and tomllib.loads(text)["budget"]["transport"] == 0
    status, page = _get(c, "/budget")
    assert "1 category with a budget" in page and "$300" in page
    assert "data-fill-avg=all" in page and page.count("data-use-avg=1") == 2  # the averages still reach a box that's filled in
    status, res = _post(c, [{"file": "assumptions", "section": "budget", "key": "dining", "value": 50}])
    assert status == 400 and "never adds a key" in res["error"]  # not in the file: a hand edit
    status, page = _get(c, "/budget?m=1999-01")  # an unknown month falls back
    assert status == 200 and "January 2026" in page

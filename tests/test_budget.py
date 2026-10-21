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


# Running hot: over the 12-month median by both floors (docs/competitors.md § fungible).

def _hot_lines(sel_spent: dict, history: list[dict]) -> dict[str, budget.Line]:
    full = [Month(f"2025-{i + 1:02d}", spent=h) for i, h in enumerate(history)]
    return {ln.category: ln for ln in budget.lines(Month("2026-08", spent=sel_spent), full, {})}


def test_median_is_over_the_full_months_with_an_empty_month_as_zero():
    ls = _hot_lines({"dining": 10}, [{"dining": 100}, {"dining": 300}, {}, {"dining": 200}, {"dining": 900}])
    assert ls["dining"].median == 200  # 0, 100, 200, 300, 900


def test_floors_default_from_the_shipped_rules_and_an_instance_overrides_one():
    from penny import home
    assert budget.hot_floors(home.defaults()) == (50.0, 0.25)
    assert budget.hot_floors({}) == (50.0, 0.25)
    assert budget.hot_floors(home.merge(home.defaults(), {"running_hot": {"share": 0.5}})) == (50.0, 0.5)


def test_running_hot_needs_both_floors():
    steady = [{"groceries": 800, "subscriptions": 20, "dining": 200}] * 12
    ls = _hot_lines({"groceries": 900, "subscriptions": 45, "dining": 300}, steady)
    hot = lambda c: budget.running_hot(ls[c], 12, 50, 0.25)
    assert ls["groceries"].actual > ls["groceries"].median and not hot("groceries")  # $100 over clears $50, misses 25% ($200)
    assert ls["subscriptions"].actual > ls["subscriptions"].median and not hot("subscriptions")  # 125% over, misses $50
    assert hot("dining")  # $100 over: clears $50 and 25% ($50)


def test_running_hot_at_the_floors_and_below_the_median():
    ls = _hot_lines({"a": 250, "b": 150, "c": 249.99}, [{"a": 200, "b": 200, "c": 200}] * 12)
    assert budget.running_hot(ls["a"], 12, 50, 0.25)  # exactly $50 and exactly 25%
    assert not budget.running_hot(ls["b"], 12, 50, 0.25)  # under the median
    assert not budget.running_hot(ls["c"], 12, 50, 0.25)  # a cent short of both


def test_running_hot_waits_for_three_full_months():
    ls = _hot_lines({"dining": 900}, [{"dining": 100}] * 2)
    assert not budget.running_hot(ls["dining"], 2, 50, 0.25)
    assert budget.running_hot(ls["dining"], 3, 50, 0.25)


def test_needs_a_look_says_running_hot_unless_it_is_already_over_budget():
    from types import SimpleNamespace
    full = [Month(f"2025-{i + 1:02d}", spent={"dining": 200, "travel": 100, "groceries": 800}) for i in range(12)]
    sel = Month("2026-08", spent={"dining": 400, "travel": 300, "groceries": 900})
    ls = budget.lines(sel, full, {"travel": 150})
    v = SimpleNamespace(s=SimpleNamespace(rules={}, today=date(2026, 8, 20)), items=[], numbers={})
    html = board._attention(v, ls, [], len(full))
    assert "Dining is running hot." in html and "$200 over its median month of $200" in html
    assert "Travel is $150 over budget." in html and "Travel is running hot" not in html  # one alert, the budget one
    assert "Groceries" not in html  # $100 over misses 25% of $800
    assert "running hot" not in board._attention(v, ls, [], 2)  # under three full months

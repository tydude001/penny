"""The To do page as rows: penny's call, one line, and the tap that accepts
it; dated to-dos as a short Later list; a card you said no to leaves."""

import json
import re
import shutil

import pytest
from feedfix import install
from test_board import (
    ASSUMPTIONS,
    INSTANCE,
    TODAY,
    WIKI,
    _get,
    _post,
    _serve,
)

from penny import board, home


@pytest.fixture
def server(tmp_path):
    """The board on the feed, as test_board's fixture serves it."""
    install(tmp_path)
    yield from _serve(tmp_path)


def _row(page, key):
    m = re.search(rf"<article class='q trow' id='{re.escape(key)}'.*?</article>", page, re.DOTALL)
    assert m, key
    return m.group(0)


def test_each_row_leads_with_a_badge_a_line_and_its_one_tap(server):
    _, c = server
    _, page = _get(c, "/todo")
    rx = _row(page, "perk:prime.rxpass")
    assert ">NEEDS YOU</span>" in rx and "data-q=answer" in rx and ">Other answer · Why</button>" in rx
    assert "or more a year makes Amazon Prime a keep; less, a drop." in rx  # the short line on the row
    assert "Amazon Prime is a keep at" in rx  # the full why, inside the row's body
    visa = _row(page, "apply:costco_visa:costco")
    assert ">SKIP</span>" in visa and "Don&#x27;t apply for the Costco Anywhere Visa" in visa
    assert "data-q=submit>OK, skip it</button>" in visa
    # The tap submits the body's first form: the skip, not the rates check.
    body = visa.split("class=qbody>", 1)[1]
    assert body.index("data-key=declined_on") < (body.index("data-key=verified") if "data-key=verified" in body else len(body))
    assert "biggest money first" in page and "Every open input" not in page


def test_skipping_a_card_records_the_day_and_the_question_leaves(server):
    root, c = server
    status, res = _post(c, [{"file": "rules", "section": "cards.costco_visa", "key": "declined_on", "value": "2026-09-13", "note": "declined on the board"}])
    assert status == 200 and res["recorded"] == [{"file": "rules.toml", "section": "cards.costco_visa", "key": "declined_on", "after": "2026-09-13"}]
    assert '[cards.costco_visa]\ndeclined_on = "2026-09-13"' in (root / "rules.toml").read_text()
    rules = home.load_rules(root / "rules.toml")
    assert rules["cards"]["costco_visa"]["rates"]  # the defaults' rates survive the new table
    _, page = _get(c, "/todo")
    assert "id='apply:costco_visa:costco'" not in page
    assert "Skipped the Costco Anywhere Visa" in page and "You said no on 2026-09-13" in page
    audit = [json.loads(x) for x in (root / "data" / "board-decisions.jsonl").read_text().splitlines()]
    assert (audit[-1]["key"], audit[-1]["before"], audit[-1]["note"]) == ("declined_on", None, "declined on the board")


@pytest.mark.parametrize("w", [
    {"file": "rules", "section": "cards.no_such_card", "key": "declined_on", "value": "2026-09-13"},
    {"file": "rules", "section": "cards.costco_visa", "key": "declined_on", "value": "soon"},
    {"file": "rules", "section": "cards.costco_visa", "key": "declined_by", "value": "2026-09-13"},
])
def test_declined_on_is_a_date_on_a_card_penny_knows(w):
    with pytest.raises(board.Refused):
        board.check_write(w, home.load_rules(INSTANCE / "rules.toml"))


def test_filters_list_only_what_has_something_open(server):
    _, c = server
    _, page = _get(c, "/todo")
    chips = re.findall(r"class=fchip data-tag='([^']*)'", page)
    assert "" in chips and "m:prime" in chips and "c:costco_visa" in chips
    assert "c:sapphire_preferred" not in chips  # nothing open on it


def test_dated_to_dos_are_a_later_list_and_the_rest_fold_away(tmp_path):
    install(tmp_path)
    shutil.copy(INSTANCE / "rules.toml", tmp_path / "rules.toml")
    (tmp_path / "assumptions.toml").write_text(ASSUMPTIONS)
    (tmp_path / "wiki.md").write_text(WIKI)
    cfg = board.Config(tmp_path, tmp_path / "rules.toml", tmp_path / "assumptions.toml", tmp_path / "wiki.md")
    page = board.render_todo(board.load_state(cfg, TODAY))
    later = re.search(r"<details class=later id='wiki:penny-call'.*?</details>", page, re.DOTALL).group(0)
    assert "Call the bank" in later and "18 days" in later and "Due 2026-10-01" in later
    assert "1 more in the to-do file" in page and "Spend caps" in page

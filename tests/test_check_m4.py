"""What the Checks page and every page's warning read: the day a check
reached, stale and missing accounts, and when an Apple Card import is due (M4)."""

from datetime import date

from penny import check
from penny.statements import Statement

TODAY = date(2026, 9, 27)


def res(acct, ok, through, gap=0.0):
    return check.Result(acct, ok, "d", gap, through=date.fromisoformat(through) if through else None)


def apple(end):
    e = date.fromisoformat(end)
    return Statement("apple", e.replace(day=1), e, 0, 0)


def test_the_next_apple_statement_closes_at_the_end_of_the_following_month():
    assert check.apple_due([apple("2026-01-31")], TODAY)[1] == date(2026, 2, 28)
    assert check.apple_due([apple("2026-12-31")], TODAY)[1] == date(2027, 1, 31)
    last, nxt, late = check.apple_due([apple("2026-07-31"), apple("2026-08-31")], TODAY)
    assert (last, nxt, late) == (date(2026, 8, 31), date(2026, 9, 30), False)
    assert check.apple_due([apple("2026-08-31")], date(2026, 10, 8))[2] is True  # 8 days past the close
    assert check.apple_due([], TODAY) is None


def test_attention_names_misses_stale_accounts_and_bad_pdfs_but_not_a_fixed_old_miss():
    c = check.Checks([
        res("Card A", False, "2026-06-30", 12.5), res("Card A", True, "2026-09-26"),  # an old miss, tied since
        res("Card B", True, "2026-09-20"), res("Card B", False, "2026-09-26", -3.0),
        res("Bank", True, "2026-08-01"),  # 57 days
        res("New", None, None),  # nothing to check yet: no warning
    ], ["x.pdf: no balance line"], [])
    lines = check.attention(c, TODAY)
    assert lines == ["statement x.pdf: no balance line", "Card B misses by -3.00 (2026-09-26)", "Bank not tied since 2026-08-01"]


def test_an_overdue_apple_import_replaces_its_stale_warning():
    c = check.Checks([res(check.APPLE, True, "2026-07-31")], [], [apple("2026-07-31")])
    assert check.attention(c, TODAY) == [f"{check.APPLE} import overdue: the statement closing 2026-08-31 isn't imported"]

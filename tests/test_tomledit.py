import tomllib

import pytest

from penny import tomledit as te

FIXTURE = """\
# Top comment: keep me.

[held]
baseline_card = "flat"   # the card every edge is measured against
costco_tier = "gold_star"

[cards.flat]
# A comment inside a dotted section.
label = "Flat # not a comment"
verified = false
default_rate = 0.015
rates = { restaurants = 0.045, gas = 0.02 }   # inline table

[cards.other]
verified = false
default_rate = 0.01

[prime.rxpass]
  note = "PLACEHOLDER: \\"quoted\\" and a # hash"
  low = 0
  base = 180
  high = 600
"""


def _diff(a: str, b: str) -> list[int]:
    la, lb = a.split("\n"), b.split("\n")
    assert len(la) == len(lb)
    return [i for i, (x, y) in enumerate(zip(la, lb)) if x != y]


def test_set_key_touches_only_the_target_line():
    out = te.set_key(FIXTURE, "cards.flat", "verified", True)
    changed = _diff(FIXTURE, out)
    assert len(changed) == 1
    assert out.split("\n")[changed[0]] == "verified = true"
    assert tomllib.loads(out)["cards"]["flat"]["verified"] is True
    assert tomllib.loads(out)["cards"]["other"]["verified"] is False  # same key, next section, untouched


def test_set_key_keeps_trailing_comment_and_indentation():
    out = te.set_key(FIXTURE, "held", "baseline_card", "sapphire")
    assert 'baseline_card = "sapphire"   # the card every edge is measured against' in out
    out = te.set_key(FIXTURE, "prime.rxpass", "base", 240)
    assert "\n  base = 240\n" in out
    assert len(_diff(FIXTURE, out)) == 1


def test_set_key_replaces_a_string_containing_a_hash():
    out = te.set_key(FIXTURE, "prime.rxpass", "note", 'Recorded 2026-09-13: $12/mo "generic", C:\\rx')
    assert tomllib.loads(out)["prime"]["rxpass"]["note"] == 'Recorded 2026-09-13: $12/mo "generic", C:\\rx'
    assert len(_diff(FIXTURE, out)) == 1


def test_missing_section_or_key_raises_and_never_appends():
    with pytest.raises(KeyError):
        te.set_key(FIXTURE, "cards.nope", "verified", True)
    with pytest.raises(KeyError):
        te.set_key(FIXTURE, "cards.other", "label", "x")  # label exists only in [cards.flat]
    with pytest.raises(KeyError):
        te.set_inline(FIXTURE, "cards.flat", "rates", "drugstores", 0.02)
    with pytest.raises(KeyError):
        te.set_inline(FIXTURE, "cards.flat", "default_rate", "x", 0.02)  # not an inline table


def test_set_inline_changes_one_subkey():
    out = te.set_inline(FIXTURE, "cards.flat", "rates", "restaurants", 0.066)
    assert "rates = { restaurants = 0.066, gas = 0.02 }   # inline table" in out
    assert len(_diff(FIXTURE, out)) == 1
    out = te.set_inline(FIXTURE, "cards.flat", "rates", "gas", 0.03)
    assert "rates = { restaurants = 0.045, gas = 0.03 }   # inline table" in out


def test_fmt():
    assert te.fmt(True) == "true" and te.fmt(False) == "false"
    assert te.fmt(0.0450) == "0.045"
    assert te.fmt(180.0) == "180"
    assert te.fmt(2.5) == "2.5"
    assert te.fmt(7) == "7"
    assert te.fmt('a "b" \\ c\nd') == '"a \\"b\\" \\\\ c\\nd"'
    assert tomllib.loads("x = " + te.fmt('a "b" \\ c\nd\te\x01'))["x"] == 'a "b" \\ c\nd\te\x01'
    with pytest.raises(ValueError):
        te.fmt(float("nan"))
    with pytest.raises(TypeError):
        te.fmt([1])


def test_write_edits_replaces_the_file(tmp_path):
    p = tmp_path / "rules.toml"
    p.write_text(FIXTURE)
    before, after = te.write_edits(p, lambda t: te.set_key(t, "cards.other", "default_rate", 0.02))
    assert before == FIXTURE and p.read_text() == after
    assert tomllib.loads(p.read_text())["cards"]["other"]["default_rate"] == 0.02
    assert [x.name for x in tmp_path.iterdir()] == ["rules.toml"]  # no temp file left


def test_write_edits_leaves_the_file_alone_when_the_edit_fails(tmp_path):
    p = tmp_path / "rules.toml"
    p.write_text(FIXTURE)
    with pytest.raises(KeyError):
        te.write_edits(p, lambda t: te.set_key(t, "cards.nope", "verified", True))
    assert p.read_text() == FIXTURE

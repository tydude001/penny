"""Every write the board makes, the CLI can make: hand overrides (row or
merchant, set or cleared) and Record writes, through the same checks."""

import json
import shutil
import tomllib

import pytest
from feedfix import install
from test_board import ASSUMPTIONS, INSTANCE

from penny import __main__ as cli


@pytest.fixture
def inst(tmp_path):
    install(tmp_path)
    shutil.copy(INSTANCE / "rules.toml", tmp_path / "rules.toml")
    (tmp_path / "assumptions.toml").write_text(ASSUMPTIONS)
    return tmp_path


def _cli(inst, *argv):
    cli.main(["--home", str(inst), *argv])


def _audit(inst):
    return [json.loads(x) for x in (inst / "data" / "board-decisions.jsonl").read_text().splitlines()]


def test_override_sets_and_clears_a_row(inst, capsys):
    _cli(inst, "override", "fix14", "--family", "restaurants")
    assert json.loads((inst / "data" / "overrides.json").read_text()) == {"fix14": {"family": "restaurants"}}
    _cli(inst, "override", "fix14", "--clear")
    assert json.loads((inst / "data" / "overrides.json").read_text()) == {}
    assert "cleared, back to the rules" in capsys.readouterr().out
    assert [a["after"] for a in _audit(inst)] == [{"family": "restaurants"}, {}]


def test_override_by_merchant_writes_merchants_json(inst):
    _cli(inst, "override", "fix14", "--merchant", "--category", "fun")
    merchants = json.loads((inst / "data" / "merchants.json").read_text())
    assert list(merchants.values()) == [{"category": "fun"}]
    assert not (inst / "data" / "overrides.json").exists()


def test_clear_with_a_family_is_refused(inst):
    with pytest.raises(SystemExit):
        _cli(inst, "override", "fix14", "--clear", "--family", "gas")


def test_set_writes_through_the_boards_allowlist(inst, capsys):
    _cli(inst, "set", "assumptions", "prime.rxpass.base", "240", "--note", "priced")
    assert tomllib.loads((inst / "assumptions.toml").read_text())["prime"]["rxpass"]["base"] == 240
    assert "[prime.rxpass] base = 240" in capsys.readouterr().out
    assert _audit(inst)[-1]["after"] == 240 and _audit(inst)[-1]["note"] == "priced"


def test_set_refuses_what_the_board_refuses(inst):
    with pytest.raises(SystemExit, match="Allowed|refuse|not|only"):
        _cli(inst, "set", "rules", "held.baseline_card", "prime_visa")


def test_set_prints_the_value_as_toml_writes_it(inst, capsys):
    _cli(inst, "set", "assumptions", "prime.rxpass.note", "priced at the pharmacy")
    assert '[prime.rxpass] note = "Recorded' in capsys.readouterr().out

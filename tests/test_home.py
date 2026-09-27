"""The instance: where penny's files resolve, the defaults merged under an
instance rules.toml, `penny init`, the board's override writes, and the
boundary that keeps an instance out of penny's own repo."""

import shutil
import stat
import subprocess
import tomllib
from pathlib import Path

import pytest

from penny import __main__ as cli
from penny import board, home, model, tomledit

REPO = Path(__file__).parent.parent
INSTANCE = Path(__file__).parent / "fixtures" / "home"


def test_resolve_flag_then_env_then_dot_home(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(home.ENV, raising=False)
    assert home.resolve() == tmp_path / "home"
    monkeypatch.setenv(home.ENV, str(tmp_path / "env"))
    assert home.resolve() == tmp_path / "env"
    assert home.resolve(tmp_path / "flag") == tmp_path / "flag"
    assert home.resolve("rel") == tmp_path / "rel"  # absolute against the cwd


def test_merge_is_table_by_table_and_arrays_replace():
    base = {"a": {"x": 1, "y": {"p": 1, "q": 2}}, "arr": [1, 2], "b": 1}
    over = {"a": {"y": {"q": 3}, "z": 4}, "arr": [9], "c": 2}
    assert home.merge(base, over) == {"a": {"x": 1, "y": {"p": 1, "q": 3}, "z": 4}, "arr": [9], "b": 1, "c": 2}
    assert base == {"a": {"x": 1, "y": {"p": 1, "q": 2}}, "arr": [1, 2], "b": 1}  # untouched


def test_defaults_hold_no_instance_tables():
    d = home.defaults()
    assert "held" not in d and "accounts" not in d
    assert d["families"] and d["cards"] and d["memberships"] and d["categories"]
    assert all("verified" in c for c in d["cards"].values())
    assert not any("accounts" in c for c in d["cards"].values())  # account numbers are the instance's


def test_instance_rules_merge_and_validate():
    rules = home.load_rules(INSTANCE / "rules.toml")
    raw = tomllib.loads((INSTANCE / "rules.toml").read_text())
    assert rules["held"] == raw["held"]
    assert rules["cards"]["sapphire_preferred"]["default_rate"] == raw["cards"]["sapphire_preferred"]["default_rate"]
    assert list(rules["families"]) == list(home.defaults()["families"])  # file order is match order
    with open(INSTANCE / "assumptions.toml", "rb") as f:
        model.validate_rules(rules, tomllib.load(f))


def test_init_writes_a_starter_and_never_overwrites(tmp_path, capsys):
    d = tmp_path / "inst"
    cli.main(["init", str(d)])
    assert stat.S_IMODE((d / "data").stat().st_mode) == 0o700
    rules = home.load_rules(d / "rules.toml")
    assert tomllib.loads((d / "rules.toml").read_text()) == {"held": {}}
    a = tomllib.loads((d / "assumptions.toml").read_text())
    assert all(v == 0 for sec in a.values() for perk in sec.values() if isinstance(perk, dict) for k, v in perk.items() if k in model.LEVELS)
    model.validate_rules(rules, a)
    (d / "rules.toml").write_text("# mine\n")
    with pytest.raises(SystemExit):
        cli.main(["init", str(d)])
    assert (d / "rules.toml").read_text() == "# mine\n"


def test_init_defaults_to_the_instance(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(home.ENV, raising=False)
    cli.main(["init"])
    assert (tmp_path / "home" / "rules.toml").is_file()


def test_override_adds_a_key_the_instance_lacks():
    text = "[held]\ncards = []\n"
    text = tomledit.override(text, "cards.prime_visa", "verified", None, True)
    assert text == "[held]\ncards = []\n\n[cards.prime_visa]\nverified = true\n"
    text = tomledit.override(text, "cards.prime_visa", "verified_on", None, "2026-09-30")
    text = tomledit.override(text, "cards.prime_visa", "rates", "gas", 0.03)
    text = tomledit.override(text, "cards.prime_visa", "rates", "amazon", 0.05)
    text = tomledit.override(text, "cards.prime_visa", "rates", "gas", 0.04)
    assert text.endswith('[cards.prime_visa]\nverified = true\nverified_on = "2026-09-30"\nrates = { gas = 0.04, amazon = 0.05 }\n')


def test_board_writes_overrides_into_the_instance_only(tmp_path):
    shutil.copy(INSTANCE / "rules.toml", tmp_path / "rules.toml")
    (tmp_path / "assumptions.toml").write_text("")
    defaults = home.DEFAULT_RULES.read_text()
    cfg = board.Config(tmp_path, tmp_path / "rules.toml", tmp_path / "assumptions.toml", None, None)
    res = board.record(cfg, [{"file": "rules", "section": "cards.costco_visa", "key": "verified", "value": True},
                             {"file": "rules", "section": "memberships.prime", "key": "fee", "value": 149}])
    assert [(r["key"], r["after"]) for r in res["recorded"]] == [("verified", True), ("fee", 149)]
    assert home.DEFAULT_RULES.read_text() == defaults
    rules = home.load_rules(tmp_path / "rules.toml")
    assert rules["cards"]["costco_visa"]["verified"] is True and rules["memberships"]["prime"]["fee"] == 149
    assert rules["memberships"]["prime"]["label"] == "Amazon Prime"  # the rest still from the defaults


def test_defaults_ship_in_the_wheel(tmp_path):
    uv = shutil.which("uv")
    if not uv:
        pytest.skip("uv not on PATH")
    r = subprocess.run([uv, "build", "--wheel", "-o", str(tmp_path), str(REPO)], capture_output=True, text=True, check=False)
    if r.returncode != 0:
        pytest.skip(f"wheel build unavailable: {r.stderr[-200:]}")
    import zipfile

    (whl,) = tmp_path.glob("*.whl")
    assert "penny/defaults/rules.toml" in zipfile.ZipFile(whl).namelist()


def test_no_instance_file_is_tracked():
    """The instance (home/) is its own private repo, git-ignored here: a stray
    `git add -f` must not put it into penny's."""
    r = subprocess.run(["git", "ls-files", "--", "home"], cwd=REPO, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        pytest.skip("not a git checkout")
    assert r.stdout.strip() == "", f"tracked under home/: {r.stdout.split()}"

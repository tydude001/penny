"""The instance: the directory holding one person's penny — ``rules.toml``,
``assumptions.toml`` and ``data/`` (overrides, merchants, the Plaid store,
Apple Card and statement files, the board's audit log). Every path penny
reads or writes on its own resolves against it; a path given on the command
line stays as given.

Which instance: ``--home``, else ``$PENNY_HOME``, else ``./home``.

``rules.toml`` in the instance is merged over the defaults shipped in
``penny/defaults/rules.toml``, table by table, the instance winning. Nothing
ever writes the defaults; the board writes overrides into the instance.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

ENV = "PENNY_HOME"
DEFAULT_DIR = Path("home")
DEFAULT_RULES = Path(__file__).parent / "defaults" / "rules.toml"


def resolve(flag: str | Path | None = None) -> Path:
    """The instance directory: ``flag``, else ``$PENNY_HOME``, else ``./home``,
    made absolute against the current directory."""
    raw = flag or os.environ.get(ENV) or DEFAULT_DIR
    return Path(raw).expanduser().absolute()


def merge(base: dict, over: dict) -> dict:
    """``over`` on top of ``base``: tables merge key by key, recursively; any
    other value (an array included) in ``over`` replaces ``base``'s. Keys keep
    ``base``'s order, with ``over``'s new ones after them."""
    out = dict(base)
    for k, v in over.items():
        out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def defaults() -> dict:
    with open(DEFAULT_RULES, "rb") as f:
        return tomllib.load(f)


def _held(rules: dict) -> dict:
    """A fresh instance's empty ``[held]`` measures edges against the flat 2%
    ``baseline`` card and holds nothing."""
    held = rules.setdefault("held", {})
    held.setdefault("baseline_card", "baseline")
    held.setdefault("cards", [])
    held.setdefault("memberships", [])
    return rules


def merged_text(text: str) -> dict:
    """The defaults with an instance ``rules.toml``'s text merged over them."""
    return _held(merge(defaults(), tomllib.loads(text)))


def load_rules(path: str | Path) -> dict:
    """An instance ``rules.toml`` merged over the defaults. The instance file
    must exist (``penny init`` writes one)."""
    with open(path, "rb") as f:
        return _held(merge(defaults(), tomllib.load(f)))


STARTER_RULES = """\
# Your penny instance. penny reads this over its defaults
# (penny/defaults/rules.toml: merchant families, budget categories, the card
# and membership catalogue), table by table: a key here wins over the same key
# there. Arrays are replaced whole, not merged.
#
# An override, for example valuing Sapphire points at 1.5 cents:
#   [cards.sapphire_preferred]
#   default_rate = 0.015
#   rates = { restaurants = 0.045, gas = 0.045, costco_gas = 0.045 }

[held]
# What you hold. Keys come from [cards.*] and [memberships.*] in the defaults.
# baseline_card = "baseline"     # the card every edge is measured against
# cards = ["prime_visa"]
# memberships = ["prime"]
# costco_tier = "gold_star"

# [accounts]
# Linked accounts by last four digits; the board shows each under its card.
# "1234" = { card = "prime_visa" }
"""

STARTER_ASSUMPTIONS = """\
# What each membership perk is worth to you a year, low / base / high, in
# dollars. Zero is the honest value unless you would pay for the perk; fill
# only what you'd buy. The board asks about each one and writes it here.

[prime.shipping]
note = "Orders that would have paid shipping"
low = 0
base = 0
high = 0

[prime.video]
note = "Prime Video you would otherwise pay for"
low = 0
base = 0
high = 0

[walmart_plus.delivery]
note = "Grocery delivery you would otherwise pay for"
low = 0
base = 0
high = 0

[walmart_plus.fuel]
note = "Per-gallon discount x gallons bought at participating stations"
low = 0
base = 0
high = 0

[costco.prices]
note = "Costco vs your usual store on the basket you buy there"
low = 0
base = 0
high = 0

[budget]
# A monthly budget per category, e.g. groceries = 600. The board's Budget page
# sets these.
"""


def init(d: str | Path) -> list[Path]:
    """Write a starter instance into ``d``: ``rules.toml`` with ``[held]``
    empty, an all-zero ``assumptions.toml`` and ``data/`` at mode 700.
    Refuses, before writing anything, if either file already exists."""
    d = Path(d)
    files = {d / "rules.toml": STARTER_RULES, d / "assumptions.toml": STARTER_ASSUMPTIONS}
    there = [p for p in files if p.exists()]
    if there:
        raise FileExistsError(", ".join(map(str, there)) + " already exists; not overwriting")
    d.mkdir(parents=True, exist_ok=True)
    written = []
    for p, text in files.items():
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        written.append(p)
    data = d / "data"
    if not data.exists():
        data.mkdir(mode=0o700)
        os.chmod(data, 0o700)  # mkdir's mode is masked by the umask
        written.append(data)
    return written

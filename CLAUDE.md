# penny — contributor notes

penny answers one question — is a paid membership and its store card worth
it — from your own transaction data, and grows a budget tracker on the same
feed. The pitch and the math are in `README.md`; this file is the traps and
conventions a pull request is checked against (see also `CONTRIBUTING.md`).

**A running instance may carry its own `home/CLAUDE.md`** with
instance-specific notes: which banks it links, quirks seen on that
instance's live feed, how its automation is wired, and the rest of what
doesn't belong in a public repo. That file is never part of this repo, isn't
read by anything here, and nothing in this file assumes one exists.

## Conventions

- **The program never carries anyone's data.** The generic half of
  `rules.toml` (families, categories, the card and membership catalogue)
  ships as `penny/defaults/rules.toml`. An instance's own `rules.toml` holds
  only `[held]`, `[accounts]` and any override, merged over the defaults on
  load, table by table. A rate fix belongs in the defaults, once, for
  everyone — never duplicated into an instance file.
- **Every path penny reads or writes resolves against the instance
  directory**: the `--home` flag, else `PENNY_HOME`, else `./home`.
  `penny/fsio.py` is the one atomic, fsynced file replace every writer uses;
  `penny/tomledit.py` is the line-based TOML editor that keeps every
  comment — a write that round-trips through a TOML parser/serializer
  instead will silently drop them.
- **The board never decides anything the CLI can't.** `penny board` renders
  state derived from the files and posts writes through the same
  allowlisted Record endpoint a CLI command could call; it holds no logic of
  its own.
- **`penny board` binds loopback by default.** `--tailscale` binds the
  Tailscale IPv4 for reaching it from another device on your own tailnet;
  `--host` still works but warns. Localhost is the only setup this repo
  calls supported until a login token exists (`SECURITY.md`).
- **No real financial data in fixtures, tests, issues or commits, ever.**
  `tests/` fixtures are synthetic. `penny demo` generates a throwaway
  instance for screenshots and manual testing; nothing outside it is
  written.
- **Rate and fee changes need a source.** Anything in
  `penny/defaults/rules.toml` marked `verified = false` is unconfirmed;
  flip it only with a link to the card or membership's own current terms.
- **Don't edit an existing test to make it pass.** If a change and a test
  disagree, say so in the pull request — the test may be the one that's
  right.
- **The trailing-365-day window annualises by scaling.** With less than a
  year of data, `penny report` says so rather than guessing; a two-month
  window × 6 is a guess, not a measurement.
- **Statement parsing shells out to `pdftotext` (poppler)**, but the test
  suite never calls the real binary — fixtures are `pdftotext -layout`
  output, and the missing-`pdftotext` path is exercised via monkeypatch — so
  `pdftotext` isn't a CI dependency.

## Running it

```sh
uv sync
uv run ruff check .
uv run pytest
uv run penny demo                          # synthetic year of data, opens the board
uv run penny report data/<export>.csv      # the verdict table
uv run penny board                         # decision board, loopback by default
```

See `README.md` for the CSV-import and Plaid quick start.

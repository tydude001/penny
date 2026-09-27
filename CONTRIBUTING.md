# Contributing to penny

Issues and pull requests are welcome. penny is maintained by one person in
their spare time, so a reply can take a while.

## Before you start

For anything bigger than a bug fix or a small rate correction, open an issue
first. `CLAUDE.md` explains the shape of the program, and a change
that fights the program/instance split (below) will usually be turned down
however good the code is.

## The rules a pull request is checked against

- **The board never decides anything the CLI can't.** Every choice `penny
  board` writes goes through the same code path a CLI command could call.
  The web page renders state and posts writes; it doesn't hold logic of its
  own.
- **Every file write goes through `penny/fsio.py`.** One atomic, fsynced
  replace, used by every writer, so a crash mid-write never leaves a
  half-written `rules.toml` or `overrides.json`.
- **TOML edits go through `penny/tomledit.py`.** It's a line-based editor
  that keeps every comment and the file's shape; a write that round-trips
  through `tomllib`/`tomli-w` instead will silently drop comments.
- **The program never carries anyone's data.** `rules.toml`'s and
  `assumptions.toml`'s generic parts (families, categories, the card and
  membership catalogue) live in `penny/defaults/`; what a person holds lives
  in their own instance directory (`home/` by default, `--home` / `PENNY_HOME`
  elsewhere), which is git-ignored and never committed here. A rate or fee
  fix belongs in the defaults, not an instance file.
- **No real financial data in fixtures, issues, or pull requests. Ever.**
  Test fixtures are synthetic. If you're filing a bug against your own data,
  describe the shape of the problem (a wrong sign, a category that never
  matches, a rate that's off) rather than pasting rows, balances, or account
  numbers.
- **Don't edit an existing test to make it pass.** If your change and a test
  disagree, say so in the pull request — the test may be the one that's
  right.
- **Rate and fee changes need a source.** Anything in `penny/defaults/rules.toml`
  marked `verified = false` is unconfirmed; flipping it to `true`, or
  changing a number that's already verified, needs a link to the card or
  membership's own terms in the pull request.

## Running the checks

```sh
uv sync
uv run ruff check .
uv run pytest
```

## Commits

Prefix commit messages with `feat:`, `fix:`, `chore:` or `docs:`.

## How a pull request lands

GitHub is a read-only mirror of the maintainer's own git server, so a pull
request is never merged with GitHub's merge button. The maintainer fetches
your branch, merges it on their side, and the next mirror sync carries it up;
GitHub then marks the pull request merged on its own once your head commit
reaches `main`. If the merge has to be squashed or reworked, the SHAs change
and the pull request is closed by hand with a note naming the commit your
work landed in. Either way, nothing is lost and you'll be credited in the
commit.

By contributing, you agree your contribution is licensed under this
project's [MIT licence](LICENSE).

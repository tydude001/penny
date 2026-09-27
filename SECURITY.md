# Security

## Reporting a vulnerability

Please report security problems privately, through GitHub's private
vulnerability reporting: the repository's **Security** tab, then **Report a
vulnerability**. Don't open a public issue for them.

## What counts

penny runs locally and is meant to be reached only from the machine it runs
on, but it holds your financial data and, if you've linked Plaid, your
access tokens — so anything that lets someone else reach or read past that
is in scope:

- **`penny board`** binds `127.0.0.1` by default. A way to reach it from off
  the machine without `--host` or `--tailscale`, or a way to read or write
  outside its own instance directory through the Record endpoint, is a
  vulnerability.
- **The instance boundary.** Every write goes through `penny/fsio.py`
  against a path resolved from the instance directory (`--home` /
  `PENNY_HOME` / `./home`). A way to make penny read or write outside that
  directory is a vulnerability.
- **Plaid credentials.** A way for penny to log, print, or write your Plaid
  client secret, access tokens, or account numbers anywhere outside your own
  instance directory is a vulnerability.

Not in scope: anything that needs an attacker to already run code as you,
`--host` used to expose the board deliberately without a login token (there
isn't one yet — see the README), and Plaid's own service.

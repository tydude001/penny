#!/usr/bin/env bash
# Prompt for Plaid keys and write them to data/plaid.env in the instance
# (--home DIR, else $PENNY_HOME, else ./home, relative to where you run it,
# exactly as penny resolves it; git-ignored, mode 600).
# Secrets are read without echo, so they never show on screen or in shell history.
set -euo pipefail

home=${PENNY_HOME:-home}
if [[ ${1:-} == --home ]]; then
    home=${2:?--home needs a directory}
elif [[ ${1:-} == --home=* ]]; then
    home=${1#--home=}
elif [[ $# -gt 0 ]]; then
    echo "usage: $0 [--home DIR]" >&2; exit 2
fi
out=$home/data/plaid.env

if [[ -e $out ]]; then
    read -rp "$out exists. Overwrite? [y/N] " yn
    [[ $yn == [yY]* ]] || { echo "Left unchanged."; exit 0; }
fi

ask() {  # ask <var> <prompt> <hidden>
    local val
    while :; do
        if [[ $3 == 1 ]]; then read -rsp "$2: " val; echo; else read -rp "$2: " val; fi
        val=${val//[[:space:]]/}
        [[ -n $val ]] && break
        echo "  Empty — paste it again."
    done
    printf -v "$1" '%s' "$val"
}

echo "Plaid Dashboard → Developers → Keys. Secrets won't echo as you paste."
ask client_id  "client_id"          0
ask sandbox    "Sandbox secret"     1
ask production "Production secret"  1

umask 077
mkdir -p "$(dirname "$out")"
# A new 0600 file renamed over the old, so the secrets are never in a file
# someone else can read, even one an earlier run left 0644.
tmp=$(mktemp "$(dirname "$out")/.plaid.env.XXXXXX")
trap 'rm -f "$tmp"' EXIT
printf 'PLAID_CLIENT_ID=%s\nPLAID_SECRET_SANDBOX=%s\nPLAID_SECRET_PRODUCTION=%s\n' \
    "$client_id" "$sandbox" "$production" > "$tmp"
chmod 600 "$tmp"
mv -f "$tmp" "$out"
trap - EXIT

echo "Wrote $out (mode 600): client_id ${#client_id} chars, sandbox secret ${#sandbox} chars, production secret ${#production} chars."
if git -C "$(dirname "$out")" rev-parse --git-dir >/dev/null 2>&1; then
    git -C "$(dirname "$out")" check-ignore -q "$(basename "$out")" && echo "git ignores it." || echo "WARNING: git does not ignore $out"
fi

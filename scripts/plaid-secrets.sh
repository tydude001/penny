#!/usr/bin/env bash
# Prompt for Plaid keys and write them to data/plaid.env in the instance
# ($PENNY_HOME, else ./home; git-ignored, mode 600).
# Secrets are read without echo, so they never show on screen or in shell history.
set -euo pipefail

cd "$(dirname "$0")/.."
out=${PENNY_HOME:-home}/data/plaid.env

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
printf 'PLAID_CLIENT_ID=%s\nPLAID_SECRET_SANDBOX=%s\nPLAID_SECRET_PRODUCTION=%s\n' \
    "$client_id" "$sandbox" "$production" > "$out"
chmod 600 "$out"

echo "Wrote $out (mode 600): client_id ${#client_id} chars, sandbox secret ${#sandbox} chars, production secret ${#production} chars."
git check-ignore -q "$out" && echo "git ignores it." || echo "WARNING: git does not ignore $out"

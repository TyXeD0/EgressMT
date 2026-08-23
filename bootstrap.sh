#!/usr/bin/env bash
set -Eeuo pipefail

REPO="TyXeD0/EgressMT"
API_URL="https://api.github.com/repos/${REPO}/commits/main"
TMP="$(mktemp /tmp/egressmt-install.XXXXXX)"

cleanup(){ rm -f "$TMP"; }
trap cleanup EXIT INT TERM

if [[ ! -r /dev/tty ]]; then
    echo "EgressMT requires an interactive terminal (/dev/tty)." >&2
    exit 1
fi

# Resolve main once, then run the whole installer from that immutable commit.
# This prevents a mixed installation when raw.githubusercontent.com/CDN still
# has one file from a previous commit cached while another file is already new.
HEAD_JSON="$(curl -fsSL --retry 4 --retry-delay 2 --retry-all-errors "$API_URL")"
HEAD_SHA="$(printf '%s' "$HEAD_JSON" | sed -nE 's/^[[:space:]]*"sha": "([0-9a-f]{40})",?$/\1/p' | head -n1)"
if [[ ! "$HEAD_SHA" =~ ^[0-9a-f]{40}$ ]]; then
    echo "EgressMT: cannot resolve repository HEAD." >&2
    exit 1
fi

INSTALL_URL="https://raw.githubusercontent.com/${REPO}/${HEAD_SHA}/install.sh"
curl -fsSL --retry 4 --retry-delay 2 --retry-all-errors "$INSTALL_URL" -o "$TMP"
chmod 700 "$TMP"

set +e
EGRESSMT_BRANCH="$HEAD_SHA" bash "$TMP" </dev/tty
rc=$?
set -e

cleanup
trap - EXIT INT TERM
exit "$rc"

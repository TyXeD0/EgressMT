#!/usr/bin/env bash
set -Eeuo pipefail

INSTALL_URL="https://raw.githubusercontent.com/TyXeD0/EgressMT/main/install.sh"
TMP="$(mktemp /tmp/egressmt-install.XXXXXX)"

cleanup(){ rm -f "$TMP"; }
trap cleanup EXIT INT TERM

if [[ ! -r /dev/tty ]]; then
    echo "EgressMT requires an interactive terminal (/dev/tty)." >&2
    exit 1
fi

curl -fsSL --retry 4 --retry-delay 2 --retry-all-errors "$INSTALL_URL" -o "$TMP"
chmod 700 "$TMP"

set +e
bash "$TMP" </dev/tty
rc=$?
set -e

cleanup
trap - EXIT INT TERM
exit "$rc"

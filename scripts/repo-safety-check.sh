#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

fail(){ echo "SAFETY CHECK FAILED: $*" >&2; exit 1; }

# Runtime secrets must never be committed. Variable-based config templates such
# as `PrivateKey = {private}` are allowed; literal key material is not.
if grep -RInE --exclude-dir=.git \
  'PrivateKey[[:space:]]*=[[:space:]]*[A-Za-z0-9+/]{42,44}={0,2}([[:space:]]|$)' .; then
  fail "literal AWG/WireGuard private key found"
fi

if grep -RInE --exclude-dir=.git \
  '(BEGIN (OPENSSH|RSA|EC|DSA) PRIVATE KEY|Authorization:[[:space:]]*Bearer[[:space:]]+[A-Za-z0-9._~-]{16,})' .; then
  fail "literal credential material found"
fi

if find . -type f \( -name '*.key' -o -name '*.token' -o -name '*.secret' -o -name '*.pem' \) -print -quit | grep -q .; then
  fail "secret-like file is tracked in the repository tree"
fi

# Public deployment addresses must never be committed. The only public IPv4
# literals allowed are Telegram network constants used by routing logic and the
# neutral resolver address used to determine the host's source IPv4. Private,
# loopback, link-local, multicast and unspecified addresses are also allowed.
python3 - <<'PY'
from pathlib import Path
import ipaddress
import re
import sys

allowed_public = [
    ipaddress.ip_network("91.108.56.0/22"),
    ipaddress.ip_network("91.108.4.0/22"),
    ipaddress.ip_network("91.108.8.0/22"),
    ipaddress.ip_network("91.108.16.0/22"),
    ipaddress.ip_network("91.108.12.0/22"),
    ipaddress.ip_network("149.154.160.0/20"),
    ipaddress.ip_network("91.105.192.0/23"),
    ipaddress.ip_network("91.108.20.0/22"),
    ipaddress.ip_network("185.76.151.0/24"),
]
neutral_public = {ipaddress.ip_address("1.1.1.1")}
pattern = re.compile(r"(?<![0-9])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9])")
violations = []

for path in Path(".").rglob("*"):
    if not path.is_file() or ".git" in path.parts:
        continue
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        continue
    for lineno, line in enumerate(text.splitlines(), 1):
        for raw in pattern.findall(line):
            try:
                ip = ipaddress.ip_address(raw)
            except ValueError:
                continue
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast:
                continue
            if ip in neutral_public or any(ip in net for net in allowed_public):
                continue
            violations.append(f"{path}:{lineno}: unexpected public IPv4 literal {ip}")

if violations:
    print("\n".join(violations), file=sys.stderr)
    raise SystemExit(1)
PY

echo "Repository safety check: OK"

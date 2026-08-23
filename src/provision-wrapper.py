#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import sys

CORE = Path("/usr/local/libexec/mtproxyl-egress-provision-core.py")

spec = importlib.util.spec_from_file_location("egressmt_provision_core", CORE)
if spec is None or spec.loader is None:
    raise SystemExit(f"cannot load provisioner core: {CORE}")
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)


def remote_packages(ssh) -> str:
    """Install AWG on a clean Ubuntu 24.04 EXIT without Launchpad REST API.

    add-apt-repository asks Launchpad's API for the PPA signing key. That API can
    fail independently of the actual apt repository. We instead fetch the
    published signing key directly, verify its full fingerprint, and configure
    the PPA with a signed-by Deb822 source.
    """
    script = r'''set -Eeuo pipefail
export DEBIAN_FRONTEND=noninteractive
. /etc/os-release
[[ "${ID:-}" == "ubuntu" && "${VERSION_ID:-}" == "24.04" ]] || {
  echo "Ubuntu 24.04 is required" >&2
  exit 78
}

APT=(apt-get -o DPkg::Lock::Timeout=120)
"${APT[@]}" update -y >/dev/null
"${APT[@]}" install -y \
  ca-certificates curl gnupg iproute2 nftables python3 dkms \
  "linux-headers-$(uname -r)" >/dev/null

if ! command -v awg >/dev/null 2>&1 || ! command -v awg-quick >/dev/null 2>&1; then
  FPR="75C9DD72C799870E310542E24166F2C257290828"
  KEYRING="/usr/share/keyrings/amnezia-archive-keyring.gpg"
  SOURCE="/etc/apt/sources.list.d/amnezia-ppa.sources"
  KEY="$(mktemp /tmp/egressmt-amnezia-key.XXXXXX)"
  trap 'rm -f "$KEY"' EXIT

  curl -fsSL --retry 5 --retry-delay 2 --retry-all-errors --connect-timeout 10 \
    "https://keyserver.ubuntu.com/pks/lookup?op=get&search=0x${FPR}" \
    -o "$KEY"

  GOT="$(gpg --batch --show-keys --with-colons "$KEY" 2>/dev/null | awk -F: '$1=="fpr"{print $10; exit}')"
  [[ "$GOT" == "$FPR" ]] || {
    echo "Amnezia PPA signing-key fingerprint mismatch" >&2
    exit 74
  }

  install -d -m 755 /usr/share/keyrings
  gpg --batch --yes --dearmor --output "$KEYRING" "$KEY"
  chmod 644 "$KEYRING"

  cat >"$SOURCE" <<EOF
Types: deb
URIs: https://ppa.launchpadcontent.net/amnezia/ppa/ubuntu
Suites: noble
Components: main
Signed-By: $KEYRING
EOF
  chmod 644 "$SOURCE"

  OK=0
  for DELAY in 0 3 10; do
    (( DELAY == 0 )) || sleep "$DELAY"
    if "${APT[@]}" update -y >/dev/null && apt-cache show amneziawg-tools >/dev/null 2>&1; then
      OK=1
      break
    fi
  done
  (( OK == 1 )) || {
    echo "Amnezia PPA unavailable or package metadata missing" >&2
    exit 75
  }

  "${APT[@]}" install -y amneziawg amneziawg-tools >/dev/null
fi

command -v awg >/dev/null
command -v awg-quick >/dev/null
ip -4 route show default | awk 'NR==1{print $5}'
'''
    out = ssh.exec(script, timeout=420).splitlines()
    if not out:
        raise RuntimeError("cannot detect remote external interface")
    return out[-1].strip()


def install_preflight() -> dict:
    """Validate provisioner files/tools before egressd is (re)started.

    install-core runs this while upgrading files and before it restarts systemd.
    The old core preflight also connected to the daemon Unix socket, which makes
    an otherwise valid fresh install/update fail if the socket is not present at
    that exact moment. Runtime daemon health is checked later by install-core via
    `egressmt status --json` after the service restart.
    """
    checks = {
        str(core.REGISTRY): Path(core.REGISTRY).exists(),
        str(core.AGENT_SOURCE): Path(core.AGENT_SOURCE).exists(),
        str(core.CONFIG): Path(core.CONFIG).exists(),
    }
    for cmd in ("awg", "awg-quick", "ssh", "sshpass"):
        checks[cmd] = shutil.which(cmd) is not None
    missing = [name for name, ok in checks.items() if not ok]
    if missing:
        raise RuntimeError("preflight failed: " + ", ".join(missing))
    return {
        "ok": True,
        "version": core.VERSION,
        "phase": "install",
        "checks": checks,
    }


core.remote_packages = remote_packages

if __name__ == "__main__":
    try:
        if len(sys.argv) == 2 and sys.argv[1] == "preflight":
            print(json.dumps(install_preflight(), ensure_ascii=False, indent=2))
        else:
            core.main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)

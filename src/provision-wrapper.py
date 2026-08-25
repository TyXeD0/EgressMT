#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import random
import secrets
import shutil
import sys
import tempfile

CORE = Path("/usr/local/libexec/mtproxyl-egress-provision-core.py")
AWG_PROFILE = "awg-3.1-full"
AWG_MTU = 1280
_REMOTE_HPK_SUPPORTED: bool | None = None

spec = importlib.util.spec_from_file_location("egressmt_provision_core", CORE)
if spec is None or spec.loader is None:
    raise SystemExit(f"cannot load provisioner core: {CORE}")
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)


def _probe_args(iface: str, key_path: str, hpk_path: str | None = None) -> list[str]:
    args = [
        "awg", "set", iface,
        "private-key", key_path,
        "jc", "4", "jmin", "32", "jmax", "96",
        "s1", "32", "s2", "32", "s3", "32", "s4", "32",
        "h1", "100000000-100001000",
        "h2", "1100000000-1100001000",
        "h3", "2100000000-2100001000",
        "h4", "3100000000-3100001000",
        "i1", "<r 8><rc 8><t>",
        "i2", "<rd 12><r 8>",
        "i3", "<rc 16><r 8>",
        "i4", "<r 16><t>",
        "i5", "<rd 8><rc 8><r 8>",
        "content-padding-addition", "8-24",
        "rekey-after-time", "110-130",
        "rekey-timeout", "4-7",
        "reject-after-time", "170-190",
        "keepalive-timeout", "8-12",
        "max-handshake-attempts", "16-24",
    ]
    if hpk_path:
        args += ["header-protection-key", hpk_path]
    return args


def local_awg31_probe(*, include_hpk: bool) -> bool:
    """Exercise the installed kernel module instead of trusting a version string."""
    iface = ("awgcap" + secrets.token_hex(3))[:15]
    key_path = hpk_path = None
    try:
        p = core.run(["ip", "link", "add", iface, "type", "amneziawg"], check=False)
        if p.returncode != 0:
            return False
        key = core.run(["awg", "genkey"]).stdout.strip()
        fd, key_path = tempfile.mkstemp(prefix="egressmt-awg-key-", dir="/run")
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(key + "\n")
        if include_hpk:
            hpk = core.run(["awg", "genkey"]).stdout.strip()
            fd, hpk_path = tempfile.mkstemp(prefix="egressmt-awg-hpk-", dir="/run")
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(hpk + "\n")
        result = core.run(_probe_args(iface, key_path, hpk_path), check=False)
        return result.returncode == 0
    finally:
        core.run(["ip", "link", "del", iface], check=False)
        for p in (key_path, hpk_path):
            if p:
                try:
                    os.unlink(p)
                except FileNotFoundError:
                    pass


def remote_packages(ssh) -> str:
    """Install current AWG and require AWG 3.1 obfuscation support on an EXIT.

    The Launchpad REST API is deliberately bypassed. The signing key is fetched
    directly, its full fingerprint is verified, and the PPA is configured with
    an explicit signed-by keyring. A throw-away amneziawg interface then checks
    every AWG 3.x knob used by EgressMT. HeaderProtectionKey is probed separately
    because some upstream kernel-module revisions expose the option in tools but
    reject it at netlink runtime; in that case the tunnel keeps every other 3.1
    obfuscation feature and HPK stays disabled until the installed module works.
    """
    global _REMOTE_HPK_SUPPORTED
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

probe_awg31() {
  local with_hpk="$1" iface="awgcap$$" key hpk rc=1
  key="$(mktemp /run/egressmt-awg-key.XXXXXX)"
  hpk="$(mktemp /run/egressmt-awg-hpk.XXXXXX)"
  chmod 600 "$key" "$hpk"
  awg genkey >"$key"
  awg genkey >"$hpk"
  ip link add "$iface" type amneziawg >/dev/null 2>&1 || { rm -f "$key" "$hpk"; return 1; }
  set +e
  ARGS=(awg set "$iface"
    private-key "$key"
    jc 4 jmin 32 jmax 96
    s1 32 s2 32 s3 32 s4 32
    h1 100000000-100001000
    h2 1100000000-1100001000
    h3 2100000000-2100001000
    h4 3100000000-3100001000
    i1 '<r 8><rc 8><t>'
    i2 '<rd 12><r 8>'
    i3 '<rc 16><r 8>'
    i4 '<r 16><t>'
    i5 '<rd 8><rc 8><r 8>'
    content-padding-addition 8-24
    rekey-after-time 110-130
    rekey-timeout 4-7
    reject-after-time 170-190
    keepalive-timeout 8-12
    max-handshake-attempts 16-24)
  [[ "$with_hpk" == 1 ]] && ARGS+=(header-protection-key "$hpk")
  "${ARGS[@]}" >/dev/null 2>&1
  rc=$?
  set -e
  ip link del "$iface" >/dev/null 2>&1 || true
  rm -f "$key" "$hpk"
  return "$rc"
}

probe_awg31 0 || {
  echo "installed AmneziaWG does not support the required AWG 3.1 profile" >&2
  exit 76
}
if probe_awg31 1; then
  echo "EGRESSMT_AWG_HPK=1"
else
  echo "EGRESSMT_AWG_HPK=0"
fi
ip -4 route show default | awk 'NR==1{print $5}'
'''
    out = ssh.exec(script, timeout=420).splitlines()
    if not out:
        raise RuntimeError("cannot detect remote external interface")
    _REMOTE_HPK_SUPPORTED = any(line.strip() == "EGRESSMT_AWG_HPK=1" for line in out)
    return out[-1].strip()


def _header_ranges(r: random.SystemRandom) -> list[str]:
    # Separate 32-bit windows guarantee that the four ranges can never overlap.
    windows = [
        (100_000_000, 850_000_000),
        (1_000_000_000, 1_750_000_000),
        (1_900_000_000, 2_650_000_000),
        (2_800_000_000, 3_950_000_000),
    ]
    result: list[str] = []
    for lo, hi in windows:
        width = r.randint(2048, 32768)
        start = r.randint(lo, hi - width)
        result.append(f"{start}-{start + width}")
    return result


def _signature_chain(r: random.SystemRandom) -> str:
    tags = [
        f"<r {r.randint(8, 40)}>",
        f"<rc {r.randint(8, 32)}>",
        f"<rd {r.randint(6, 24)}>",
    ]
    r.shuffle(tags)
    # Timestamp varies every handshake and avoids a byte-identical deployment signature.
    tags.insert(r.randint(0, len(tags)), "<t>")
    return "".join(tags)


def awg_params() -> dict[str, object]:
    """Generate a per-node AWG 3.1 profile with all usable obfuscation knobs."""
    r = random.SystemRandom()
    h1, h2, h3, h4 = _header_ranges(r)
    jmin = r.randint(32, 96)
    jmax = r.randint(max(jmin + 64, 160), 420)
    # S1-S4 stay >= 24 so HeaderProtectionKey, when supported, has enough nonce padding.
    params: dict[str, object] = {
        "Jc": r.randint(6, 12),
        "Jmin": jmin,
        "Jmax": jmax,
        "S1": r.randint(32, 128),
        "S2": r.randint(32, 128),
        "S3": r.randint(24, 96),
        "S4": r.randint(24, 72),
        "H1": h1,
        "H2": h2,
        "H3": h3,
        "H4": h4,
        "I1": _signature_chain(r),
        "I2": _signature_chain(r),
        "I3": _signature_chain(r),
        "I4": _signature_chain(r),
        "I5": _signature_chain(r),
        "ContentPaddingAddition": f"{r.randint(8, 20)}-{r.randint(32, 64)}",
        "RekeyAfterTime": f"{r.randint(105, 115)}-{r.randint(125, 140)}",
        "RekeyTimeout": f"{r.randint(4, 5)}-{r.randint(6, 8)}",
        "RejectAfterTime": f"{r.randint(165, 175)}-{r.randint(185, 200)}",
        "KeepaliveTimeout": f"{r.randint(8, 10)}-{r.randint(11, 15)}",
        "MaxHandshakeAttempts": f"{r.randint(16, 19)}-{r.randint(21, 26)}",
        "PersistentKeepalive": f"{r.randint(20, 23)}-{r.randint(27, 32)}",
        "MTU": AWG_MTU,
        "Profile": AWG_PROFILE,
    }
    local_hpk = local_awg31_probe(include_hpk=True)
    if local_hpk and _REMOTE_HPK_SUPPORTED:
        params["HeaderProtectionKey"] = core.run(["awg", "genkey"]).stdout.strip()
        params["HeaderProtection"] = True
    else:
        params["HeaderProtectionKey"] = ""
        params["HeaderProtection"] = False
    return params


def config_text(*, private: str, address: str, peer_public: str, allowed_cidr: str,
                params: dict[str, object], listen: int | None = None,
                endpoint: str | None = None) -> str:
    lines = [
        "[Interface]",
        f"PrivateKey = {private}",
        f"Address = {address}/30",
        "Table = off",
        f"MTU = {int(params['MTU'])}",
    ]
    if listen:
        lines.append(f"ListenPort = {listen}")
    for k in (
        "Jc", "Jmin", "Jmax",
        "S1", "S2", "S3", "S4",
        "H1", "H2", "H3", "H4",
        "I1", "I2", "I3", "I4", "I5",
        "ContentPaddingAddition",
        "RekeyAfterTime", "RekeyTimeout", "RejectAfterTime",
        "KeepaliveTimeout", "MaxHandshakeAttempts",
    ):
        lines.append(f"{k} = {params[k]}")
    if params.get("HeaderProtectionKey"):
        lines.append(f"HeaderProtectionKey = {params['HeaderProtectionKey']}")
    lines += [
        "",
        "[Peer]",
        f"PublicKey = {peer_public}",
        f"AllowedIPs = {allowed_cidr}",
    ]
    if endpoint:
        lines += [
            f"Endpoint = {endpoint}",
            f"PersistentKeepalive = {params['PersistentKeepalive']}",
        ]
    return "\n".join(lines) + "\n"


def install_preflight() -> dict:
    """Validate files/tools and the AWG 3.1 feature set before daemon restart."""
    checks = {
        str(core.REGISTRY): Path(core.REGISTRY).exists(),
        str(core.AGENT_SOURCE): Path(core.AGENT_SOURCE).exists(),
        str(core.CONFIG): Path(core.CONFIG).exists(),
    }
    for cmd in ("awg", "awg-quick", "ssh", "sshpass", "ip"):
        checks[cmd] = shutil.which(cmd) is not None
    missing = [name for name, ok in checks.items() if not ok]
    if missing:
        raise RuntimeError("preflight failed: " + ", ".join(missing))
    awg31 = local_awg31_probe(include_hpk=False)
    if not awg31:
        raise RuntimeError("preflight failed: installed AmneziaWG lacks required AWG 3.1 features")
    hpk = local_awg31_probe(include_hpk=True)
    return {
        "ok": True,
        "version": core.VERSION,
        "phase": "install",
        "checks": checks,
        "transport": {
            "profile": AWG_PROFILE,
            "mtu": AWG_MTU,
            "awg31": True,
            "header_protection_supported": hpk,
        },
    }


core.remote_packages = remote_packages
core.awg_params = awg_params
core.config_text = config_text

if __name__ == "__main__":
    try:
        if len(sys.argv) == 2 and sys.argv[1] == "preflight":
            print(json.dumps(install_preflight(), ensure_ascii=False, indent=2))
        else:
            core.main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)

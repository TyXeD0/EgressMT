#!/usr/bin/env python3
from __future__ import annotations

import base64
import contextlib
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import random
import re
import secrets
import shlex
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
from typing import Any

VERSION = "0.1.0-rc1"
ETC = Path("/etc/mtproxyl-egress")
NODES_DIR = ETC / "nodes.d"
TOKENS_DIR = ETC / "nodes"
SSH_DIR = ETC / "ssh"
CONFIG = ETC / "config.toml"
STATE_DIR = Path("/var/lib/mtproxyl-egress")
EVENTS = STATE_DIR / "events.log"
RUN_DIR = Path("/run/mtproxyl-egress")
LOCK = RUN_DIR / "provision.lock"
SOCKET = "/run/mtproxyl-egress/control.sock"
AGENT_SOURCE = Path("/usr/local/libexec/mtproxyl-node-agent-source")
REGISTRY = Path("/usr/local/libexec/mtproxyl-egress-registry")
AGENT_PORT = 9784
TEST_TELEGRAM_IP = "149.154.167.51"
TEST_TELEGRAM_PORT = 443
REMOTE_OWNER_DIR = "/etc/egressmt-node"
REMOTE_OWNER = REMOTE_OWNER_DIR + "/owner.json"
TG4 = [
    "91.108.56.0/22", "91.108.4.0/22", "91.108.8.0/22",
    "91.108.16.0/22", "91.108.12.0/22", "149.154.160.0/20",
    "91.105.192.0/23", "91.108.20.0/22", "185.76.151.0/24",
]


def fail(msg: str) -> "NoReturn":
    raise RuntimeError(msg)


def run(args: list[str], *, input_text: str | None = None, timeout: float = 30,
        check: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    p = subprocess.run(
        args, input=input_text, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=timeout, check=False, env=env,
    )
    if check and p.returncode != 0:
        fail(f"command failed ({p.returncode}): {args[0]}: {(p.stderr or p.stdout).strip()}")
    return p


def atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError): os.unlink(tmp)


def event(msg: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with EVENTS.open("a", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + " " + msg + "\n")


def load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as f: return tomllib.load(f)


def load_nodes() -> list[dict[str, Any]]:
    out = []
    for p in sorted(NODES_DIR.glob("*.toml")):
        n = load_toml(p); n["_path"] = str(p); out.append(n)
    return sorted(out, key=lambda n: (int(n.get("priority", 999999)), str(n.get("id", ""))))


def find_node(ref: str) -> dict[str, Any]:
    folded = ref.casefold()
    hits = [n for n in load_nodes() if folded in {
        str(n.get("id", "")).casefold(), str(n.get("name", "")).casefold(),
    }]
    if len(hits) != 1: fail("node not found or ambiguous")
    return hits[0]


def q(v: Any) -> str: return json.dumps(str(v), ensure_ascii=False)


def render_node(n: dict[str, Any]) -> str:
    fields = [
        ("id", q(n["id"])), ("name", q(n["name"])),
        ("enabled", "true" if bool(n.get("enabled", True)) else "false"),
        ("priority", str(int(n["priority"]))), ("endpoint", q(n.get("endpoint", ""))),
        ("public_ip", q(n.get("public_ip", ""))), ("ssh_host", q(n.get("ssh_host", n.get("public_ip", "")))),
        ("ssh_port", str(int(n.get("ssh_port", 22)))), ("ssh_user", q(n.get("ssh_user", "root"))),
        ("awg_interface", q(n["awg_interface"])), ("awg_port", str(int(n.get("awg_port", 0)))),
        ("local_tunnel_ip", q(n["local_tunnel_ip"])), ("remote_tunnel_ip", q(n["remote_tunnel_ip"])),
        ("routing_table", str(int(n["routing_table"]))), ("agent_port", str(int(n.get("agent_port", AGENT_PORT)))),
        ("agent_token_file", q(n.get("agent_token_file", ""))),
        ("provisioned", "true" if bool(n.get("provisioned", True)) else "false"),
    ]
    return "\n".join(f"{k} = {v}" for k, v in fields) + "\n"


def control(payload: dict[str, Any]) -> dict[str, Any]:
    raw = (json.dumps(payload, ensure_ascii=False) + "\n").encode()
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.settimeout(20)
    try:
        s.connect(SOCKET); s.sendall(raw); chunks = []
        while True:
            b = s.recv(65536)
            if not b: break
            chunks.append(b)
            if b"\n" in b: break
    finally: s.close()
    if not chunks: fail("egressd returned empty response")
    reply = json.loads(b"".join(chunks).split(b"\n", 1)[0])
    if not reply.get("ok"):
        err = reply.get("error") or {}; fail(str(err.get("message") or "egressd request failed"))
    return dict(reply.get("data") or {})


def validate_name(name: str) -> str:
    name = name.strip()
    if not name or len(name) > 64 or any(ord(c) < 32 or ord(c) == 127 for c in name):
        fail("node name must contain 1..64 printable characters")
    if any(str(n.get("name", "")).casefold() == name.casefold() for n in load_nodes()):
        fail("node name already exists")
    return name


def allocate(name: str, host: str, port: int, user: str, priority: int | None) -> dict[str, Any]:
    nodes = load_nodes(); used_ids = {str(n.get("id")) for n in nodes}
    while True:
        node_id = "n-" + secrets.token_hex(4)
        if node_id not in used_ids: break
    suffix = node_id[2:]; iface = "awg-" + suffix
    used_slots: set[int] = set(); used_tables = {int(n.get("routing_table", 0)) for n in nodes}
    for n in nodes:
        for key in ("local_tunnel_ip", "remote_tunnel_ip"):
            try:
                parts = str(ipaddress.IPv4Address(str(n.get(key, "")))).split(".")
                if parts[:2] == ["10", "253"]: used_slots.add(int(parts[2]))
            except Exception: pass
    slot = next((i for i in range(1, 255) if i not in used_slots), None)
    if slot is None: fail("no free tunnel slot")
    table = 52000 + slot
    while table in used_tables: table += 256
    if table > 65000: fail("no free routing table")
    if priority is None:
        priority = (max([int(n.get("priority", 0)) for n in nodes], default=0) // 10 + 1) * 10
    if not 1 <= priority <= 9999 or any(int(n.get("priority", 0)) == priority for n in nodes):
        fail("priority must be unique in range 1..9999")
    return {
        "id": node_id, "name": name, "enabled": True, "priority": priority,
        "endpoint": host, "public_ip": "", "ssh_host": host, "ssh_port": port, "ssh_user": user,
        "awg_interface": iface, "awg_port": random.SystemRandom().randint(20000, 59999),
        "local_tunnel_ip": f"10.253.{slot}.1", "remote_tunnel_ip": f"10.253.{slot}.2",
        "routing_table": table, "agent_port": AGENT_PORT,
        "agent_token_file": str(TOKENS_DIR / f"{node_id}.token"), "provisioned": True,
    }


class SSH:
    def __init__(self, host: str, port: int, user: str, auth: dict[str, Any]):
        self.host = host; self.port = port; self.user = user
        self.mode = str(auth.get("mode", "auto")); self.secret = str(auth.get("secret") or "")
        self.key_path = str(auth.get("key_path") or ""); self.temp_key: Path | None = None
        if self.mode not in {"auto", "password", "key"}: fail("auth mode must be auto/password/key")
        SSH_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.known_hosts = SSH_DIR / "known_hosts"
        self.base = [
            "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3",
            "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={self.known_hosts}",
        ]
        if self.mode == "auto": self.base += ["-o", "BatchMode=yes"]
        elif self.mode == "key":
            p = Path(self.key_path) if self.key_path else None
            if p and p.is_file(): key = p
            else:
                if not self.secret: fail("private key is empty")
                RUN_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
                fd, tmp = tempfile.mkstemp(prefix="ssh-key-", dir=str(RUN_DIR)); os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as f: f.write(self.secret.rstrip() + "\n")
                self.temp_key = Path(tmp); key = self.temp_key
            self.base += ["-i", str(key), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes"]
        elif not self.secret: fail("SSH password is empty")

    def close(self) -> None:
        if self.temp_key:
            with contextlib.suppress(FileNotFoundError): self.temp_key.unlink()

    def _prefix(self, tool: str) -> tuple[list[str], dict[str, str] | None]:
        env = None; cmd: list[str] = []
        if self.mode == "password":
            cmd += ["sshpass", "-e"]; env = dict(os.environ); env["SSHPASS"] = self.secret
        cmd.append(tool); return cmd, env

    def exec(self, script: str, *, timeout: float = 120) -> str:
        cmd, env = self._prefix("ssh")
        cmd += self.base + ["-p", str(self.port), f"{self.user}@{self.host}", "bash", "-s"]
        return run(cmd, input_text=script, timeout=timeout, env=env).stdout.strip()

    def copy(self, src: Path, dst: str, *, timeout: float = 60) -> None:
        cmd, env = self._prefix("scp"); opts: list[str] = []; i = 0
        while i < len(self.base):
            if self.base[i] in {"-o", "-i"}: opts += self.base[i:i+2]; i += 2
            else: i += 1
        cmd += opts + ["-P", str(self.port), str(src), f"{self.user}@{self.host}:{dst}"]
        run(cmd, timeout=timeout, env=env)


def remote_write(ssh: SSH, path: str, text: str, mode: int = 0o600) -> None:
    data = base64.b64encode(text.encode()).decode()
    ssh.exec(f"set -Eeuo pipefail; mkdir -p {shlex.quote(str(Path(path).parent))}; printf '%s' {shlex.quote(data)} | base64 -d > {shlex.quote(path)}; chmod {mode:o} {shlex.quote(path)}")


def remote_claim(ssh: SSH, n: dict[str, Any]) -> None:
    marker = json.dumps({"product":"EgressMT", "node_id":n["id"], "awg_interface":n["awg_interface"]}, separators=(",", ":"))
    script = f'''set -Eeuo pipefail
OWNER={shlex.quote(REMOTE_OWNER)}
NODE_ID={shlex.quote(str(n['id']))}
IFACE={shlex.quote(str(n['awg_interface']))}
if [[ -f "$OWNER" ]]; then
  python3 - "$OWNER" "$NODE_ID" "$IFACE" <<'PY'
import json,sys
p,nid,iface=sys.argv[1:]
d=json.load(open(p))
if d.get("product")!="EgressMT" or d.get("node_id")!=nid or d.get("awg_interface")!=iface:
    raise SystemExit("remote VPS is already claimed by another EgressMT node")
PY
  exit 0
fi
# Never delete or overwrite managed-looking resources unless this VPS has a matching owner marker.
for p in /etc/mtproxyl-node-agent /usr/local/bin/mtproxyl-node-agent /usr/local/sbin/mtproxyl-egress-node-firewall /etc/systemd/system/mtproxyl-node-agent.service /etc/systemd/system/mtproxyl-egress-node.service /etc/amnezia/amneziawg/$IFACE.conf; do
  [[ ! -e "$p" ]] || {{ echo "unclaimed managed resource exists: $p" >&2; exit 73; }}
done
ip link show dev "$IFACE" >/dev/null 2>&1 && {{ echo "unclaimed AWG interface exists: $IFACE" >&2; exit 73; }} || true
install -d -m 700 {shlex.quote(REMOTE_OWNER_DIR)}
printf '%s\n' {shlex.quote(marker)} > "$OWNER"
chmod 600 "$OWNER"
'''
    ssh.exec(script)


def remote_marker_matches(ssh: SSH, node_id: str, iface: str) -> bool:
    script = f'''set -Eeuo pipefail
python3 - {shlex.quote(REMOTE_OWNER)} {shlex.quote(node_id)} {shlex.quote(iface)} <<'PY'
import json,sys,os
p,nid,iface=sys.argv[1:]
if not os.path.isfile(p): raise SystemExit(2)
d=json.load(open(p))
raise SystemExit(0 if d.get("product")=="EgressMT" and d.get("node_id")==nid and d.get("awg_interface")==iface else 3)
PY
'''
    try: ssh.exec(script); return True
    except Exception: return False


def awg_params() -> dict[str, int]:
    r = random.SystemRandom(); jmin = r.randint(10, 40); jmax = r.randint(max(jmin + 30, 50), 127)
    hs: list[int] = []
    while len(hs) < 4:
        x = r.randint(1, 2_000_000_000)
        if x not in hs: hs.append(x)
    return {"Jc":r.randint(4,12), "Jmin":jmin, "Jmax":jmax, "S1":r.randint(20,150), "S2":r.randint(20,150), "H1":hs[0], "H2":hs[1], "H3":hs[2], "H4":hs[3]}


def config_text(*, private: str, address: str, peer_public: str, allowed_cidr: str,
                params: dict[str, int], listen: int | None = None, endpoint: str | None = None) -> str:
    lines = ["[Interface]", f"PrivateKey = {private}", f"Address = {address}/30", "Table = off"]
    if listen: lines.append(f"ListenPort = {listen}")
    for k in ("Jc","Jmin","Jmax","S1","S2","H1","H2","H3","H4"): lines.append(f"{k} = {params[k]}")
    lines += ["", "[Peer]", f"PublicKey = {peer_public}", f"AllowedIPs = {allowed_cidr}"]
    if endpoint: lines += [f"Endpoint = {endpoint}", "PersistentKeepalive = 25"]
    return "\n".join(lines) + "\n"


def remote_packages(ssh: SSH) -> str:
    script = r'''set -Eeuo pipefail
export DEBIAN_FRONTEND=noninteractive
. /etc/os-release
[[ "${ID:-}" == "ubuntu" ]] || { echo "unsupported OS" >&2; exit 78; }
apt-get update -y >/dev/null
apt-get install -y software-properties-common ca-certificates iproute2 nftables python3 >/dev/null
if ! command -v awg >/dev/null 2>&1 || ! command -v awg-quick >/dev/null 2>&1; then
  add-apt-repository -y ppa:amnezia/ppa >/dev/null
  apt-get update -y >/dev/null
  apt-get install -y amneziawg amneziawg-tools >/dev/null
fi
command -v awg >/dev/null
command -v awg-quick >/dev/null
ip -4 route show default | awk 'NR==1{print $5}'
'''
    out = ssh.exec(script, timeout=300).splitlines()
    if not out: fail("cannot detect remote external interface")
    return out[-1].strip()


def remote_public_ip(ssh: SSH) -> str:
    out = ssh.exec(r"set -e; ip -4 route get 1.1.1.1 | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -n1")
    ipaddress.IPv4Address(out.strip()); return out.strip()


def remote_firewall(ssh: SSH, n: dict[str, Any], ext: str) -> None:
    elems = ", ".join(TG4); iface=n["awg_interface"]; local=n["local_tunnel_ip"]; remote=n["remote_tunnel_ip"]; port=int(n["agent_port"])
    fw = f'''#!/usr/bin/env bash
set -Eeuo pipefail
nft delete table inet mtproxyl_egress_node 2>/dev/null || true
nft -f - <<'NFT'
table inet mtproxyl_egress_node {{
  set tg4 {{ type ipv4_addr; flags interval; elements = {{ {elems} }} }}
  chain forward {{
    type filter hook forward priority filter; policy accept;
    iifname "{iface}" ip saddr {local} ip daddr @tg4 accept
    iifname "{iface}" drop
  }}
  chain postrouting {{
    type nat hook postrouting priority srcnat; policy accept;
    iifname "{iface}" oifname "{ext}" ip saddr {local} ip daddr @tg4 masquerade
  }}
}}
NFT
nft delete table inet mtproxyl_node_agent 2>/dev/null || true
nft -f - <<'NFT'
table inet mtproxyl_node_agent {{
  chain input {{
    type filter hook input priority filter; policy accept;
    iifname "{iface}" ip saddr {local} ip daddr {remote} tcp dport {port} accept
    ip daddr {remote} tcp dport {port} drop
  }}
}}
NFT
'''
    remote_write(ssh, "/usr/local/sbin/mtproxyl-egress-node-firewall", fw, 0o755)
    unit = '''[Unit]\nDescription=EgressMT EXIT firewall\nAfter=network-online.target\nWants=network-online.target\n\n[Service]\nType=oneshot\nExecStart=/usr/local/sbin/mtproxyl-egress-node-firewall\nRemainAfterExit=yes\n\n[Install]\nWantedBy=multi-user.target\n'''
    remote_write(ssh, "/etc/systemd/system/mtproxyl-egress-node.service", unit, 0o644)
    ssh.exec(r'''set -Eeuo pipefail
install -d -m 700 /etc/egressmt-node
if [[ ! -f /etc/egressmt-node/ip_forward.before ]]; then
  sysctl -n net.ipv4.ip_forward > /etc/egressmt-node/ip_forward.before
  chmod 600 /etc/egressmt-node/ip_forward.before
fi
printf 'net.ipv4.ip_forward=1\n' >/etc/sysctl.d/99-egressmt.conf
sysctl --system >/dev/null
systemctl daemon-reload
systemctl enable --now mtproxyl-egress-node.service >/dev/null
''')


def remote_agent(ssh: SSH, n: dict[str, Any], token: str) -> None:
    ssh.copy(AGENT_SOURCE, "/tmp/egressmt-node-agent")
    cfg = f"NODE_NAME={n['name']}\nBIND_IP={n['remote_tunnel_ip']}\nPORT={int(n['agent_port'])}\nALLOWED_SOURCE={n['local_tunnel_ip']}\n"
    remote_write(ssh, "/etc/mtproxyl-node-agent/config.env", cfg, 0o640)
    remote_write(ssh, "/etc/mtproxyl-node-agent/token", token + "\n", 0o640)
    unit = '''[Unit]\nDescription=EgressMT EXIT node agent\nAfter=network-online.target\nWants=network-online.target\n\n[Service]\nType=simple\nUser=mtproxyl-node-agent\nGroup=mtproxyl-node-agent\nExecStart=/usr/local/bin/mtproxyl-node-agent\nRestart=always\nRestartSec=2\nNoNewPrivileges=yes\nPrivateTmp=yes\nProtectSystem=strict\nProtectHome=yes\nReadWritePaths=/run\n\n[Install]\nWantedBy=multi-user.target\n'''
    remote_write(ssh, "/etc/systemd/system/mtproxyl-node-agent.service", unit, 0o644)
    ssh.exec(r'''set -Eeuo pipefail
id mtproxyl-node-agent >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin mtproxyl-node-agent
install -o root -g root -m 755 /tmp/egressmt-node-agent /usr/local/bin/mtproxyl-node-agent
rm -f /tmp/egressmt-node-agent
chown root:mtproxyl-node-agent /etc/mtproxyl-node-agent/config.env /etc/mtproxyl-node-agent/token
chmod 640 /etc/mtproxyl-node-agent/config.env /etc/mtproxyl-node-agent/token
systemctl daemon-reload
systemctl enable --now mtproxyl-node-agent.service >/dev/null
''')


def remote_cleanup(ssh: SSH, node_id: str, iface: str) -> None:
    if not remote_marker_matches(ssh, node_id, iface):
        fail("remote cleanup refused: missing or mismatched EgressMT ownership marker")
    script = f'''set -Eeuo pipefail
systemctl disable --now mtproxyl-node-agent.service >/dev/null 2>&1 || true
systemctl disable --now mtproxyl-egress-node.service >/dev/null 2>&1 || true
systemctl disable --now awg-quick@{shlex.quote(iface)}.service >/dev/null 2>&1 || true
systemctl reset-failed awg-quick@{shlex.quote(iface)}.service >/dev/null 2>&1 || true
rm -f /etc/amnezia/amneziawg/{shlex.quote(iface)}.conf
rm -f /etc/systemd/system/mtproxyl-node-agent.service /etc/systemd/system/mtproxyl-egress-node.service
rm -rf /etc/mtproxyl-node-agent
rm -f /usr/local/bin/mtproxyl-node-agent /usr/local/sbin/mtproxyl-egress-node-firewall /tmp/egressmt-node-agent
nft delete table inet mtproxyl_egress_node 2>/dev/null || true
nft delete table inet mtproxyl_node_agent 2>/dev/null || true
BEFORE=""
[[ -f /etc/egressmt-node/ip_forward.before ]] && BEFORE="$(cat /etc/egressmt-node/ip_forward.before 2>/dev/null || true)"
rm -f /etc/sysctl.d/99-egressmt.conf
if [[ "$BEFORE" =~ ^[01]$ ]]; then sysctl -w net.ipv4.ip_forward="$BEFORE" >/dev/null 2>&1 || true; fi
rm -rf /etc/egressmt-node
id mtproxyl-node-agent >/dev/null 2>&1 && userdel mtproxyl-node-agent >/dev/null 2>&1 || true
systemctl daemon-reload
'''
    ssh.exec(script, timeout=60)


def cleanup_enter_nft(iface: str) -> None:
    p = run(["nft", "-a", "list", "chain", "inet", "mtproxyl_egress", "postrouting"], check=False)
    if p.returncode != 0: return
    for line in p.stdout.splitlines():
        if f'oifname "{iface}"' not in line: continue
        m = re.search(r"# handle ([0-9]+)\s*$", line)
        if m: run(["nft", "delete", "rule", "inet", "mtproxyl_egress", "postrouting", "handle", m.group(1)], check=False)


def candidate_route_up(n: dict[str, Any]) -> None:
    used = set()
    for line in run(["ip", "rule", "show"], check=False).stdout.splitlines():
        m = re.match(r"^([0-9]+):", line.strip())
        if m: used.add(int(m.group(1)))
    prio = next((p for p in range(10700,10800) if p not in used), None)
    if prio is None: fail("no free temporary policy-rule priority")
    iface=str(n["awg_interface"]); local=str(n["local_tunnel_ip"]); table=str(int(n["routing_table"])); n["_candidate_rule_priority"]=prio
    run(["ip","route","replace","blackhole","default","metric","32760","table",table])
    run(["ip","route","replace","default","dev",iface,"src",local,"metric","10","table",table])
    run(["ip","rule","add","priority",str(prio),"from",f"{local}/32","lookup",table])
    p=run(["ip","route","get",TEST_TELEGRAM_IP,"from",local,"mark","0x200000"],check=False)
    if p.returncode != 0 or f"dev {iface}" not in p.stdout:
        candidate_route_down(n, flush=True); fail("candidate Telegram probe is not routed through new AWG interface")


def candidate_route_down(n: dict[str, Any], *, flush: bool=False) -> None:
    prio=n.pop("_candidate_rule_priority",None)
    if prio is not None:
        while run(["ip","rule","del","priority",str(int(prio))],check=False).returncode==0: pass
    if flush: run(["ip","route","flush","table",str(int(n["routing_table"]))],check=False)


def local_cleanup(n: dict[str, Any]) -> None:
    iface=str(n["awg_interface"]); candidate_route_down(n,flush=True); unit=f"awg-quick@{iface}.service"
    run(["systemctl","disable","--now",unit],check=False); run(["systemctl","reset-failed",unit],check=False); cleanup_enter_nft(iface)
    with contextlib.suppress(FileNotFoundError): (Path("/etc/amnezia/amneziawg")/f"{iface}.conf").unlink()
    token=Path(str(n.get("agent_token_file", "")))
    if str(token):
        with contextlib.suppress(FileNotFoundError): token.unlink()


def ping_tunnel(iface: str, remote: str, timeout_sec: int=30) -> float:
    deadline=time.monotonic()+timeout_sec; last=""
    while time.monotonic()<deadline:
        p=run(["ping","-I",iface,"-c","1","-W","1",remote],check=False,timeout=3); last=p.stderr or p.stdout
        if p.returncode==0:
            m=re.search(r"time[=<]([0-9.]+)\s*ms",p.stdout); return float(m.group(1)) if m else 0.0
        time.sleep(1)
    fail("AWG tunnel ping failed: "+last.strip())


def telegram_tcp(local_ip: str) -> float:
    s=socket.socket(socket.AF_INET,socket.SOCK_STREAM); s.settimeout(5); started=time.monotonic()
    try: s.bind((local_ip,0)); s.connect((TEST_TELEGRAM_IP,TEST_TELEGRAM_PORT)); return round((time.monotonic()-started)*1000,1)
    finally: s.close()


def agent_check(n: dict[str, Any], token: str) -> None:
    import urllib.request
    req=urllib.request.Request(f"http://{n['remote_tunnel_ip']}:{int(n['agent_port'])}/health",headers={"Authorization":"Bearer "+token})
    deadline=time.monotonic()+20; last=None
    while time.monotonic()<deadline:
        try:
            with urllib.request.urlopen(req,timeout=3) as r: data=json.loads(r.read())
            if data.get("ok"): return
        except Exception as exc: last=exc
        time.sleep(1)
    fail(f"node-agent health failed: {type(last).__name__ if last else 'unknown'}")


def add_node(req: dict[str, Any]) -> dict[str, Any]:
    name=validate_name(str(req.get("name") or "")); host=str(req.get("host") or "").strip()
    if not host or len(host)>253 or any(c.isspace() for c in host): fail("invalid SSH host")
    port=int(req.get("port") or 22); user=str(req.get("user") or "root").strip()
    if not 1<=port<=65535 or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,31}",user): fail("invalid SSH connection settings")
    priority=req.get("priority"); n=allocate(name,host,port,user,int(priority) if priority is not None else None)
    ssh=SSH(host,port,user,dict(req.get("auth") or {})); local_cfg=Path("/etc/amnezia/amneziawg")/f"{n['awg_interface']}.conf"
    registered=False; remote_claimed=False; token=secrets.token_urlsafe(32)
    try:
        if ssh.exec("id -u").strip()!="0": fail("SSH user must be root")
        remote_claim(ssh,n); remote_claimed=True
        ext=remote_packages(ssh); n["public_ip"]=remote_public_ip(ssh)
        local_priv=run(["awg","genkey"]).stdout.strip(); local_pub=run(["awg","pubkey"],input_text=local_priv+"\n").stdout.strip()
        remote_priv=ssh.exec("umask 077; awg genkey").strip(); remote_pub=ssh.exec(f"printf '%s\\n' {shlex.quote(remote_priv)} | awg pubkey").strip(); params=awg_params()
        local_text=config_text(private=local_priv,address=n["local_tunnel_ip"],peer_public=remote_pub,allowed_cidr="0.0.0.0/0",params=params,endpoint=f"{host}:{n['awg_port']}")
        remote_text=config_text(private=remote_priv,address=n["remote_tunnel_ip"],peer_public=local_pub,allowed_cidr=f"{n['local_tunnel_ip']}/32",params=params,listen=int(n["awg_port"]))
        remote_write(ssh,f"/etc/amnezia/amneziawg/{n['awg_interface']}.conf",remote_text,0o600)
        remote_unit=f"awg-quick@{n['awg_interface']}.service"
        ssh.exec(f"set -Eeuo pipefail; systemctl daemon-reload; systemctl enable --now {shlex.quote(remote_unit)}; systemctl is-active --quiet {shlex.quote(remote_unit)}; ip link show dev {shlex.quote(str(n['awg_interface']))} >/dev/null; awg show {shlex.quote(str(n['awg_interface']))} >/dev/null")
        local_cfg.parent.mkdir(parents=True,exist_ok=True); atomic_write(local_cfg,local_text,0o600); local_unit=f"awg-quick@{n['awg_interface']}.service"
        run(["systemctl","daemon-reload"]); run(["systemctl","enable","--now",local_unit]); run(["systemctl","is-active","--quiet",local_unit]); run(["ip","link","show","dev",str(n["awg_interface"])]); run(["awg","show",str(n["awg_interface"])])
        remote_firewall(ssh,n,ext); remote_agent(ssh,n,token)
        rtt=ping_tunnel(str(n["awg_interface"]),str(n["remote_tunnel_ip"])); candidate_route_up(n)
        try: tg_ms=telegram_tcp(str(n["local_tunnel_ip"]))
        finally: candidate_route_down(n,flush=False)
        agent_check(n,token)
        TOKENS_DIR.mkdir(parents=True,exist_ok=True); atomic_write(Path(str(n["agent_token_file"])),token+"\n",0o600)
        NODES_DIR.mkdir(parents=True,exist_ok=True); atomic_write(NODES_DIR/f"{n['id']}.toml",render_node(n),0o600); registered=True
        run([str(REGISTRY),"validate"]); control({"action":"reload"}); time.sleep(1); test=control({"action":"node_test","node":str(n["id"])})
        if not test.get("health"): fail("node registered but dynamic health check is DOWN")
        event(f"node_add id={n['id']} name={name!r} priority={n['priority']}")
        return {"id":n["id"],"name":n["name"],"public_ip":n["public_ip"],"priority":n["priority"],"interface":n["awg_interface"],"tunnel_rtt_ms":rtt,"telegram_tcp_ms":tg_ms,"health":True}
    except Exception:
        if registered:
            with contextlib.suppress(FileNotFoundError): (NODES_DIR/f"{n['id']}.toml").unlink()
            with contextlib.suppress(Exception): control({"action":"reload"})
        local_cleanup(n)
        if remote_claimed:
            with contextlib.suppress(Exception): remote_cleanup(ssh,str(n["id"]),str(n["awg_interface"]))
        raise
    finally: ssh.close()


def wait_not_active(node_id: str, timeout: int=45) -> None:
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        if control({"action":"status"}).get("active_node")!=node_id: return
        time.sleep(1)
    fail("timed out waiting for production to leave node")


def remove_node(req: dict[str, Any]) -> dict[str, Any]:
    n=find_node(str(req.get("node") or "")); node_id=str(n["id"]); nodes=load_nodes(); status=control({"action":"status"}); fallback=str(req.get("fallback") or "").casefold()
    others=[x for x in nodes if str(x["id"])!=node_id and bool(x.get("enabled",True))]
    if status.get("mode")=="manual" and status.get("manual_node")==node_id: control({"action":"set_mode","mode":"auto"})
    if status.get("active_node")==node_id:
        if others:
            control({"action":"set_mode","mode":"auto"}); control({"action":"node_disable","node":node_id}); wait_not_active(node_id)
        else:
            if fallback not in {"block","direct"}: fail("deleting last active node requires fallback=block or direct")
            control({"action":"set_mode","mode":fallback}); wait_not_active(node_id)
    else:
        with contextlib.suppress(Exception): control({"action":"node_disable","node":node_id})
    remote_result="not_requested"; auth=dict(req.get("auth") or {})
    if bool(req.get("remote_cleanup")):
        ssh=SSH(str(n.get("ssh_host") or n.get("public_ip")),int(n.get("ssh_port",22)),str(n.get("ssh_user","root")),auth)
        try:
            remote_cleanup(ssh,node_id,str(n["awg_interface"])); remote_result="ok"
        except Exception as exc: remote_result=f"refused_or_failed:{type(exc).__name__}:{exc}"
        finally: ssh.close()
    local_cleanup(n)
    with contextlib.suppress(FileNotFoundError): Path(str(n["_path"])).unlink()
    run([str(REGISTRY),"validate"]); control({"action":"reload"}); cleanup_enter_nft(str(n["awg_interface"])); run(["ip","route","flush","table",str(int(n["routing_table"]))],check=False)
    event(f"node_remove id={node_id} name={n['name']!r} remote_cleanup={remote_result}")
    return {"id":node_id,"name":n["name"],"removed":True,"remote_cleanup":remote_result}


def preflight() -> dict[str, Any]:
    checks={str(p):p.exists() for p in (REGISTRY,AGENT_SOURCE,CONFIG)}; checks["socket"]=Path(SOCKET).exists()
    for c in ("awg","awg-quick","ssh","sshpass"): checks[c]=run(["bash","-lc",f"command -v {c}"],check=False).returncode==0
    if not all(checks.values()): fail("preflight failed: "+", ".join(k for k,v in checks.items() if not v))
    st=control({"action":"status"}); return {"ok":True,"version":VERSION,"nodes":len(st.get("nodes") or []),"active":st.get("active_node")}


def locked_call(action: str, req: dict[str, Any]) -> dict[str, Any]:
    RUN_DIR.mkdir(parents=True,exist_ok=True,mode=0o700)
    with LOCK.open("w") as f:
        try: fcntl.flock(f.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: fail("another add/remove operation is already running")
        if action=="add": return add_node(req)
        if action=="remove": return remove_node(req)
        fail("unknown action")


def main() -> None:
    import argparse
    ap=argparse.ArgumentParser(description="EgressMT EXIT provisioner"); ap.add_argument("--version",action="store_true"); sub=ap.add_subparsers(dest="cmd"); sub.add_parser("preflight"); p=sub.add_parser("request"); p.add_argument("--pretty",action="store_true"); args=ap.parse_args()
    if args.version: print(f"egressmt-provision {VERSION}"); return
    if args.cmd=="preflight": print(json.dumps(preflight(),ensure_ascii=False,indent=2)); return
    if args.cmd=="request":
        req=json.load(sys.stdin); action=str(req.get("action") or "")
        if action not in {"add","remove"}: fail("request action must be add/remove")
        print(json.dumps(locked_call(action,req),ensure_ascii=False,indent=2 if args.pretty else None)); return
    ap.print_help()


if __name__=="__main__":
    try: main()
    except Exception as exc:
        print(f"ERROR: {exc}",file=sys.stderr); raise SystemExit(1)

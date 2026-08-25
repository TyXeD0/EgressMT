#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import datetime as dt
import fcntl
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import socketserver
import subprocess
import tempfile
import threading
import time
import tomllib
import urllib.request
from typing import Any

VERSION = "0.1.0-rc1"

ETC = Path("/etc/mtproxyl-egress")
CONFIG = ETC / "config.toml"
NODES_DIR = ETC / "nodes.d"
STATE_DIR = Path("/var/lib/mtproxyl-egress")
CONTROL_FILE = STATE_DIR / "control.json"
STATE_FILE = STATE_DIR / "dynamic-status.json"
ACTIVE_FILE = STATE_DIR / "active"
EVENTS_FILE = STATE_DIR / "events.log"
SOCKET_PATH = Path("/run/mtproxyl-egress/control.sock")
LOCK_PATH = Path("/run/mtproxyl-egress/daemon.lock")

TELEGRAM_PROBES = [
    ("dc1", "149.154.175.50"),
    ("dc2", "149.154.167.51"),
    ("dc3", "149.154.175.100"),
    ("dc4", "149.154.167.91"),
    ("dc5", "91.108.56.151"),
]
TELEGRAM_PROBE_PORT = 443
PROBE_RULE_START = 10800
PROBE_RULE_END = 10899
TEMP_RULE_PRIORITY = 10999
TELEGRAM_V4 = [
    "91.108.56.0/22",
    "91.108.4.0/22",
    "91.108.8.0/22",
    "91.108.16.0/22",
    "91.108.12.0/22",
    "149.154.160.0/20",
    "91.105.192.0/23",
    "91.108.20.0/22",
    "185.76.151.0/24",
]

_stop = threading.Event()


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def log(msg: str) -> None:
    print(f"{now_iso()} {msg}", flush=True)


def run(args: list[str], timeout: float = 8, check: bool = False) -> subprocess.CompletedProcess[str]:
    try:
        p = subprocess.run(
            args,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        if check:
            raise RuntimeError(f"timeout: {' '.join(args)}") from exc
        return subprocess.CompletedProcess(args, 124, "", "timeout")
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(args)}: {p.stderr.strip() or p.stdout.strip()}")
    return p


def atomic_text(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)


def load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as f:
        return tomllib.load(f)


def load_nodes() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in sorted(NODES_DIR.glob("*.toml")):
        n = load_toml(p)
        n["_path"] = str(p)
        out.append(n)
    return sorted(out, key=lambda n: (int(n.get("priority", 999999)), str(n.get("id", ""))))


def find_node(nodes: list[dict[str, Any]], ref: str) -> dict[str, Any]:
    folded = ref.casefold()
    hits = [n for n in nodes if folded in {
        str(n.get("id", "")).casefold(),
        str(n.get("name", "")).casefold(),
    }]
    if len(hits) != 1:
        raise ValueError(f"node not found or ambiguous: {ref}")
    return hits[0]


def event(message: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with EVENTS_FILE.open("a", encoding="utf-8") as f:
        f.write(f"{now_iso()} {message}\n")


def save_control(data: dict[str, Any]) -> None:
    atomic_text(CONTROL_FILE, json.dumps(data, ensure_ascii=False, indent=2) + "\n", 0o600)


def load_control(config: dict[str, Any], nodes: list[dict[str, Any]]) -> dict[str, Any]:
    if CONTROL_FILE.exists():
        try:
            data = json.loads(CONTROL_FILE.read_text(encoding="utf-8"))
            if data.get("mode") in {"auto", "direct", "block", "manual"}:
                return {"mode": data["mode"], "manual_node": data.get("manual_node")}
        except Exception:
            pass

    mode = str(config.get("mode", "auto")).casefold()
    if mode in {"auto", "direct", "block"}:
        data = {"mode": mode, "manual_node": None}
    else:
        try:
            n = find_node(nodes, mode)
            data = {"mode": "manual", "manual_node": n["id"]}
        except Exception:
            data = {"mode": "auto", "manual_node": None}
    save_control(data)
    return data


def direct_public_ipv4() -> str:
    p = run(["ip", "-4", "route", "get", "1.1.1.1"], check=True)
    m = re.search(r"\bsrc\s+([0-9.]+)", p.stdout)
    if not m:
        raise RuntimeError("cannot determine ENTER public IPv4")
    ipaddress.IPv4Address(m.group(1))
    return m.group(1)


def ensure_routes(nodes: list[dict[str, Any]], config: dict[str, Any]) -> None:
    for n in nodes:
        table = str(int(n["routing_table"]))
        iface = str(n["awg_interface"])
        local = str(n["local_tunnel_ip"])
        run(["ip", "route", "replace", "blackhole", "default", "metric", "32760", "table", table], check=True)
        if (Path("/sys/class/net") / iface).exists():
            p = run(["ip", "route", "replace", "default", "dev", iface, "src", local, "metric", "10", "table", table])
            if p.returncode != 0:
                log(f"route reconcile warning for {n['name']}: {p.stderr.strip()}")

    priorities: list[int] = []
    for line in run(["ip", "rule", "show"]).stdout.splitlines():
        m = re.match(r"^(\d+):", line.strip())
        if m and PROBE_RULE_START <= int(m.group(1)) <= PROBE_RULE_END:
            priorities.append(int(m.group(1)))
    for prio in sorted(set(priorities), reverse=True):
        while run(["ip", "rule", "del", "priority", str(prio)]).returncode == 0:
            pass

    for idx, n in enumerate(nodes[: PROBE_RULE_END - PROBE_RULE_START + 1]):
        run([
            "ip", "rule", "add", "priority", str(PROBE_RULE_START + idx),
            "from", f"{n['local_tunnel_ip']}/32",
            "lookup", str(int(n["routing_table"])),
        ], check=True)

    block_table = str(int(config["routing"].get("block_table", 51839)))
    run(["ip", "route", "replace", "blackhole", "default", "table", block_table], check=True)


def nft_script(text: str) -> None:
    p = subprocess.run(
        ["nft", "-f", "-"], input=text, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=8, check=False,
    )
    if p.returncode != 0:
        raise RuntimeError(f"nft: {p.stderr.strip() or p.stdout.strip()}")


def ensure_nft(nodes: list[dict[str, Any]]) -> None:
    p = run(["nft", "list", "table", "inet", "mtproxyl_egress"])
    if p.returncode != 0:
        elements = ", ".join(TELEGRAM_V4)
        nat_rules = "\n".join(
            f'        oifname "{n["awg_interface"]}" ip daddr @tg4 counter masquerade'
            for n in nodes
        )
        rules = f"""
table inet mtproxyl_egress {{
    set tg4 {{
        type ipv4_addr
        flags interval
        elements = {{ {elements} }}
    }}
    chain output {{
        type route hook output priority -150; policy accept;
        ip daddr @tg4 counter meta mark set meta mark | 0x00200000
    }}
    chain postrouting {{
        type nat hook postrouting priority srcnat; policy accept;
{nat_rules}
    }}
}}
"""
        nft_script(rules)
        p = run(["nft", "list", "table", "inet", "mtproxyl_egress"], check=True)

    text = p.stdout
    if "@tg4" not in text or "meta mark set" not in text:
        raise RuntimeError("EgressMT nft table is incomplete")

    for n in nodes:
        iface = str(n["awg_interface"])
        needle = f'oifname "{iface}" ip daddr @tg4'
        if needle in text:
            continue
        q = run([
            "nft", "add", "rule", "inet", "mtproxyl_egress", "postrouting",
            "oifname", iface, "ip", "daddr", "@tg4", "counter", "masquerade",
        ])
        if q.returncode != 0:
            raise RuntimeError(f"failed to add nft NAT for {iface}: {q.stderr.strip()}")


def latest_handshake_age(iface: str) -> int:
    if not (Path("/sys/class/net") / iface).exists():
        return -1
    p = run(["awg", "show", iface, "latest-handshakes"])
    if p.returncode != 0:
        return -1
    epochs: list[int] = []
    for line in p.stdout.splitlines():
        parts = line.split()
        if parts and parts[-1].isdigit():
            epochs.append(int(parts[-1]))
    latest = max(epochs, default=0)
    return -1 if latest <= 0 else max(0, int(time.time()) - latest)


def awg_transfer(iface: str) -> tuple[int, int]:
    p = run(["awg", "show", iface, "transfer"])
    rx = tx = 0
    if p.returncode == 0:
        for line in p.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 3:
                with contextlib.suppress(ValueError):
                    rx += int(parts[-2]); tx += int(parts[-1])
    return rx, tx


def ping_node(n: dict[str, Any]) -> tuple[bool, float | None]:
    p = run(["ping", "-I", str(n["awg_interface"]), "-c", "1", "-W", "1", str(n["remote_tunnel_ip"])], timeout=3)
    if p.returncode != 0:
        return False, None
    m = re.search(r"time[=<]([0-9.]+)\s*ms", p.stdout)
    return True, float(m.group(1)) if m else None


def _tcp_probe(local_ip: str, host: str, timeout: float = 2.5) -> tuple[bool, float | None]:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    started = time.monotonic()
    try:
        s.bind((local_ip, 0))
        s.connect((host, TELEGRAM_PROBE_PORT))
        return True, round((time.monotonic() - started) * 1000, 1)
    except OSError:
        return False, None
    finally:
        s.close()


def telegram_node(n: dict[str, Any]) -> tuple[bool, float | None, str | None, str | None, int]:
    """Probe Telegram through a node; any reachable Telegram DC is sufficient."""
    local = str(n["local_tunnel_ip"])
    seed = sum(str(n.get("id", "")).encode()) + int(time.time() // 60)
    start = seed % len(TELEGRAM_PROBES)
    ordered = TELEGRAM_PROBES[start:] + TELEGRAM_PROBES[:start]
    attempts = 0
    for label, host in ordered:
        attempts += 1
        ok, ms = _tcp_probe(local, host)
        if ok:
            return True, ms, label, host, attempts
    return False, None, None, None, attempts


def health_node(n: dict[str, Any], handshake_max_age: int) -> dict[str, Any]:
    iface = str(n["awg_interface"])
    iface_up = (Path("/sys/class/net") / iface).exists()
    age = latest_handshake_age(iface) if iface_up else -1
    rx, tx = awg_transfer(iface) if iface_up else (0, 0)
    hs_ok = iface_up and 0 <= age <= handshake_max_age
    ping_ok, rtt = ping_node(n) if iface_up else (False, None)
    tg_ok, tg_ms, tg_label, tg_ip, tg_attempts = telegram_node(n) if iface_up else (False, None, None, None, 0)
    enabled = bool(n.get("enabled", True))
    return {
        "id": str(n["id"]),
        "name": str(n["name"]),
        "enabled": enabled,
        "priority": int(n["priority"]),
        "public_ip": str(n.get("public_ip", "")),
        "health": enabled and hs_ok and tg_ok,
        "awg": {
            "interface": iface, "up": iface_up, "handshake_age_sec": age,
            "rx_bytes": rx, "tx_bytes": tx,
        },
        "transport": {
            "profile": str(n.get("transport_profile", "legacy")),
            "header_protection": bool(n.get("header_protection", False)),
            "mtu": int(n.get("transport_mtu", 0) or 0),
            "updated_at": str(n.get("transport_updated_at", "")) or None,
        },
        "connectivity": {
            "tunnel": ping_ok, "tunnel_rtt_ms": rtt,
            "telegram": tg_ok, "telegram_tcp_ms": tg_ms,
            "telegram_target": tg_label, "telegram_ip": tg_ip,
            "telegram_probes_tried": tg_attempts,
            "telegram_probe_total": len(TELEGRAM_PROBES),
        },
    }


def agent_node(n: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Query the private node-agent strictly through this node tunnel."""
    token_path = Path(str(n.get("agent_token_file", "")))
    if not token_path.is_file():
        return {"reachable": False, "error": "token_missing"}, None
    conn: http.client.HTTPConnection | None = None
    try:
        token = token_path.read_text(encoding="utf-8").strip()
        host = str(n["remote_tunnel_ip"])
        port = int(n.get("agent_port", 9784))
        local = str(n["local_tunnel_ip"])
        started = time.monotonic()
        conn = http.client.HTTPConnection(host, port, timeout=2.5, source_address=(local, 0))
        conn.request("GET", "/metrics", headers={"Authorization": "Bearer " + token})
        response = conn.getresponse()
        raw = response.read()
        if response.status != 200:
            raise RuntimeError(f"node-agent HTTP {response.status}")
        data = json.loads(raw)
        agent = {
            "reachable": True,
            "request_ms": round((time.monotonic() - started) * 1000, 1),
            "version": data.get("version"),
            "error": None,
        }
        system = {k: data.get(k) for k in ("hostname", "uptime_sec", "cpu", "memory", "disk", "network")}
        return agent, system
    except Exception as exc:
        return {"reachable": False, "error": type(exc).__name__}, None
    finally:
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()

def telemt_nat_ip(config_path: Path) -> str:
    try:
        data = load_toml(config_path)
        return str(data.get("general", {}).get("middle_proxy_nat_ip", ""))
    except Exception:
        return ""


def set_telemt_nat_ip(config_path: Path, desired: str) -> bool:
    current = telemt_nat_ip(config_path)
    if current == desired:
        return False
    lines = config_path.read_text(encoding="utf-8").splitlines()
    section = None
    section_end = len(lines)
    found = None
    for i, line in enumerate(lines):
        v = line.strip()
        if v == "[general]":
            section = i
            continue
        if section is not None and i > section and v.startswith("[") and v.endswith("]"):
            section_end = i
            break
    if section is None:
        lines += ["", "[general]", f'middle_proxy_nat_ip = "{desired}"']
    else:
        for i in range(section + 1, section_end):
            if lines[i].strip().startswith("middle_proxy_nat_ip"):
                found = i; break
        if found is None:
            lines.insert(section + 1, f'middle_proxy_nat_ip = "{desired}"')
        else:
            lines[found] = f'middle_proxy_nat_ip = "{desired}"'
    backup = config_path.with_name(config_path.name + ".before-egressmt")
    if not backup.exists():
        shutil.copy2(config_path, backup)
    atomic_text(config_path, "\n".join(lines) + "\n", 0o600)
    return True


def dc_status() -> dict[str, Any]:
    p = run(["mtproxyl", "dc", "status", "--json"], timeout=8)
    if p.returncode != 0:
        return {}
    try:
        return json.loads(p.stdout)
    except Exception:
        return {}


def wait_dc_ready(threshold: int, timeout_sec: int = 70) -> tuple[bool, dict[str, Any]]:
    deadline = time.monotonic() + timeout_sec
    last: dict[str, Any] = {}
    while time.monotonic() < deadline and not _stop.is_set():
        last = dc_status()
        if (
            last.get("available") is True
            and int(last.get("coverage_pct", 0) or 0) >= threshold
            and int(last.get("alive_writers", 0) or 0) > 0
        ):
            return True, last
        time.sleep(2)
    return False, last


def replace_mark_rule(table: str, mark: str, rule_priority: int) -> None:
    mask = mark
    while run(["ip", "rule", "del", "priority", str(TEMP_RULE_PRIORITY)]).returncode == 0:
        pass
    run([
        "ip", "rule", "add", "priority", str(TEMP_RULE_PRIORITY),
        "fwmark", f"{mark}/{mask}", "lookup", table,
    ], check=True)
    while run(["ip", "rule", "del", "priority", str(rule_priority)]).returncode == 0:
        pass
    run([
        "ip", "rule", "add", "priority", str(rule_priority),
        "fwmark", f"{mark}/{mask}", "lookup", table,
    ], check=True)
    while run(["ip", "rule", "del", "priority", str(TEMP_RULE_PRIORITY)]).returncode == 0:
        pass


def write_active(value: str) -> None:
    atomic_text(ACTIVE_FILE, value + "\n", 0o644)


def render_node(n: dict[str, Any]) -> str:
    q = lambda v: json.dumps(str(v), ensure_ascii=False)
    fields = [
        ("id", q(n["id"])), ("name", q(n["name"])),
        ("enabled", "true" if bool(n.get("enabled", True)) else "false"),
        ("priority", str(int(n["priority"]))),
        ("endpoint", q(n.get("endpoint", ""))),
        ("public_ip", q(n.get("public_ip", ""))),
        ("ssh_host", q(n.get("ssh_host", n.get("public_ip", "")))),
        ("ssh_port", str(int(n.get("ssh_port", 22)))),
        ("ssh_user", q(n.get("ssh_user", "root"))),
        ("awg_interface", q(n["awg_interface"])),
        ("awg_port", str(int(n.get("awg_port", 0)))),
        ("local_tunnel_ip", q(n["local_tunnel_ip"])),
        ("remote_tunnel_ip", q(n["remote_tunnel_ip"])),
        ("routing_table", str(int(n["routing_table"]))),
        ("agent_port", str(int(n.get("agent_port", 9784)))),
        ("agent_token_file", q(n.get("agent_token_file", ""))),
        ("transport_profile", q(n.get("transport_profile", "legacy"))),
        ("transport_mtu", str(int(n.get("transport_mtu", 0) or 0))),
        ("header_protection", "true" if bool(n.get("header_protection", False)) else "false"),
        ("transport_updated_at", q(n.get("transport_updated_at", ""))),
        ("provisioned", "true" if bool(n.get("provisioned", True)) else "false"),
    ]
    return "\n".join(f"{k} = {v}" for k, v in fields) + "\n"


def write_node(n: dict[str, Any]) -> None:
    atomic_text(NODES_DIR / f"{n['id']}.toml", render_node(n), 0o600)


def update_config_manager(values: dict[str, int]) -> dict[str, int]:
    lines = CONFIG.read_text(encoding="utf-8").splitlines()
    section = None
    end = len(lines)
    positions: dict[str, int] = {}
    for i, line in enumerate(lines):
        v = line.strip()
        if v == "[manager]":
            section = i; continue
        if section is not None and i > section and v.startswith("[") and v.endswith("]"):
            end = i; break
    if section is None:
        raise RuntimeError("[manager] section missing")
    for i in range(section + 1, end):
        if "=" in lines[i]:
            positions[lines[i].split("=", 1)[0].strip()] = i
    insert_at = section + 1
    for key, value in values.items():
        if key in positions:
            lines[positions[key]] = f"{key} = {int(value)}"
        else:
            lines.insert(insert_at, f"{key} = {int(value)}"); insert_at += 1
    atomic_text(CONFIG, "\n".join(lines) + "\n", 0o600)
    m = load_toml(CONFIG)["manager"]
    return {k: int(m[k]) for k in ("check_interval", "fail_threshold", "failback_hold", "handshake_max_age")}


class Manager:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.config = load_toml(CONFIG)
        self.nodes = load_nodes()
        self.control = load_control(self.config, self.nodes)
        self.active = self._initial_active()
        self.phase = "starting"
        self.last_error: str | None = None
        self.fail_counts = {str(n["id"]): 0 for n in self.nodes}
        self.healthy_since: dict[str, float | None] = {str(n["id"]): None for n in self.nodes}
        self.rows: dict[str, dict[str, Any]] = {}
        self.agent_cache: dict[str, tuple[dict[str, Any], dict[str, Any] | None]] = {}
        self.quarantined_until: dict[str, float] = {}
        self.last_switch = 0.0
        self.wakeup = threading.Event()

    def _initial_active(self) -> str:
        raw = ACTIVE_FILE.read_text(encoding="utf-8").strip() if ACTIVE_FILE.exists() else ""
        if raw in {"block", "direct"}:
            return raw
        try:
            return str(find_node(self.nodes, raw)["id"])
        except Exception:
            pass
        enabled = [n for n in self.nodes if n.get("enabled", True)]
        return str(enabled[0]["id"]) if enabled else "block"

    def active_route_table(self) -> str:
        if self.active == "block":
            return str(int(self.config["routing"].get("block_table", 51839)))
        if self.active == "direct":
            return "main"
        try:
            return str(int(find_node(self.nodes, self.active)["routing_table"]))
        except Exception:
            return str(int(self.config["routing"].get("block_table", 51839)))

    def reconcile_active_route(self) -> None:
        mark = str(self.config["routing"].get("mark", "0x200000"))
        priority = int(self.config["routing"].get("rule_priority", 11000))
        table = self.active_route_table()
        mark_int = int(mark, 0)
        mark_token = f"fwmark 0x{mark_int:x}/0x{mark_int:x}"
        lookup_token = f"lookup {table}"
        lines = run(["ip", "rule", "show"]).stdout.splitlines()
        matches = [line.strip() for line in lines if line.strip().startswith(f"{priority}:")]
        if len(matches) == 1 and mark_token in matches[0] and lookup_token in matches[0]:
            return
        replace_mark_rule(table, mark, priority)
        event(f"route_reconcile active={self.active} table={table}")

    def reload(self) -> None:
        with self.lock:
            self.config = load_toml(CONFIG)
            self.nodes = load_nodes()
            valid = {str(n["id"]) for n in self.nodes}
            for node_id in valid:
                self.fail_counts.setdefault(node_id, 0)
                self.healthy_since.setdefault(node_id, None)
            self.fail_counts = {k: v for k, v in self.fail_counts.items() if k in valid}
            self.healthy_since = {k: v for k, v in self.healthy_since.items() if k in valid}
            ensure_routes(self.nodes, self.config)
            self.reconcile_active_route()
            ensure_nft(self.nodes)

    def collect_health(self) -> dict[str, dict[str, Any]]:
        max_age = int(self.config["manager"].get("handshake_max_age", 180))
        nodes = list(self.nodes)
        if not nodes:
            return {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(nodes))) as ex:
            futures = {ex.submit(health_node, n, max_age): n for n in nodes}
            rows: dict[str, dict[str, Any]] = {}
            for fut, n in futures.items():
                try:
                    row = fut.result()
                except Exception as exc:
                    row = {
                        "id": str(n["id"]), "name": str(n["name"]),
                        "enabled": bool(n.get("enabled", True)), "priority": int(n["priority"]),
                        "public_ip": str(n.get("public_ip", "")), "health": False,
                        "awg": {"interface": str(n["awg_interface"]), "up": False, "handshake_age_sec": -1},
                        "transport": {
                            "profile": str(n.get("transport_profile", "legacy")),
                            "header_protection": bool(n.get("header_protection", False)),
                            "mtu": int(n.get("transport_mtu", 0) or 0),
                            "updated_at": str(n.get("transport_updated_at", "")) or None,
                        },
                        "connectivity": {
                            "tunnel": False, "tunnel_rtt_ms": None, "telegram": False,
                            "telegram_target": None, "telegram_ip": None,
                            "telegram_probes_tried": 0, "telegram_probe_total": len(TELEGRAM_PROBES),
                        },
                        "error": type(exc).__name__,
                    }
                node_id = str(n["id"])
                until = self.quarantined_until.get(node_id, 0.0)
                if until > time.monotonic():
                    row["health"] = False
                    row["runtime_quarantine_sec"] = round(until - time.monotonic(), 1)
                    row["runtime_error"] = "telemt_not_ready"
                elif node_id in self.quarantined_until:
                    self.quarantined_until.pop(node_id, None)
                rows[node_id] = row
        return rows

    def collect_agents(self) -> None:
        candidates = [n for n in self.nodes if self.rows.get(str(n["id"]), {}).get("awg", {}).get("up")]
        if not candidates:
            return
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(candidates))) as ex:
            futures = {ex.submit(agent_node, n): n for n in candidates}
            for fut, n in futures.items():
                try:
                    self.agent_cache[str(n["id"])] = fut.result()
                except Exception as exc:
                    self.agent_cache[str(n["id"])] = ({"reachable": False, "error": type(exc).__name__}, None)

    def role_for(self, node_id: str) -> str:
        ids = [str(n["id"]) for n in self.nodes if n.get("enabled", True)]
        if node_id not in ids:
            return "disabled"
        return "primary" if ids.index(node_id) == 0 else "backup"

    def _desired_auto(self) -> str:
        enabled = [n for n in self.nodes if n.get("enabled", True)]
        healthy = [n for n in enabled if self.rows.get(str(n["id"]), {}).get("health")]
        threshold = int(self.config["manager"].get("fail_threshold", 3))
        hold = int(self.config["manager"].get("failback_hold", 30))
        enabled_ids = {str(n["id"]) for n in enabled}

        if self.active in enabled_ids:
            if not self.rows.get(self.active, {}).get("health"):
                if self.fail_counts.get(self.active, 0) < threshold:
                    return self.active
                return str(healthy[0]["id"]) if healthy else "block"
        else:
            return str(healthy[0]["id"]) if healthy else "block"

        if not healthy:
            return self.active
        active_prio = next(int(n["priority"]) for n in enabled if str(n["id"]) == self.active)
        higher = [n for n in healthy if int(n["priority"]) < active_prio]
        if not higher:
            return self.active
        candidate = higher[0]
        since = self.healthy_since.get(str(candidate["id"]))
        if since is not None and time.monotonic() - since >= hold:
            return str(candidate["id"])
        return self.active

    def desired(self) -> str:
        mode = self.control.get("mode")
        if mode == "block": return "block"
        if mode == "direct": return "direct"
        if mode == "manual":
            target = str(self.control.get("manual_node") or "")
            row = self.rows.get(target)
            return target if row and row.get("enabled") and row.get("health") else "block"
        return self._desired_auto()

    def telemt_container_running(self) -> bool:
        container = str(self.config.get("telemt", {}).get("container", "mtproxyl"))
        p = run(["docker", "inspect", "-f", "{{.State.Running}}", container], timeout=5)
        return p.returncode == 0 and p.stdout.strip().casefold() == "true"

    def sync_telemt(self, target: str) -> tuple[bool, dict[str, Any]]:
        telemt = self.config.get("telemt", {})
        cfg_path = Path(str(telemt.get("config", "")))
        container = str(telemt.get("container", "mtproxyl"))
        if not cfg_path.is_file():
            raise RuntimeError(f"Telemt config missing: {cfg_path}")
        if target == "block":
            return True, {}
        desired_ip = direct_public_ipv4() if target == "direct" else str(find_node(self.nodes, target).get("public_ip", ""))
        ipaddress.IPv4Address(desired_ip)
        changed = set_telemt_nat_ip(cfg_path, desired_ip)
        if changed:
            event(f"telemt_nat target={target} ip={desired_ip}")
            p = run(["docker", "restart", container], timeout=30)
            if p.returncode != 0:
                raise RuntimeError(f"Telemt restart failed: {p.stderr.strip()}")
        threshold = int(self.config["manager"].get("dc_ready_threshold", 80))
        ok, dc = wait_dc_ready(threshold)
        if changed and ok:
            event(f"telemt_ready target={target} coverage={dc.get('coverage_pct')} writers={dc.get('alive_writers')}/{dc.get('required_writers')}")
        return ok, dc

    def reconcile_telemt_nat(self) -> None:
        target = self.active
        if target == "block":
            return
        if not self.telemt_container_running():
            self.phase = "waiting_telemt"
            self.last_error = None
            return
        telemt = self.config.get("telemt", {})
        cfg_path = Path(str(telemt.get("config", "")))
        if not cfg_path.is_file():
            raise RuntimeError(f"Telemt config missing: {cfg_path}")
        desired_ip = direct_public_ipv4() if target == "direct" else str(find_node(self.nodes, target).get("public_ip", ""))
        ipaddress.IPv4Address(desired_ip)
        current_ip = telemt_nat_ip(cfg_path)
        if current_ip == desired_ip:
            return
        self.phase = "reconciling"
        self.last_error = None
        self.persist_status()
        event(f"telemt_nat_reconcile target={target} current={current_ip or '-'} desired={desired_ip}")
        ok, dc = self.sync_telemt(target)
        if not ok:
            self.last_error = f"Telemt DC coverage not ready after NAT reconcile for {target}: {dc.get('coverage_pct', 0)}%"
            self.phase = "degraded"
            event(f"warning {self.last_error}")
            return
        self.phase = "running"
        self.last_error = None
        event(f"telemt_nat_reconcile_ready target={target} coverage={dc.get('coverage_pct')} writers={dc.get('alive_writers')}/{dc.get('required_writers')}")

    def switch(self, target: str, reason: str) -> bool:
        if target == self.active:
            return True
        mark = str(self.config["routing"].get("mark", "0x200000"))
        priority = int(self.config["routing"].get("rule_priority", 11000))
        if target == "block":
            table = str(int(self.config["routing"].get("block_table", 51839)))
        elif target == "direct":
            table = "main"
        else:
            table = str(int(find_node(self.nodes, target)["routing_table"]))
        previous = self.active
        self.phase = "switching"; self.last_error = None; self.persist_status()
        try:
            replace_mark_rule(table, mark, priority)
            write_active(target)
            self.active = target
            event(f"switch {previous} -> {target} reason={reason}")
            ok, dc = self.sync_telemt(target)
            if not ok:
                self.last_error = f"Telemt DC coverage not ready after switch to {target}: {dc.get('coverage_pct', 0)}%"
                if target not in {"block", "direct"}:
                    self.quarantined_until[target] = time.monotonic() + 60
                    self.fail_counts[target] = int(self.config["manager"].get("fail_threshold", 3))
                event(f"warning {self.last_error}")
                self.phase = "degraded"; self.last_switch = time.monotonic()
                return False
            self.phase = "running"; self.last_switch = time.monotonic()
            return True
        except Exception as exc:
            self.last_error = str(exc); self.phase = "error"
            event(f"switch_error target={target} error={type(exc).__name__}:{exc}")
            try:
                block_table = str(int(self.config["routing"].get("block_table", 51839)))
                replace_mark_rule(block_table, mark, priority)
                write_active("block"); self.active = "block"
                event("fail_closed after switch error")
            except Exception as block_exc:
                event(f"CRITICAL fail_closed_error {block_exc}")
            return False

    def update_health_counters(self) -> None:
        now = time.monotonic()
        for n in self.nodes:
            node_id = str(n["id"])
            if self.rows.get(node_id, {}).get("health"):
                self.fail_counts[node_id] = 0
                if self.healthy_since.get(node_id) is None:
                    self.healthy_since[node_id] = now
            else:
                self.fail_counts[node_id] = self.fail_counts.get(node_id, 0) + 1
                self.healthy_since[node_id] = None

    def status(self) -> dict[str, Any]:
        with self.lock:
            rows = []
            for n in self.nodes:
                node_id = str(n["id"])
                row = dict(self.rows.get(node_id) or health_node(n, int(self.config["manager"].get("handshake_max_age", 180))))
                row["role"] = self.role_for(node_id)
                row["fail_count"] = self.fail_counts.get(node_id, 0)
                agent, system = self.agent_cache.get(node_id, ({"reachable": False, "error": "not_sampled"}, None))
                row["agent"] = agent; row["system"] = system; rows.append(row)
            dc = dc_status()
            return {
                "version": VERSION, "timestamp": now_iso(), "phase": self.phase,
                "mode": self.control.get("mode", "auto"), "manual_node": self.control.get("manual_node"),
                "active_node": self.active, "last_error": self.last_error,
                "telemt": {
                    "nat_ip": telemt_nat_ip(Path(str(self.config.get("telemt", {}).get("config", "")))),
                    "dc_available": dc.get("available"), "dc_verdict": dc.get("verdict"),
                    "dc_coverage_pct": dc.get("coverage_pct"), "alive_writers": dc.get("alive_writers"),
                    "required_writers": dc.get("required_writers"),
                },
                "nodes": rows,
            }

    def persist_status(self) -> None:
        atomic_text(STATE_FILE, json.dumps(self.status(), ensure_ascii=False, indent=2) + "\n", 0o644)

    def set_mode(self, mode: str, node_ref: str | None = None) -> dict[str, Any]:
        with self.lock:
            self.reload()
            if mode in {"auto", "direct", "block"}:
                self.control = {"mode": mode, "manual_node": None}
            elif mode == "manual":
                if not node_ref:
                    raise ValueError("manual node is required")
                n = find_node(self.nodes, node_ref)
                if not bool(n.get("enabled", True)):
                    raise ValueError("node is disabled")
                self.control = {"mode": "manual", "manual_node": str(n["id"])}
            else:
                raise ValueError("invalid mode")
            save_control(self.control)
            event(f"mode={self.control['mode']}" + (f" node={self.control.get('manual_node')}" if self.control.get("manual_node") else ""))
            self.wakeup.set()
            return self.control

    def test_node(self, ref: str) -> dict[str, Any]:
        with self.lock:
            self.reload()
            n = find_node(self.nodes, ref)
            h = health_node(n, int(self.config["manager"].get("handshake_max_age", 180)))
            ag, system = agent_node(n) if h.get("awg", {}).get("up") else ({"reachable": False, "error": "awg_down"}, None)
            h["agent"] = ag; h["system"] = system
            return h

    def mutate_node(self, action: str, ref: str, value: Any = None) -> dict[str, Any]:
        with self.lock:
            self.reload()
            n = find_node(self.nodes, ref); node_id = str(n["id"])
            if action == "rename":
                name = str(value).strip()
                if not name or len(name) > 64 or any(ord(c) < 32 or ord(c) == 127 for c in name):
                    raise ValueError("invalid node name")
                for other in self.nodes:
                    if str(other["id"]) != node_id and str(other.get("name", "")).casefold() == name.casefold():
                        raise ValueError("node name already exists")
                old = str(n["name"]); n["name"] = name; write_node(n)
                event(f"node_rename id={node_id} {old!r} -> {name!r}")
            elif action in {"enable", "disable"}:
                desired = action == "enable"
                if not desired and self.control.get("mode") == "manual" and self.control.get("manual_node") == node_id:
                    raise ValueError("cannot disable node selected in manual mode")
                n["enabled"] = desired; write_node(n); event(f"node_{action} id={node_id} name={n['name']}")
            elif action == "priority":
                prio = int(value)
                if prio <= 0 or prio > 9999:
                    raise ValueError("priority must be 1..9999")
                for other in self.nodes:
                    if str(other["id"]) != node_id and int(other["priority"]) == prio:
                        raise ValueError("priority already in use")
                old = int(n["priority"]); n["priority"] = prio; write_node(n)
                event(f"node_priority id={node_id} {old} -> {prio}")
            else:
                raise ValueError("invalid node mutation")
            self.reload(); self.wakeup.set()
            return find_node(self.nodes, node_id)

    def set_config(self, values: dict[str, Any]) -> dict[str, int]:
        allowed = {
            "check_interval": (2, 60), "fail_threshold": (1, 10),
            "failback_hold": (5, 600), "handshake_max_age": (30, 600),
        }
        clean: dict[str, int] = {}
        for key, (lo, hi) in allowed.items():
            if key not in values: raise ValueError(f"missing {key}")
            v = int(values[key])
            if not lo <= v <= hi: raise ValueError(f"{key} out of range")
            clean[key] = v
        result = update_config_manager(clean)
        with self.lock:
            self.reload(); event("manager_config_updated"); self.wakeup.set()
        return result

    def get_config(self) -> dict[str, int]:
        m = load_toml(CONFIG)["manager"]
        return {k: int(m[k]) for k in ("check_interval", "fail_threshold", "failback_hold", "handshake_max_age")}

    def loop(self) -> None:
        self.reload()
        self.phase = "running"
        event(f"egressd_start version={VERSION} active={self.active}")
        while not _stop.is_set():
            cycle_start = time.monotonic()
            try:
                self.reload()
                self.rows = self.collect_health()
                self.update_health_counters()
                desired = self.desired()
                reason = str(self.control.get("mode", "auto"))
                if desired != self.active:
                    self.switch(desired, reason=reason)
                elif self.active != "block":
                    active_ready = self.active == "direct" or bool(self.rows.get(self.active, {}).get("health"))
                    if active_ready:
                        self.reconcile_telemt_nat()
                    else:
                        self.phase = "waiting_route"; self.last_error = None
                self.collect_agents()
                if self.phase != "switching":
                    dc = dc_status(); threshold = int(self.config["manager"].get("dc_ready_threshold", 80))
                    if self.active == "block":
                        self.phase = "running"
                    elif dc.get("available") is True and int(dc.get("coverage_pct", 0) or 0) >= threshold and int(dc.get("alive_writers", 0) or 0) > 0:
                        self.phase = "running"; self.last_error = None
                self.persist_status()
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"; self.phase = "error"
                event(f"manager_loop_error {self.last_error}"); log(self.last_error)
                with contextlib.suppress(Exception): self.persist_status()
            interval = int(self.config.get("manager", {}).get("check_interval", 5))
            timeout = max(0.2, interval - (time.monotonic() - cycle_start))
            self.wakeup.wait(timeout); self.wakeup.clear()
        event("egressd_stop")


class ControlHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        manager: Manager = self.server.manager  # type: ignore[attr-defined]
        try:
            req = json.loads(self.rfile.readline(1024 * 1024))
            action = req.get("action")
            if action == "status": data = manager.status()
            elif action == "set_mode": data = manager.set_mode(str(req.get("mode")), req.get("node"))
            elif action == "node_test": data = manager.test_node(str(req.get("node", "")))
            elif action in {"node_rename", "node_enable", "node_disable", "node_priority"}:
                mapping = {"node_rename":"rename", "node_enable":"enable", "node_disable":"disable", "node_priority":"priority"}
                data = manager.mutate_node(mapping[action], str(req.get("node", "")), req.get("value"))
            elif action == "config_get": data = manager.get_config()
            elif action == "config_set": data = manager.set_config(dict(req.get("config") or {}))
            elif action == "reload":
                manager.reload(); manager.wakeup.set(); data = {"reloaded": True}
            else: raise ValueError("unknown action")
            reply = {"ok": True, "data": data}
        except Exception as exc:
            reply = {"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)}}
        self.wfile.write((json.dumps(reply, ensure_ascii=False) + "\n").encode())


class ControlServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True
    def __init__(self, path: str, handler: type[ControlHandler], manager: Manager):
        self.manager = manager
        super().__init__(path, handler)


def probe_only() -> int:
    cfg = load_toml(CONFIG); nodes = load_nodes()
    ensure_routes(nodes, cfg)
    max_age = int(cfg["manager"].get("handshake_max_age", 180))
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, max(1, len(nodes)))) as ex:
        rows = list(ex.map(lambda n: health_node(n, max_age), nodes)) if nodes else []
    print("EgressMT — probe")
    print("================")
    for r in rows:
        target = r.get("connectivity", {}).get("telegram_target") or "—"
        print(f"{r['priority']:>3} {r['name']} [{r['id']}] {'HEALTHY' if r['health'] else 'DOWN'} hs={r['awg']['handshake_age_sec']}s rtt={r['connectivity']['tunnel_rtt_ms']}ms TG={'OK' if r['connectivity']['telegram'] else 'FAIL'} target={target}")
    enabled = [r for r in rows if r.get("enabled")]
    return 0 if not enabled or all(r["health"] for r in enabled) else 2


def acquire_lock() -> Any:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    f = LOCK_PATH.open("w")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise RuntimeError("another egressd is already running") from exc
    return f


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe-only", action="store_true")
    ap.add_argument("--version", action="store_true")
    args = ap.parse_args()
    if args.version:
        print(f"egressmt-daemon {VERSION}"); return
    if args.probe_only:
        raise SystemExit(probe_only())
    lock = acquire_lock()
    signal.signal(signal.SIGTERM, lambda *_: _stop.set())
    signal.signal(signal.SIGINT, lambda *_: _stop.set())
    SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(FileNotFoundError): SOCKET_PATH.unlink()
    manager = Manager()
    server = ControlServer(str(SOCKET_PATH), ControlHandler, manager)
    os.chmod(SOCKET_PATH, 0o600)
    t_server = threading.Thread(target=server.serve_forever, name="control", daemon=True)
    t_manager = threading.Thread(target=manager.loop, name="manager", daemon=True)
    t_server.start(); t_manager.start()
    log(f"EgressMT daemon {VERSION} started")
    try:
        while not _stop.wait(0.5): pass
    finally:
        server.shutdown(); server.server_close(); manager.wakeup.set(); t_manager.join(timeout=15)
        with contextlib.suppress(FileNotFoundError): SOCKET_PATH.unlink()
        lock.close(); log("EgressMT daemon stopped")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import tomllib
from typing import Any

ETC = Path(os.environ.get("EGRESSMT_ETC", "/etc/mtproxyl-egress"))
NODES_DIR = ETC / "nodes.d"
CONFIG = ETC / "config.toml"
NAME_MAX = 64
ID_RE = re.compile(r"^n-[0-9a-f]{8}$")


def fail(message: str) -> "NoReturn":
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
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
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def q(value: Any) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as f:
        return tomllib.load(f)


def all_nodes() -> list[dict[str, Any]]:
    if not NODES_DIR.exists():
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(NODES_DIR.glob("*.toml")):
        try:
            n = load_toml(p)
        except Exception as exc:
            fail(f"cannot read {p}: {exc}")
        n["_path"] = str(p)
        out.append(n)
    return sorted(out, key=lambda n: (int(n.get("priority", 999999)), str(n.get("id", ""))))


def find_node(ref: str) -> dict[str, Any]:
    folded = ref.casefold()
    hits = [n for n in all_nodes() if folded in {
        str(n.get("id", "")).casefold(),
        str(n.get("name", "")).casefold(),
    }]
    if len(hits) != 1:
        fail("node not found or ambiguous")
    return hits[0]


def valid_name(value: str) -> str:
    value = value.strip()
    if not value or len(value) > NAME_MAX:
        fail(f"node name must contain 1..{NAME_MAX} characters")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        fail("node name contains control characters")
    return value


def render_node(n: dict[str, Any]) -> str:
    fields = [
        ("id", q(n["id"])),
        ("name", q(n["name"])),
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
        ("provisioned", "true" if bool(n.get("provisioned", True)) else "false"),
    ]
    return "\n".join(f"{k} = {v}" for k, v in fields) + "\n"


def render_config(telemt_config: str, container: str = "mtproxyl") -> str:
    return f'''version = 1
mode = "auto"

[manager]
check_interval = 5
fail_threshold = 3
failback_hold = 30
handshake_max_age = 180
dc_ready_threshold = 80
recommended_max_nodes = 5

[routing]
mark = "0x200000"
rule_priority = 11000
block_table = 51839

[telemt]
container = {q(container)}
config = {q(telemt_config)}
'''


def init_registry(telemt_config: str, container: str, force: bool) -> None:
    p = Path(telemt_config)
    if not p.is_file():
        fail(f"Telemt config does not exist: {p}")
    if CONFIG.exists() and not force:
        print(f"Registry already initialized: {CONFIG}")
        return
    ETC.mkdir(parents=True, exist_ok=True)
    NODES_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write(CONFIG, render_config(str(p), container), 0o600)
    print(f"Registry initialized: {CONFIG}")


def validate_registry() -> None:
    if not CONFIG.is_file():
        fail(f"missing {CONFIG}")
    cfg = load_toml(CONFIG)
    if int(cfg.get("version", 0)) != 1:
        fail("unsupported registry version")
    for section in ("manager", "routing", "telemt"):
        if not isinstance(cfg.get(section), dict):
            fail(f"missing [{section}] section")
    telemt_cfg = Path(str(cfg["telemt"].get("config", "")))
    if not telemt_cfg.is_file():
        fail(f"Telemt config does not exist: {telemt_cfg}")

    nodes = all_nodes()
    ids: set[str] = set()
    names: set[str] = set()
    priorities: set[int] = set()
    ifaces: set[str] = set()
    tunnel_ips: set[str] = set()
    tables: set[int] = set()

    for n in nodes:
        node_id = str(n.get("id", ""))
        if not ID_RE.fullmatch(node_id):
            fail(f"invalid node id: {node_id}")
        if node_id in ids:
            fail(f"duplicate node id: {node_id}")
        ids.add(node_id)

        name = valid_name(str(n.get("name", "")))
        folded = name.casefold()
        if folded in names:
            fail(f"duplicate node name: {name}")
        names.add(folded)

        prio = int(n.get("priority", 0))
        if not 1 <= prio <= 9999 or prio in priorities:
            fail(f"invalid or duplicate priority: {prio}")
        priorities.add(prio)

        iface = str(n.get("awg_interface", ""))
        if not re.fullmatch(r"awg-[0-9a-f]{8}", iface):
            fail(f"invalid AWG interface: {iface}")
        if iface in ifaces:
            fail(f"duplicate AWG interface: {iface}")
        ifaces.add(iface)

        for key in ("local_tunnel_ip", "remote_tunnel_ip"):
            ip = str(ipaddress.IPv4Address(str(n.get(key, ""))))
            if ip in tunnel_ips:
                fail(f"duplicate tunnel IP: {ip}")
            tunnel_ips.add(ip)

        table = int(n.get("routing_table", 0))
        if table <= 0 or table in tables:
            fail(f"invalid or duplicate routing table: {table}")
        tables.add(table)

        public = str(n.get("public_ip", ""))
        if public:
            ipaddress.IPv4Address(public)

    print(f"Registry OK: {len(nodes)} node(s)")


def list_nodes() -> None:
    print(f"{'PRIO':<6} {'ID':<11} {'NAME':<28} {'EN':<4} {'PUBLIC IP':<16} {'AWG'}")
    for n in all_nodes():
        print(
            f"{int(n.get('priority',0)):<6} "
            f"{str(n.get('id','')):<11} "
            f"{str(n.get('name',''))[:27]:<28} "
            f"{'yes' if n.get('enabled',True) else 'no':<4} "
            f"{str(n.get('public_ip','-')):<16} "
            f"{str(n.get('awg_interface','-'))}"
        )


def rename_node(ref: str, new_name: str) -> None:
    n = find_node(ref)
    new_name = valid_name(new_name)
    for other in all_nodes():
        if str(other.get("id")) != str(n.get("id")) and str(other.get("name", "")).casefold() == new_name.casefold():
            fail("node name already exists")
    n["name"] = new_name
    p = Path(str(n["_path"]))
    atomic_write(p, render_node(n), 0o600)
    print(f"Renamed {n['id']} -> {new_name}")


def main() -> None:
    ap = argparse.ArgumentParser(prog="egressmt-registry")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init")
    p.add_argument("--telemt-config", required=True)
    p.add_argument("--container", default="mtproxyl")
    p.add_argument("--force", action="store_true")
    sub.add_parser("validate")
    sub.add_parser("list")
    p = sub.add_parser("show"); p.add_argument("node")
    p = sub.add_parser("rename"); p.add_argument("node"); p.add_argument("name")
    args = ap.parse_args()

    if args.cmd == "init":
        init_registry(args.telemt_config, args.container, args.force)
    elif args.cmd == "validate":
        validate_registry()
    elif args.cmd == "list":
        list_nodes()
    elif args.cmd == "show":
        n = find_node(args.node)
        n.pop("_path", None)
        print(json.dumps(n, ensure_ascii=False, indent=2))
    elif args.cmd == "rename":
        rename_node(args.node, args.name)


if __name__ == "__main__":
    main()

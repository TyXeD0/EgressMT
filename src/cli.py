#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import socket
import sys
import time

SOCKET = "/run/mtproxyl-egress/control.sock"
EVENTS = Path("/var/lib/mtproxyl-egress/events.log")


def request(payload: dict) -> dict:
    raw = (json.dumps(payload, ensure_ascii=False) + "\n").encode()
    deadline = time.monotonic() + 20
    last_exc: Exception | None = None
    while True:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(20)
        try:
            s.connect(SOCKET)
            s.sendall(raw)
            chunks = []
            while True:
                part = s.recv(65536)
                if not part:
                    break
                chunks.append(part)
                if b"\n" in part:
                    break
            if not chunks:
                raise RuntimeError("EgressMT daemon returned empty response")
            reply = json.loads(b"".join(chunks).split(b"\n", 1)[0])
            if not reply.get("ok"):
                err = reply.get("error") or {}
                raise RuntimeError(err.get("message") or "EgressMT request failed")
            return reply["data"]
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            last_exc = exc
            if time.monotonic() >= deadline:
                raise RuntimeError(f"EgressMT daemon control socket is not ready: {exc}") from exc
            time.sleep(0.25)
        finally:
            s.close()


def human_bytes(value):
    if value is None:
        return "—"
    v = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(v) < 1024:
            return f"{v:.1f} {unit}"
        v /= 1024
    return f"{v:.1f} PB"


def human_status(d: dict) -> None:
    print("EgressMT")
    print("========")
    print(f"Version:     {d.get('version')}")
    print(f"Phase:       {d.get('phase')}")
    print(f"Mode:        {d.get('mode')}")
    if d.get("manual_node"):
        print(f"Manual node: {d.get('manual_node')}")
    print(f"Active:      {d.get('active_node')}")
    if d.get("last_error"):
        print(f"Last error:  {d.get('last_error')}")
    t = d.get("telemt") or {}
    print(
        "Telemt:      "
        f"NAT={t.get('nat_ip') or '—'} "
        f"writers={t.get('alive_writers')}/{t.get('required_writers')} "
        f"coverage={t.get('dc_coverage_pct')}% "
        f"verdict={t.get('dc_verdict')}"
    )
    print()

    nodes = sorted(d.get("nodes", []), key=lambda n: (int(n.get("priority", 999999)), n.get("id", "")))
    for n in nodes:
        flags = ["HEALTHY" if n.get("health") else "DOWN", str(n.get("role", "disabled")).upper()]
        if n.get("id") == d.get("active_node"):
            flags.append("ACTIVE")
        if not n.get("enabled"):
            flags.append("DISABLED")
        print(f"{int(n.get('priority', 0)):>3} {n.get('name')} [{n.get('id')}] " + " ".join(flags))
        awg = n.get("awg") or {}
        c = n.get("connectivity") or {}
        agent = n.get("agent") or {}
        transport = n.get("transport") or {}
        target = c.get("telegram_target") or "—"
        hpk = "HPK" if transport.get("header_protection") else "no-HPK"
        rt = "RT" if transport.get("random_trailers") else "no-RT"
        cookies = "no-cookie" if transport.get("disable_cookies") else "cookies"
        print(
            f"    {n.get('public_ip') or '—'} {awg.get('interface') or '—'} "
            f"transport={transport.get('profile') or 'legacy'}/{hpk}/{rt}/{cookies} "
            f"hs={awg.get('handshake_age_sec')}s rtt={c.get('tunnel_rtt_ms')}ms "
            f"TG={'OK' if c.get('telegram') else 'FAIL'}({target}) "
            f"Agent={'OK' if agent.get('reachable') else 'OFF'} "
            f"fails={n.get('fail_count', 0)}"
        )
        system = n.get("system") or {}
        if system:
            cpu = system.get("cpu") or {}
            mem = system.get("memory") or {}
            disk = system.get("disk") or {}
            net = system.get("network") or {}
            print(
                f"    host={system.get('hostname') or '—'} "
                f"cpu={cpu.get('usage_percent', '—')}% ram={mem.get('usage_percent', '—')}% "
                f"disk={disk.get('usage_percent', '—')}% net={net.get('interface') or '—'} "
                f"rx={human_bytes(net.get('rx_bytes'))} tx={human_bytes(net.get('tx_bytes'))}"
            )


def node_list(d: dict) -> None:
    print(f"{'PRIO':<6} {'ID':<11} {'NAME':<24} {'EN':<4} {'HEALTH':<8} {'ACTIVE':<7} {'PUBLIC IP':<16}")
    for n in sorted(d.get("nodes", []), key=lambda x: (int(x.get("priority", 999999)), x.get("id", ""))):
        print(
            f"{int(n.get('priority', 0)):<6} "
            f"{str(n.get('id', '-')):<11} "
            f"{str(n.get('name', '-'))[:23]:<24} "
            f"{'yes' if n.get('enabled') else 'no':<4} "
            f"{'healthy' if n.get('health') else 'down':<8} "
            f"{'yes' if n.get('id') == d.get('active_node') else 'no':<7} "
            f"{str(n.get('public_ip', '-')):<16}"
        )


def main() -> None:
    ap = argparse.ArgumentParser(prog="egressmt")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("status")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("mode")
    p.add_argument("value")
    p.add_argument("node", nargs="?")

    p = sub.add_parser("switch")
    p.add_argument("node")
    sub.add_parser("auto")
    sub.add_parser("direct")
    sub.add_parser("block")

    p = sub.add_parser("events")
    p.add_argument("limit", nargs="?", type=int, default=30)

    p = sub.add_parser("config")
    csub = p.add_subparsers(dest="config_cmd", required=True)
    csub.add_parser("get")
    ps = csub.add_parser("set")
    ps.add_argument("check_interval", type=int)
    ps.add_argument("fail_threshold", type=int)
    ps.add_argument("failback_hold", type=int)
    ps.add_argument("handshake_max_age", type=int)

    p = sub.add_parser("node")
    nsub = p.add_subparsers(dest="node_cmd", required=True)
    nsub.add_parser("list")
    ps = nsub.add_parser("show"); ps.add_argument("node")
    ps = nsub.add_parser("test"); ps.add_argument("node")
    ps = nsub.add_parser("rename"); ps.add_argument("node"); ps.add_argument("name")
    ps = nsub.add_parser("enable"); ps.add_argument("node")
    ps = nsub.add_parser("disable"); ps.add_argument("node")
    ps = nsub.add_parser("priority"); ps.add_argument("node"); ps.add_argument("priority", type=int)

    args = ap.parse_args()
    try:
        if args.cmd == "status":
            d = request({"action": "status"})
            print(json.dumps(d, ensure_ascii=False, indent=2) if args.json else "") if args.json else human_status(d)
        elif args.cmd == "mode":
            value = args.value.casefold()
            if value in {"auto", "direct", "block"}:
                d = request({"action": "set_mode", "mode": value})
            else:
                d = request({"action": "set_mode", "mode": "manual", "node": args.node or args.value})
            print(json.dumps(d, ensure_ascii=False, indent=2))
        elif args.cmd == "switch":
            print(json.dumps(request({"action": "set_mode", "mode": "manual", "node": args.node}), ensure_ascii=False, indent=2))
        elif args.cmd in {"auto", "direct", "block"}:
            print(json.dumps(request({"action": "set_mode", "mode": args.cmd}), ensure_ascii=False, indent=2))
        elif args.cmd == "events":
            limit = max(1, min(200, int(args.limit)))
            if EVENTS.exists():
                print("\n".join(EVENTS.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]))
        elif args.cmd == "config":
            d = request({"action": "config_get"}) if args.config_cmd == "get" else request({
                "action": "config_set",
                "config": {
                    "check_interval": args.check_interval,
                    "fail_threshold": args.fail_threshold,
                    "failback_hold": args.failback_hold,
                    "handshake_max_age": args.handshake_max_age,
                },
            })
            print(json.dumps(d, ensure_ascii=False, indent=2))
        elif args.cmd == "node":
            if args.node_cmd == "list":
                node_list(request({"action": "status"}))
            elif args.node_cmd == "show":
                d = request({"action": "status"})
                folded = args.node.casefold()
                hits = [n for n in d.get("nodes", []) if folded in {
                    str(n.get("id", "")).casefold(), str(n.get("name", "")).casefold()
                }]
                if len(hits) != 1:
                    raise RuntimeError("node not found or ambiguous")
                print(json.dumps(hits[0], ensure_ascii=False, indent=2))
            else:
                action_map = {
                    "test": ("node_test", None),
                    "rename": ("node_rename", args.name if hasattr(args, "name") else None),
                    "enable": ("node_enable", None),
                    "disable": ("node_disable", None),
                    "priority": ("node_priority", args.priority if hasattr(args, "priority") else None),
                }
                action, value = action_map[args.node_cmd]
                payload = {"action": action, "node": args.node}
                if value is not None:
                    payload["value"] = value
                print(json.dumps(request(payload), ensure_ascii=False, indent=2))
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()

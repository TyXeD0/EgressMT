#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time

VERSION = "0.1.0-rc1"
CONFIG = Path("/etc/mtproxyl-node-agent/config.env")
TOKEN = Path("/etc/mtproxyl-node-agent/token")


def parse_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def meminfo() -> dict[str, int]:
    vals: dict[str, int] = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, rest = line.split(":", 1)
        try:
            vals[key] = int(rest.strip().split()[0]) * 1024
        except Exception:
            pass
    total = vals.get("MemTotal", 0)
    available = vals.get("MemAvailable", vals.get("MemFree", 0))
    used = max(0, total - available)
    return {
        "total_bytes": total,
        "used_bytes": used,
        "available_bytes": available,
        "usage_percent": round((used / total * 100) if total else 0.0, 1),
    }


def diskinfo() -> dict[str, int | str | float]:
    st = os.statvfs("/")
    total = st.f_frsize * st.f_blocks
    available = st.f_frsize * st.f_bavail
    used = max(0, total - available)
    return {
        "mount": "/",
        "total_bytes": total,
        "used_bytes": used,
        "available_bytes": available,
        "usage_percent": round((used / total * 100) if total else 0.0, 1),
    }


def cpu_snapshot() -> tuple[int, int]:
    parts = Path("/proc/stat").read_text().splitlines()[0].split()[1:]
    vals = [int(x) for x in parts]
    total = sum(vals)
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    return total, idle


def cpuinfo() -> dict[str, int | float]:
    t1, i1 = cpu_snapshot()
    time.sleep(0.08)
    t2, i2 = cpu_snapshot()
    dt = max(1, t2 - t1)
    usage = max(0.0, min(100.0, (1.0 - ((i2 - i1) / dt)) * 100.0))
    load1, load5, load15 = os.getloadavg()
    return {
        "usage_percent": round(usage, 1),
        "logical_cpus": os.cpu_count() or 1,
        "load1": round(load1, 2),
        "load5": round(load5, 2),
        "load15": round(load15, 2),
    }


def default_iface() -> str:
    try:
        for line in Path("/proc/net/route").read_text().splitlines()[1:]:
            p = line.split()
            if len(p) >= 2 and p[1] == "00000000":
                return p[0]
    except Exception:
        pass
    return ""


def iface_totals(iface: str) -> tuple[int, int]:
    if not iface:
        return 0, 0
    base = Path("/sys/class/net") / iface / "statistics"
    try:
        return int((base / "rx_bytes").read_text()), int((base / "tx_bytes").read_text())
    except Exception:
        return 0, 0


class NetSampler:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.last: dict[str, tuple[float, int, int]] = {}

    def sample(self, iface: str) -> dict[str, int | float | str | None]:
        now = time.monotonic()
        rx, tx = iface_totals(iface)
        with self.lock:
            prev = self.last.get(iface)
            self.last[iface] = (now, rx, tx)
        rxps = txps = 0.0
        if prev:
            dt = max(0.001, now - prev[0])
            rxps = max(0.0, (rx - prev[1]) / dt)
            txps = max(0.0, (tx - prev[2]) / dt)
        return {
            "interface": iface or None,
            "rx_bytes": rx,
            "tx_bytes": tx,
            "rx_bytes_per_sec": round(rxps, 1),
            "tx_bytes_per_sec": round(txps, 1),
            "rx_bits_per_sec": round(rxps * 8.0, 1),
            "tx_bits_per_sec": round(txps * 8.0, 1),
        }


NET = NetSampler()


def uptime_seconds() -> int:
    try:
        return int(float(Path("/proc/uptime").read_text().split()[0]))
    except Exception:
        return 0


def metrics(node_name: str) -> dict:
    iface = default_iface()
    return {
        "version": VERSION,
        "node": node_name,
        "hostname": socket.gethostname(),
        "timestamp": int(time.time()),
        "uptime_sec": uptime_seconds(),
        "cpu": cpuinfo(),
        "memory": meminfo(),
        "disk": diskinfo(),
        "network": NET.sample(iface),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "egressmt-node-agent/1"

    def log_message(self, fmt: str, *args) -> None:
        return

    def _authorized(self) -> bool:
        cfg = self.server.cfg  # type: ignore[attr-defined]
        if self.client_address[0] != cfg["ALLOWED_SOURCE"]:
            return False
        return self.headers.get("Authorization", "") == "Bearer " + self.server.token  # type: ignore[attr-defined]

    def _json(self, code: int, data: dict) -> None:
        raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if not self._authorized():
            self._json(403, {"ok": False, "error": "forbidden"})
            return
        if self.path == "/health":
            self._json(200, {"ok": True, "version": VERSION})
        elif self.path == "/metrics":
            self._json(200, metrics(self.server.cfg.get("NODE_NAME", "node")))  # type: ignore[attr-defined]
        else:
            self._json(404, {"ok": False, "error": "not_found"})


def main() -> None:
    cfg = parse_env(CONFIG)
    bind = cfg["BIND_IP"]
    port = int(cfg.get("PORT", "9784"))
    token = TOKEN.read_text(encoding="utf-8").strip()
    server = ThreadingHTTPServer((bind, port), Handler)
    server.cfg = cfg  # type: ignore[attr-defined]
    server.token = token  # type: ignore[attr-defined]
    server.serve_forever(poll_interval=0.5)


if __name__ == "__main__":
    main()

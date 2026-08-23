# EgressMT

EgressMT adds managed Telegram egress nodes to an MTProxyL/Telemt server when Telegram infrastructure is unreachable or unreliable from the server's own network or region.

The public proxy endpoint stays on the **entry VPS**. One or more remote **egress nodes** are connected to it over AmneziaWG. Egress nodes must be able to reach Telegram. EgressMT continuously checks them and routes only Telegram traffic through the highest-priority healthy node. If the active node fails, the next healthy node is selected automatically. If no healthy egress exists, Telegram traffic is blocked instead of leaking through the entry VPS.

```text
Telegram client
      |
      v
ENTRY VPS
(Telegram unavailable/restricted)
      |
      +-- AmneziaWG --> EGRESS node #1 --> Telegram
      +-- AmneziaWG --> EGRESS node #2 --> Telegram
      +-- AmneziaWG --> EGRESS node #N --> Telegram
```

EgressMT does **not** assume a country, provider, public IP range, node name or fixed number of nodes.

## Status

**v0.1.0-rc1 — pre-release.** The failover core has been validated on real VPS infrastructure, including entry-host reboot, backup-node reboot, active-node failover/failback and fail-closed operation. A final clean-install validation from this repository is required before the first stable release.

The release candidate currently targets the **Docker Telemt backend** of MTProxyL. MTProxyL also supports a binary Telemt backend; EgressMT detects it and refuses to claim compatibility instead of silently applying Docker-specific lifecycle logic.

## One-command installation

Run on the entry VPS:

```bash
curl -fsSL https://raw.githubusercontent.com/TyXeD0/EgressMT/main/install.sh | sudo bash
```

The very first screen asks for **Русский / English**. After that the installer is fully interactive: it explains each step, uses numbered choices and `y/n` confirmations, and can guide a non-technical user through the complete setup.

The release-candidate flow includes installing/checking MTProxyL, installing EgressMT Core, adding/removing egress nodes over SSH, setting priorities, selecting AUTO/MANUAL/BLOCK modes, installing the optional Panel integration and showing system status.

Supported target for the first release candidate: **Ubuntu 24.04 LTS**.

## Updating MTProxyL, Telemt or Panel

You can update MTProxyL/Telemt with their normal upstream tools. Afterwards run the EgressMT one-command installer again.

EgressMT records the upstream versions seen during the last successful compatibility check. On the next run it detects MTProxyL, Telemt and Panel changes and validates the runtime contract before doing anything destructive.

For the web panel, EgressMT does **not** blindly apply a permanent patch to an old pinned source tree. It fetches the current MTProxyL Panel source, applies the EgressMT patch in a disposable build directory and performs a complete panel build first. The live panel binary is replaced only after both patching and the full build succeed. If a future upstream change breaks an anchor or compilation, the currently working panel is left untouched.

The repository CI repeats the same panel patch/build against the current MTProxyL `main` branch every day, so upstream incompatibilities can be detected even before a user runs an update.

The installer also has a separate **compatibility check** menu item. Telemt itself is not binary-patched by EgressMT; compatibility is based on the MTProxyL control contract, the active Telemt configuration and the DC-status API used by the failover manager.

## How it works

EgressMT uses **priority-based failover**, not traffic balancing. Egress tunnels stay up continuously. Health checks cover AmneziaWG, the private tunnel and a real Telegram TCP connection. Only Telegram IPv4 ranges are policy-routed through the selected egress; unrelated traffic from the entry VPS keeps its normal route.

Telemt's `middle_proxy_nat_ip` is synchronized with the public IPv4 of the active egress node and DC writers are verified after every switch.

A pre-Docker boot guard installs a fail-closed route before Telemt can start. After boot, EgressMT reconstructs the persisted active egress route before normal Telegram forwarding is allowed.

EgressMT is an add-on for [MTProxyL](https://github.com/Liafanx/MTProxyL) / Telemt and does not replace them.

## Security

Tunnel private keys, node tokens, SSH secrets and runtime configuration are generated or supplied on the servers and must never be committed to this repository.

The repository intentionally contains no real deployment IP addresses, hostnames, AmneziaWG keys, tokens, passwords or other infrastructure-specific data.

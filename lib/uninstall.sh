#!/usr/bin/env bash
set -Eeuo pipefail

LANG_CODE="${EGRESSMT_LANG:-en}"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="/root/egressmt-uninstall-backup-$STAMP"
msg(){ [[ "$LANG_CODE" == "ru" ]] && echo "$1" || echo "$2"; }
[[ ${EUID:-$(id -u)} -eq 0 ]] || { echo "root required" >&2; exit 1; }

install -d -m 700 "$BACKUP"
for p in /etc/mtproxyl-egress /var/lib/mtproxyl-egress /etc/systemd/system/mtproxyl-egressd.service /etc/systemd/system/mtproxyl-egress-boot-guard.service /etc/systemd/system/docker.service.d/10-mtproxyl-egress-boot-guard.conf; do
    [[ -e "$p" ]] && cp -a --parents "$p" "$BACKUP/" 2>/dev/null || true
done

TELEMT_CONFIG=""
if [[ -f /etc/mtproxyl-egress/config.toml ]]; then
    TELEMT_CONFIG="$(python3 - <<'PY'
import tomllib
try:
    with open('/etc/mtproxyl-egress/config.toml','rb') as f: d=tomllib.load(f)
    print(d.get('telemt',{}).get('config',''))
except Exception: print('')
PY
)"
fi
[[ -n "$TELEMT_CONFIG" && -f "$TELEMT_CONFIG" ]] && cp -a "$TELEMT_CONFIG" "$BACKUP/telemt-config.toml" || true

systemctl disable --now mtproxyl-egressd.service >/dev/null 2>&1 || true
systemctl disable mtproxyl-egress-boot-guard.service >/dev/null 2>&1 || true

if [[ -d /etc/mtproxyl-egress/nodes.d ]]; then
    python3 - <<'PY'
from pathlib import Path
import subprocess,tomllib
for p in Path('/etc/mtproxyl-egress/nodes.d').glob('*.toml'):
    try:
        n=tomllib.load(open(p,'rb')); iface=str(n.get('awg_interface',''))
    except Exception: continue
    if iface:
        subprocess.run(['systemctl','disable','--now',f'awg-quick@{iface}.service'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        subprocess.run(['systemctl','reset-failed',f'awg-quick@{iface}.service'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try: (Path('/etc/amnezia/amneziawg')/f'{iface}.conf').unlink()
        except FileNotFoundError: pass
PY
fi

# Restore the original Telemt config captured before EgressMT first changed the NAT identity.
if [[ -n "$TELEMT_CONFIG" && -f "${TELEMT_CONFIG}.before-egressmt" ]]; then
    cp -a "${TELEMT_CONFIG}.before-egressmt" "$TELEMT_CONFIG"
fi

# Remove EgressMT routing only after its daemon is stopped.
while ip rule del priority 10999 2>/dev/null; do :; done
while ip rule del priority 11000 2>/dev/null; do :; done
for p in $(seq 10700 10899); do
    while ip rule del priority "$p" 2>/dev/null; do :; done
done
nft delete table inet mtproxyl_egress 2>/dev/null || true

rm -f /etc/systemd/system/mtproxyl-egressd.service
rm -f /etc/systemd/system/mtproxyl-egress-boot-guard.service
rm -f /etc/systemd/system/docker.service.d/10-mtproxyl-egress-boot-guard.conf
rmdir /etc/systemd/system/docker.service.d 2>/dev/null || true
rm -f /usr/local/libexec/mtproxyl-egressd /usr/local/libexec/mtproxyl-egress-registry /usr/local/libexec/mtproxyl-egress-provision /usr/local/libexec/mtproxyl-node-agent-source /usr/local/libexec/mtproxyl-egress-boot-guard
rm -f /usr/local/bin/egressmt /usr/local/bin/mtproxyl-egress
rm -rf /etc/mtproxyl-egress /var/lib/mtproxyl-egress /run/mtproxyl-egress
systemctl daemon-reload

if [[ -n "$TELEMT_CONFIG" ]] && docker inspect mtproxyl >/dev/null 2>&1; then
    docker restart mtproxyl >/dev/null || true
fi

msg "EgressMT удалён с ENTER. Удалённые EXIT VPS не изменялись." "EgressMT removed from ENTER. Remote EXIT VPS instances were not modified."
echo "Backup: $BACKUP"

#!/usr/bin/env bash
set -Eeuo pipefail

REPO="TyXeD0/EgressMT"
BRANCH="${EGRESSMT_BRANCH:-main}"
RAW="https://raw.githubusercontent.com/${REPO}/${BRANCH}"
LANG_CODE="${EGRESSMT_LANG:-en}"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="/root/egressmt-backup-$STAMP"
TMP="$(mktemp -d /tmp/egressmt-core.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

msg(){ local ru="$1" en="$2"; [[ "$LANG_CODE" == "ru" ]] && printf '%s\n' "$ru" || printf '%s\n' "$en"; }
fail(){ echo "ERROR: $*" >&2; exit 1; }

[[ ${EUID:-$(id -u)} -eq 0 ]] || fail "root required"
if [[ -r /etc/os-release ]]; then
    . /etc/os-release
    [[ "${ID:-}" == "ubuntu" ]] || fail "Ubuntu is required for v0.1.0-rc1"
fi

msg "Проверка зависимостей..." "Checking dependencies..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -y >/dev/null
apt-get install -y ca-certificates curl python3 iproute2 iputils-ping nftables openssh-client sshpass software-properties-common >/dev/null

if ! command -v docker >/dev/null 2>&1 || ! command -v mtproxyl >/dev/null 2>&1; then
    fail "MTProxyL/Telemt must be installed first"
fi

if ! command -v awg >/dev/null 2>&1 || ! command -v awg-quick >/dev/null 2>&1; then
    msg "Устанавливаю AmneziaWG..." "Installing AmneziaWG..."
    add-apt-repository -y ppa:amnezia/ppa >/dev/null
    apt-get update -y >/dev/null
    apt-get install -y amneziawg amneziawg-tools >/dev/null
fi

TELEMT_CONFIG=""
if [[ -f /opt/mtproxyl/mtproxy/config.toml ]]; then
    TELEMT_CONFIG="/opt/mtproxyl/mtproxy/config.toml"
else
    TELEMT_CONFIG="$(python3 - <<'PY'
import json, pathlib, subprocess
try:
    data=json.loads(subprocess.check_output(["docker","inspect","mtproxyl"], text=True))[0]
except Exception:
    print(""); raise SystemExit
c=[]
for m in data.get("Mounts",[]):
    src=pathlib.Path(str(m.get("Source",""))); dst=str(m.get("Destination",""))
    if src.is_file() and src.suffix==".toml":
        c.append((100 + (50 if "config.toml" in (str(src)+dst).lower() else 0), str(src)))
    elif src.is_dir():
        for name in ("config.toml","telemt.toml"):
            p=src/name
            if p.is_file(): c.append((40,str(p)))
print(max(c, default=(0,""))[1])
PY
)"
fi
[[ -n "$TELEMT_CONFIG" && -f "$TELEMT_CONFIG" ]] || fail "cannot detect Telemt config.toml"

download(){
    local path="$1" dest="$2"
    curl -fsSL --retry 4 --retry-delay 2 --retry-all-errors "${RAW}/${path}" -o "$dest"
}

for f in egressd.py cli.py registry.py provision.py node-agent.py boot-guard.sh; do
    download "src/$f" "$TMP/$f"
done
download "lib/manage.sh" "$TMP/manage.sh"

python3 -m py_compile "$TMP/egressd.py" "$TMP/cli.py" "$TMP/registry.py" "$TMP/provision.py" "$TMP/node-agent.py"
bash -n "$TMP/boot-guard.sh" "$TMP/manage.sh"

install -d -m 700 "$BACKUP"
for p in \
    /etc/mtproxyl-egress \
    /var/lib/mtproxyl-egress \
    /usr/local/libexec/mtproxyl-egressd \
    /usr/local/libexec/mtproxyl-egress-registry \
    /usr/local/libexec/mtproxyl-egress-provision \
    /usr/local/libexec/mtproxyl-node-agent-source \
    /usr/local/libexec/mtproxyl-egress-boot-guard \
    /usr/local/bin/mtproxyl-egress \
    /usr/local/bin/egressmt \
    /usr/local/bin/egressmt-menu \
    /etc/systemd/system/mtproxyl-egressd.service \
    /etc/systemd/system/mtproxyl-egress-boot-guard.service \
    /etc/systemd/system/docker.service.d/10-mtproxyl-egress-boot-guard.conf
 do
    [[ -e "$p" ]] && cp -a --parents "$p" "$BACKUP/" 2>/dev/null || true
 done

install -d -m 700 /etc/mtproxyl-egress /etc/mtproxyl-egress/nodes.d /etc/mtproxyl-egress/nodes /etc/mtproxyl-egress/ssh
install -d -m 700 /var/lib/mtproxyl-egress /run/mtproxyl-egress
install -d -m 755 /usr/local/libexec /usr/local/bin

if [[ ! -f /etc/mtproxyl-egress/config.toml ]]; then
    install -m 755 "$TMP/registry.py" /usr/local/libexec/mtproxyl-egress-registry
    /usr/local/libexec/mtproxyl-egress-registry init --telemt-config "$TELEMT_CONFIG" --container mtproxyl
fi

install -m 755 "$TMP/egressd.py" /usr/local/libexec/mtproxyl-egressd
install -m 755 "$TMP/cli.py" /usr/local/bin/mtproxyl-egress
ln -sfn /usr/local/bin/mtproxyl-egress /usr/local/bin/egressmt
install -m 755 "$TMP/manage.sh" /usr/local/bin/egressmt-menu
install -m 755 "$TMP/registry.py" /usr/local/libexec/mtproxyl-egress-registry
install -m 755 "$TMP/provision.py" /usr/local/libexec/mtproxyl-egress-provision
install -m 755 "$TMP/node-agent.py" /usr/local/libexec/mtproxyl-node-agent-source
install -m 755 "$TMP/boot-guard.sh" /usr/local/libexec/mtproxyl-egress-boot-guard

/usr/local/libexec/mtproxyl-egress-registry validate

cat >/etc/systemd/system/mtproxyl-egress-boot-guard.service <<'UNIT'
[Unit]
Description=EgressMT pre-Docker fail-closed boot guard
Documentation=https://github.com/TyXeD0/EgressMT
After=local-fs.target
Before=docker.service mtproxyl-egressd.service

[Service]
Type=oneshot
ExecStart=/usr/local/libexec/mtproxyl-egress-boot-guard
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
UNIT

install -d -m 755 /etc/systemd/system/docker.service.d
cat >/etc/systemd/system/docker.service.d/10-mtproxyl-egress-boot-guard.conf <<'DROPIN'
[Unit]
Requires=mtproxyl-egress-boot-guard.service
After=mtproxyl-egress-boot-guard.service
DROPIN

cat >/etc/systemd/system/mtproxyl-egressd.service <<'UNIT'
[Unit]
Description=EgressMT Telegram egress manager
Documentation=https://github.com/TyXeD0/EgressMT
After=network-online.target docker.service
Wants=network-online.target docker.service

[Service]
Type=simple
ExecStartPre=/usr/local/libexec/mtproxyl-egress-registry validate
ExecStart=/usr/local/libexec/mtproxyl-egressd
Restart=always
RestartSec=2
TimeoutStopSec=20
KillSignal=SIGTERM
UMask=0077
LimitNOFILE=65536
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable mtproxyl-egress-boot-guard.service mtproxyl-egressd.service >/dev/null

# Fresh installs intentionally begin in BLOCK until the first healthy EXIT is added.
# Existing EgressMT installs keep their active route and let the daemon reconcile it atomically.
if ! systemctl is-active --quiet mtproxyl-egressd.service; then
    /usr/local/libexec/mtproxyl-egress-boot-guard
fi
systemctl restart mtproxyl-egressd.service

for _ in $(seq 1 20); do
    systemctl is-active --quiet mtproxyl-egressd.service && break
    sleep 1
done
systemctl is-active --quiet mtproxyl-egressd.service || fail "EgressMT daemon failed to start"
/usr/local/bin/egressmt status --json | python3 -m json.tool >/dev/null

cat >"$BACKUP/RESTORE-NOTE.txt" <<EOF
EgressMT backup created before installation/update.
Date: $STAMP
Telemt config used by EgressMT: $TELEMT_CONFIG
This directory is root-only and may contain runtime credentials. Do not publish it.
EOF
chmod -R go-rwx "$BACKUP"

msg "EgressMT Core установлен." "EgressMT Core installed."
echo "Backup: $BACKUP"
echo
/usr/local/bin/egressmt status

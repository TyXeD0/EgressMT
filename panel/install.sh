#!/usr/bin/env bash
set -Eeuo pipefail

REPO="TyXeD0/EgressMT"
BRANCH="${EGRESSMT_BRANCH:-main}"
RAW="https://raw.githubusercontent.com/${REPO}/${BRANCH}"
LANG_CODE="${EGRESSMT_LANG:-en}"
BASE_COMMIT="8e6ef1d598a2d4f3af2b4a81ac028b0f9ae7afe5"
CUSTOM_VERSION="1.0.14-egressmt-rc1"
UPSTREAM="https://github.com/Liafanx/MTProxyL.git"

WORK="/opt/egressmt-panel-build"
SRC="$WORK/MTProxyL"
PANEL_SRC="$SRC/mtproxyl-panel"
ASSETS="$WORK/assets"
PANEL_BIN="/usr/local/bin/mtproxyl-panel"
PANEL_CFG="/etc/mtproxyl-panel/config.toml"
PANEL_SERVICE="mtproxyl-panel.service"
BRIDGE="/usr/local/sbin/mtproxyl-egress-panel-bridge"
JOB_RUNNER="/usr/local/libexec/mtproxyl-egress-panel-job"
PROVISIONER="/usr/local/libexec/mtproxyl-egress-provision"
SUDOERS="/etc/sudoers.d/mtproxyl-panel-egress"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="/root/egressmt-panel-backup-$STAMP"

msg(){ [[ "$LANG_CODE" == "ru" ]] && echo "$1" || echo "$2"; }
fail(){ echo "ERROR: $*" >&2; exit 1; }
[[ ${EUID:-$(id -u)} -eq 0 ]] || fail "root required"

for c in git docker python3 sudo visudo systemctl install curl; do command -v "$c" >/dev/null 2>&1 || fail "missing command: $c"; done
[[ -x /usr/local/bin/egressmt ]] || fail "EgressMT Core must be installed first"
[[ -x "$PROVISIONER" ]] || fail "EgressMT EXIT provisioner is missing"
systemctl is-active --quiet mtproxyl-egressd.service || fail "EgressMT daemon is not active"

if [[ ! -x "$PANEL_BIN" || ! -f "$PANEL_CFG" ]]; then
    msg "Устанавливаю официальную MTProxyL Panel..." "Installing the official MTProxyL Panel..."
    mtproxyl panel install
fi
[[ -x "$PANEL_BIN" && -f "$PANEL_CFG" ]] || fail "MTProxyL Panel is not installed"
id mtproxyl-panel >/dev/null 2>&1 || fail "mtproxyl-panel system user is missing"

/usr/local/libexec/mtproxyl-egress-registry validate
"$PROVISIONER" preflight | python3 -m json.tool >/dev/null
CURRENT_PANEL="$($PANEL_BIN version 2>/dev/null || true)"

msg "Подготавливаю EgressMT Panel..." "Preparing EgressMT Panel..."
rm -rf "$WORK"
install -d -m 755 "$ASSETS/backend" "$ASSETS/frontend"

download(){ local path="$1" dest="$2"; curl -fsSL --retry 4 --retry-delay 2 --retry-all-errors "${RAW}/${path}" -o "$dest"; }
download panel/patch.py "$ASSETS/patch.py"
download panel/bridge.sh "$ASSETS/bridge.sh"
download panel/job-runner.py "$ASSETS/job-runner.py"
download panel/backend/egress.go "$ASSETS/backend/egress.go"
download panel/frontend/EgressPage.tsx "$ASSETS/frontend/EgressPage.tsx"
download panel/frontend/api.fragment.ts "$ASSETS/frontend/api.fragment.ts"
python3 -m py_compile "$ASSETS/patch.py" "$ASSETS/job-runner.py"
bash -n "$ASSETS/bridge.sh"

install -d -m 700 "$BACKUP"
cp -a "$PANEL_BIN" "$BACKUP/mtproxyl-panel"
cp -a "$PANEL_CFG" "$BACKUP/config.toml"
cp -a "/etc/systemd/system/$PANEL_SERVICE" "$BACKUP/" 2>/dev/null || true
cp -a "$BRIDGE" "$BACKUP/mtproxyl-egress-panel-bridge" 2>/dev/null || true
cp -a "$JOB_RUNNER" "$BACKUP/mtproxyl-egress-panel-job" 2>/dev/null || true
cp -a "$SUDOERS" "$BACKUP/mtproxyl-panel-egress.sudoers" 2>/dev/null || true
printf '%s\n' "$CURRENT_PANEL" >"$BACKUP/version.txt"
chmod -R go-rwx "$BACKUP"

msg "Получаю исходный код MTProxyL Panel..." "Fetching MTProxyL Panel source..."
git clone --filter=blob:none --no-checkout "$UPSTREAM" "$SRC"
git -C "$SRC" checkout "$BASE_COMMIT"
[[ "$(git -C "$SRC" rev-parse HEAD)" == "$BASE_COMMIT" ]] || fail "upstream commit mismatch"
python3 "$ASSETS/patch.py" "$PANEL_SRC" "$ASSETS"

case "$(uname -m)" in
    x86_64|amd64) ARCH="amd64" ;;
    aarch64|arm64) ARCH="arm64" ;;
    *) fail "unsupported architecture: $(uname -m)" ;;
esac
IMAGE="egressmt-panel:$CUSTOM_VERSION"
OUT="$WORK/mtproxyl-panel-egressmt"
msg "Собираю панель. Это может занять несколько минут..." "Building the panel. This may take several minutes..."
docker build --build-arg TARGETARCH="$ARCH" --build-arg VERSION="$CUSTOM_VERSION" -t "$IMAGE" "$PANEL_SRC"
CID="$(docker create "$IMAGE")"
cleanup_cid(){ docker rm -f "$CID" >/dev/null 2>&1 || true; }
trap cleanup_cid EXIT
docker cp "$CID:/usr/local/bin/mtproxyl-panel" "$OUT"
cleanup_cid
trap - EXIT
chmod 755 "$OUT"
[[ "$($OUT version)" == *"$CUSTOM_VERSION"* ]] || fail "built panel version mismatch"

install -d -m 755 /usr/local/libexec /usr/local/sbin
install -o root -g root -m 755 "$ASSETS/job-runner.py" "$JOB_RUNNER"
install -o root -g root -m 755 "$ASSETS/bridge.sh" "$BRIDGE"
python3 -m py_compile "$JOB_RUNNER"
"$JOB_RUNNER" gc --days 7 >/dev/null

cat >"$SUDOERS" <<EOF
# MTProxyL Panel -> restricted EgressMT bridge only.
mtproxyl-panel ALL=(root) NOPASSWD: $BRIDGE
EOF
chmod 440 "$SUDOERS"; chown root:root "$SUDOERS"; visudo -cf "$SUDOERS"
sudo -u mtproxyl-panel sudo -n "$BRIDGE" status | python3 -m json.tool >/dev/null
sudo -u mtproxyl-panel sudo -n "$BRIDGE" config-get | python3 -m json.tool >/dev/null
sudo -u mtproxyl-panel sudo -n "$BRIDGE" provision-preflight | python3 -m json.tool >/dev/null

cat >/root/rollback-egressmt-panel.sh <<EOF
#!/usr/bin/env bash
set -Eeuo pipefail
systemctl stop "$PANEL_SERVICE" || true
install -o root -g root -m 755 "$BACKUP/mtproxyl-panel" "$PANEL_BIN"
if [[ -f "$BACKUP/mtproxyl-egress-panel-bridge" ]]; then install -m 755 "$BACKUP/mtproxyl-egress-panel-bridge" "$BRIDGE"; else rm -f "$BRIDGE"; fi
if [[ -f "$BACKUP/mtproxyl-egress-panel-job" ]]; then install -m 755 "$BACKUP/mtproxyl-egress-panel-job" "$JOB_RUNNER"; else rm -f "$JOB_RUNNER"; fi
if [[ -f "$BACKUP/mtproxyl-panel-egress.sudoers" ]]; then install -m 440 "$BACKUP/mtproxyl-panel-egress.sudoers" "$SUDOERS"; else rm -f "$SUDOERS"; fi
systemctl daemon-reload
systemctl start "$PANEL_SERVICE"
"$PANEL_BIN" version
EOF
chmod 700 /root/rollback-egressmt-panel.sh

install -o root -g root -m 755 "$OUT" "$PANEL_BIN.new"
systemctl stop "$PANEL_SERVICE"
mv "$PANEL_BIN.new" "$PANEL_BIN"
if ! systemctl start "$PANEL_SERVICE"; then
    /root/rollback-egressmt-panel.sh || true
    fail "custom panel failed to start; previous panel restored"
fi
sleep 3
systemctl is-active --quiet "$PANEL_SERVICE" || { /root/rollback-egressmt-panel.sh || true; fail "custom panel is not active; previous panel restored"; }

"$PANEL_BIN" version
sudo -u mtproxyl-panel sudo -n "$BRIDGE" status | python3 -m json.tool >/dev/null
msg "EgressMT Panel установлена. Откройте раздел EgressMT · EXIT nodes." "EgressMT Panel installed. Open EgressMT · EXIT nodes."
echo "Backup:   $BACKUP"
echo "Rollback: /root/rollback-egressmt-panel.sh"

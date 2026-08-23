#!/usr/bin/env bash
set -Eeuo pipefail

LANG_FILE="/etc/egressmt/language"
LANG_CODE="${EGRESSMT_LANG:-$(cat "$LANG_FILE" 2>/dev/null || echo en)}"
CLI="/usr/local/bin/egressmt"
PROVISION="/usr/local/libexec/mtproxyl-egress-provision"
RUN="/run/mtproxyl-egress"

[[ ${EUID:-$(id -u)} -eq 0 ]] || exec sudo -- "$0" "$@"
[[ -x "$CLI" && -x "$PROVISION" ]] || { echo "EgressMT core is not installed" >&2; exit 1; }

m(){
  case "$LANG_CODE:$1" in
    ru:title) echo "EgressMT — выходные ноды";; en:title) echo "EgressMT — egress nodes";;
    ru:add) echo "Добавить выходную ноду";; en:add) echo "Add egress node";;
    ru:list) echo "Статус и список нод";; en:list) echo "Status and node list";;
    ru:auto) echo "Включить AUTO";; en:auto) echo "Enable AUTO";;
    ru:switch) echo "Переключиться на ноду вручную";; en:switch) echo "Switch to a node manually";;
    ru:remove) echo "Удалить выходную ноду";; en:remove) echo "Remove egress node";;
    ru:back) echo "Назад";; en:back) echo "Back";;
    ru:name) echo "Понятное название ноды (например: резерв-1)";; en:name) echo "Friendly node name (for example: backup-1)";;
    ru:host) echo "IP или домен выходного VPS";; en:host) echo "Egress VPS IP or hostname";;
    ru:port) echo "SSH порт [22]";; en:port) echo "SSH port [22]";;
    ru:prio) echo "Приоритет (меньше = важнее; Enter = автоматически)";; en:prio) echo "Priority (lower = preferred; Enter = automatic)";;
    ru:auth) echo "SSH: 1) уже настроенный ключ  2) пароль  3) private key из файла";; en:auth) echo "SSH: 1) existing key  2) password  3) private key from file";;
    ru:pass) echo "SSH пароль";; en:pass) echo "SSH password";;
    ru:keypath) echo "Путь к private key";; en:keypath) echo "Private key path";;
    ru:node) echo "ID или имя ноды";; en:node) echo "Node ID or name";;
    ru:remote) echo "Также очистить удалённый выходной VPS по SSH? [y/N]";; en:remote) echo "Also clean the remote egress VPS over SSH? [y/N]";;
    ru:fallback) echo "Если это последняя нода: 1) BLOCK (рекомендуется)  2) DIRECT";; en:fallback) echo "If this is the last node: 1) BLOCK (recommended)  2) DIRECT";;
    *) echo "$1";;
  esac
}

yes(){ [[ "${1,,}" =~ ^(y|yes|д|да)$ ]]; }

read_auth(){
  AUTH_MODE="auto"; AUTH_SECRET_FILE=""
  echo "$(m auth)"; printf "> "; read -r a
  case "$a" in
    2)
      AUTH_MODE="password"; printf '%s: ' "$(m pass)"; read -rs secret; echo
      install -d -m 700 "$RUN"; AUTH_SECRET_FILE="$(mktemp "$RUN/.auth.XXXXXX")"; chmod 600 "$AUTH_SECRET_FILE"; printf '%s' "$secret" >"$AUTH_SECRET_FILE"; secret="" ;;
    3)
      AUTH_MODE="key"; printf '%s: ' "$(m keypath)"; read -r kp; [[ -f "$kp" ]] || { echo "File not found" >&2; return 1; }
      install -d -m 700 "$RUN"; AUTH_SECRET_FILE="$(mktemp "$RUN/.auth.XXXXXX")"; chmod 600 "$AUTH_SECRET_FILE"; cat "$kp" >"$AUTH_SECRET_FILE" ;;
    *) AUTH_MODE="auto" ;;
  esac
}

cleanup_auth(){ [[ -n "${AUTH_SECRET_FILE:-}" ]] && rm -f "$AUTH_SECRET_FILE" || true; AUTH_SECRET_FILE=""; }
trap cleanup_auth EXIT

add_node(){
  printf '%s: ' "$(m name)"; read -r name
  printf '%s: ' "$(m host)"; read -r host
  printf '%s: ' "$(m port)"; read -r port; port="${port:-22}"
  printf '%s: ' "$(m prio)"; read -r prio
  read_auth
  req="$(python3 - "$name" "$host" "$port" "$prio" "$AUTH_MODE" "${AUTH_SECRET_FILE:-}" <<'PY'
import json,sys
name,host,port,prio,mode,path=sys.argv[1:]
secret=open(path).read() if path else ""
d={"action":"add","name":name,"host":host,"port":int(port),"user":"root","auth":{"mode":mode,"secret":secret}}
if prio.strip(): d["priority"]=int(prio)
print(json.dumps(d,ensure_ascii=False))
PY
)"
  cleanup_auth
  printf '%s\n' "$req" | "$PROVISION" request --pretty
  "$CLI" auto >/dev/null || true
  echo; "$CLI" status
}

remove_node(){
  "$CLI" node list; echo
  printf '%s: ' "$(m node)"; read -r node
  printf '%s ' "$(m remote)"; read -r r
  remote=false; [[ -n "$r" ]] && yes "$r" && remote=true
  fallback=block; echo "$(m fallback)"; printf "> "; read -r f; [[ "$f" == "2" ]] && fallback=direct
  AUTH_MODE="auto"; AUTH_SECRET_FILE=""
  [[ "$remote" == true ]] && read_auth
  req="$(python3 - "$node" "$remote" "$fallback" "$AUTH_MODE" "${AUTH_SECRET_FILE:-}" <<'PY'
import json,sys
node,remote,fallback,mode,path=sys.argv[1:]
secret=open(path).read() if path else ""
print(json.dumps({"action":"remove","node":node,"remote_cleanup":remote=="true","fallback":fallback,"auth":{"mode":mode,"secret":secret}},ensure_ascii=False))
PY
)"
  cleanup_auth
  printf '%s\n' "$req" | "$PROVISION" request --pretty
  echo; "$CLI" status
}

while true; do
  echo; echo "=============================="; echo "$(m title)"; echo "=============================="
  echo "1) $(m add)"; echo "2) $(m list)"; echo "3) $(m auto)"; echo "4) $(m switch)"; echo "5) $(m remove)"; echo "0) $(m back)"; echo; printf "> "; read -r c
  case "$c" in
    1) add_node;;
    2) "$CLI" status;;
    3) "$CLI" auto; "$CLI" status;;
    4) "$CLI" node list; printf '%s: ' "$(m node)"; read -r n; "$CLI" switch "$n"; "$CLI" status;;
    5) remove_node;;
    0) exit 0;;
  esac
done

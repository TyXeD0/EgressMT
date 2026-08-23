#!/usr/bin/env bash
set -Eeuo pipefail

MODE="${1:-human}"
STATE_DIR="/var/lib/egressmt"
STATE_FILE="$STATE_DIR/upstream-compat.json"
UPSTREAM_RAW="https://raw.githubusercontent.com/Liafanx/MTProxyL/main"

json_escape(){ python3 -c 'import json,sys; print(json.dumps(sys.stdin.read().rstrip("\n")))'; }

have(){ command -v "$1" >/dev/null 2>&1; }

MTPROXYL_INSTALLED=""
MTPROXYL_LATEST=""
ENGINE_BACKEND=""
ENGINE_CURRENT=""
ENGINE_LATEST=""
ENGINE_CONFIG=""
ENGINE_RUNNING="false"
PANEL_INSTALLED=""
PANEL_LATEST=""
PANEL_PATCHED="false"
PANEL_NEEDS_REPATCH="false"
CONTRACT_OK="false"
CONTRACT_REASON=""
PREV_MTPROXYL=""
PREV_ENGINE=""
PREV_PANEL=""

if have mtproxyl; then
    MTPROXYL_INSTALLED="$(mtproxyl version 2>/dev/null | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' | head -n1 || true)"
    MODE_JSON="$(mtproxyl mode --json 2>/dev/null || true)"
    ENGINE_JSON="$(mtproxyl engine versions 2>/dev/null || true)"

    read -r ENGINE_BACKEND ENGINE_CONFIG ENGINE_RUNNING <<<"$(python3 - "$MODE_JSON" <<'PY'
import json,sys
try:d=json.loads(sys.argv[1])
except Exception:d={}
print(d.get('engine',''), d.get('engine_config',''), 'true' if d.get('running') else 'false')
PY
)"

    read -r ENGINE_CURRENT ENGINE_LATEST <<<"$(python3 - "$ENGINE_JSON" <<'PY'
import json,sys
try:d=json.loads(sys.argv[1])
except Exception:d={}
rels=d.get('releases') or []
latest=(rels[0].get('tag','') if rels else '')
print(str(d.get('current','')), str(latest).lstrip('v'))
PY
)"

    if [[ -n "$MODE_JSON" && -n "$ENGINE_JSON" && -n "$ENGINE_CONFIG" && -f "$ENGINE_CONFIG" ]]; then
        if python3 - "$ENGINE_CONFIG" <<'PY' >/dev/null 2>&1
import sys,tomllib
with open(sys.argv[1],'rb') as f: tomllib.load(f)
PY
        then
            DC_JSON="$(mtproxyl dc status --json 2>/dev/null || true)"
            if python3 - "$DC_JSON" <<'PY' >/dev/null 2>&1
import json,sys
d=json.loads(sys.argv[1])
assert isinstance(d,dict) and 'available' in d and 'verdict' in d
PY
            then
                # v0.1.0-rc1 currently supports the Docker backend. MTProxyL 1.5.4+
                # also offers a binary backend; report it explicitly instead of
                # pretending it is compatible with Docker-specific lifecycle code.
                if [[ "$ENGINE_BACKEND" == "docker" ]]; then
                    CONTRACT_OK="true"
                else
                    CONTRACT_REASON="engine backend '$ENGINE_BACKEND' is not supported by EgressMT v0.1.0-rc1"
                fi
            else
                CONTRACT_REASON="mtproxyl dc status --json contract is unavailable"
            fi
        else
            CONTRACT_REASON="Telemt configuration is not valid TOML"
        fi
    else
        CONTRACT_REASON="MTProxyL mode/engine contract or engine config is unavailable"
    fi
else
    CONTRACT_REASON="MTProxyL is not installed"
fi

if have curl; then
    MTPROXYL_LATEST="$(curl -fsSL --max-time 8 "$UPSTREAM_RAW/version" 2>/dev/null | tr -cd '0-9.\n' | head -n1 || true)"
    PANEL_LATEST="$(curl -fsSL --max-time 8 "$UPSTREAM_RAW/mtproxyl-panel/main.go" 2>/dev/null | sed -nE 's/^[[:space:]]*var version = "([^"]+)".*/\1/p' | head -n1 || true)"
fi

if have mtproxyl-panel; then
    PANEL_INSTALLED="$(mtproxyl-panel version 2>/dev/null | awk '{print $NF}' | head -n1 || true)"
    [[ "$PANEL_INSTALLED" == *-egressmt* ]] && PANEL_PATCHED="true"
    if [[ -n "$PANEL_LATEST" ]]; then
        if [[ "$PANEL_PATCHED" != "true" ]]; then
            PANEL_NEEDS_REPATCH="true"
        else
            BASE="${PANEL_INSTALLED%%-egressmt*}"
            [[ "$BASE" != "$PANEL_LATEST" ]] && PANEL_NEEDS_REPATCH="true"
        fi
    fi
fi

if [[ -f "$STATE_FILE" ]]; then
    read -r PREV_MTPROXYL PREV_ENGINE PREV_PANEL <<<"$(python3 - "$STATE_FILE" <<'PY'
import json,sys
try:d=json.load(open(sys.argv[1]))
except Exception:d={}
print(d.get('mtproxyl',''), d.get('telemt',''), d.get('panel',''))
PY
)"
fi

MTPROXYL_CHANGED="false"; ENGINE_CHANGED="false"; PANEL_CHANGED="false"
[[ -n "$PREV_MTPROXYL" && -n "$MTPROXYL_INSTALLED" && "$PREV_MTPROXYL" != "$MTPROXYL_INSTALLED" ]] && MTPROXYL_CHANGED="true"
[[ -n "$PREV_ENGINE" && -n "$ENGINE_CURRENT" && "$PREV_ENGINE" != "$ENGINE_CURRENT" ]] && ENGINE_CHANGED="true"
[[ -n "$PREV_PANEL" && -n "$PANEL_INSTALLED" && "$PREV_PANEL" != "$PANEL_INSTALLED" ]] && PANEL_CHANGED="true"

emit_json(){
python3 - "$MTPROXYL_INSTALLED" "$MTPROXYL_LATEST" "$ENGINE_BACKEND" "$ENGINE_CURRENT" "$ENGINE_LATEST" "$ENGINE_CONFIG" "$ENGINE_RUNNING" "$PANEL_INSTALLED" "$PANEL_LATEST" "$PANEL_PATCHED" "$PANEL_NEEDS_REPATCH" "$CONTRACT_OK" "$CONTRACT_REASON" "$MTPROXYL_CHANGED" "$ENGINE_CHANGED" "$PANEL_CHANGED" <<'PY'
import json,sys
(v,latest,backend,eng,eng_latest,cfg,running,panel,panel_latest,patched,repatch,ok,reason,mchg,echg,pchg)=sys.argv[1:]
b=lambda x:x=='true'
print(json.dumps({
  'ok': b(ok),
  'reason': reason,
  'mtproxyl': {'installed':v,'latest':latest,'changed_since_check':b(mchg)},
  'telemt': {'backend':backend,'installed':eng,'latest':eng_latest,'config':cfg,'running':b(running),'changed_since_check':b(echg)},
  'panel': {'installed':panel,'latest_upstream':panel_latest,'egressmt_patch':b(patched),'needs_repatch':b(repatch),'changed_since_check':b(pchg)},
}, ensure_ascii=False))
PY
}

record(){
    install -d -m 700 "$STATE_DIR"
    python3 - "$STATE_FILE" "$MTPROXYL_INSTALLED" "$ENGINE_CURRENT" "$PANEL_INSTALLED" <<'PY'
import json,sys,tempfile,os
path,mt,te,pa=sys.argv[1:]
data={'mtproxyl':mt,'telemt':te,'panel':pa}
fd,tmp=tempfile.mkstemp(prefix='.compat.',dir=os.path.dirname(path)); os.fchmod(fd,0o600)
with os.fdopen(fd,'w') as f: json.dump(data,f,indent=2); f.write('\n'); f.flush(); os.fsync(f.fileno())
os.replace(tmp,path)
PY
}

case "$MODE" in
    --json|json)
        emit_json
        ;;
    --record|record)
        [[ "$CONTRACT_OK" == "true" ]] || { emit_json; exit 2; }
        record
        emit_json
        ;;
    human|"")
        echo "EgressMT upstream compatibility"
        echo "==============================="
        printf 'MTProxyL: %s' "${MTPROXYL_INSTALLED:-not installed}"
        [[ -n "$MTPROXYL_LATEST" ]] && printf ' (upstream %s)' "$MTPROXYL_LATEST"
        echo
        printf 'Telemt:   %s [%s]' "${ENGINE_CURRENT:-unknown}" "${ENGINE_BACKEND:-unknown}"
        [[ -n "$ENGINE_LATEST" ]] && printf ' (latest %s)' "$ENGINE_LATEST"
        echo
        printf 'Panel:    %s' "${PANEL_INSTALLED:-not installed}"
        [[ -n "$PANEL_LATEST" ]] && printf ' (upstream %s)' "$PANEL_LATEST"
        echo
        echo "Contract: $([[ "$CONTRACT_OK" == true ]] && echo OK || echo FAIL)"
        [[ -n "$CONTRACT_REASON" ]] && echo "Reason:   $CONTRACT_REASON"
        [[ "$PANEL_NEEDS_REPATCH" == true ]] && echo "Panel:    EgressMT patch must be rebuilt/reapplied"
        ;;
    *)
        echo "usage: $0 [--json|--record]" >&2
        exit 2
        ;;
esac

#!/usr/bin/env bash
set -Eeuo pipefail

CLI="/usr/local/bin/egressmt"
PROVISION="/usr/local/libexec/mtproxyl-egress-provision"
JOB="/usr/local/libexec/mtproxyl-egress-panel-job"
fail(){ echo "$*" >&2; exit 1; }
[[ -x "$CLI" ]] || fail "EgressMT CLI missing"

case "${1:-}" in
  status) exec "$CLI" status --json ;;
  mode)
    case "${2:-}" in
      auto) exec "$CLI" auto ;;
      direct) exec "$CLI" direct ;;
      block) exec "$CLI" block ;;
      manual) [[ -n "${3:-}" ]] || fail "manual node required"; exec "$CLI" switch "$3" ;;
      *) fail "invalid mode" ;;
    esac ;;
  node-test) [[ -n "${2:-}" ]] || fail "node required"; exec "$CLI" node test "$2" ;;
  node-rename) [[ -n "${2:-}" && -n "${3:-}" ]] || fail "node and name required"; exec "$CLI" node rename "$2" "$3" ;;
  node-enable) [[ -n "${2:-}" ]] || fail "node required"; exec "$CLI" node enable "$2" ;;
  node-disable) [[ -n "${2:-}" ]] || fail "node required"; exec "$CLI" node disable "$2" ;;
  node-priority)
    [[ -n "${2:-}" && "${3:-}" =~ ^[0-9]+$ ]] || fail "node and numeric priority required"
    (( 10#$3 >= 1 && 10#$3 <= 9999 )) || fail "invalid priority"
    exec "$CLI" node priority "$2" "$3" ;;
  config-get) exec "$CLI" config get ;;
  config-set)
    for v in "${2:-}" "${3:-}" "${4:-}" "${5:-}"; do [[ "$v" =~ ^[0-9]+$ ]] || fail "configuration values must be integers"; done
    exec "$CLI" config set "$2" "$3" "$4" "$5" ;;
  events)
    n="${2:-30}"; [[ "$n" =~ ^[0-9]+$ ]] || fail "invalid limit"; (( n>=1 && n<=200 )) || fail "invalid limit"
    "$CLI" events "$n" | python3 -c 'import json,sys; print(json.dumps([x.rstrip("\n") for x in sys.stdin],ensure_ascii=False))' ;;
  provision-preflight) [[ -x "$PROVISION" ]] || fail "EXIT provisioner missing"; exec "$PROVISION" preflight ;;
  job-start) [[ -x "$PROVISION" && -x "$JOB" ]] || fail "provisioner/job runner missing"; exec "$JOB" start ;;
  job-status) [[ -x "$JOB" && "${2:-}" =~ ^j-[0-9a-f]{16}$ ]] || fail "invalid job"; exec "$JOB" status "$2" ;;
  *) fail "unsupported EgressMT panel action" ;;
esac

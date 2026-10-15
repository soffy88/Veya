#!/usr/bin/env bash
# Periodic Veya endpoint + host-memory check. Designed for a systemd user timer.
set -uo pipefail

LOCAL_URL="${VEYA_UPTIME_LOCAL_URL:-http://127.0.0.1:8767/health}"
PUBLIC_URL="${VEYA_UPTIME_PUBLIC_URL:-https://veya.aiinote.com/team}"
NOTIFY_URL="${VEYA_UPTIME_NOTIFY_URL:-http://127.0.0.1:8767/integrations/notify}"
STATE_FILE="${VEYA_UPTIME_STATE:-${HOME}/.local/state/veya-uptime.json}"
MEMORY_THRESHOLD_KB="${VEYA_UPTIME_MEMORY_THRESHOLD_KB:-2097152}"
CHECKED_AT="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

log_event() {
    if command -v logger >/dev/null 2>&1; then
        logger -t veya-uptime -- "$*" || true
    fi
    printf '%s %s\n' "$CHECKED_AT" "$*"
}

mkdir -p "$(dirname "$STATE_FILE")" || {
    log_event "ERROR state directory unavailable; cannot persist uptime state"
    exit 1
}

# Capture body and status without treating HTTP 4xx/5xx as transport failures.
local_raw="$(curl --silent --show-error --connect-timeout 3 --max-time 6 \
    --write-out $'\n%{http_code}' "$LOCAL_URL" 2>/dev/null || true)"
local_status="${local_raw##*$'\n'}"
local_body="${local_raw%$'\n'*}"
[[ "$local_status" =~ ^[0-9]{3}$ ]] || local_status=000
local_ok=0
if [[ "$local_status" == 200 ]] && printf '%s' "$local_body" \
    | grep -Eq '"status"[[:space:]]*:[[:space:]]*"ok"'; then
    local_ok=1
fi

public_status="$(curl --silent --show-error --connect-timeout 5 --max-time 10 \
    --output /dev/null --write-out '%{http_code}' "$PUBLIC_URL" 2>/dev/null || true)"
[[ "$public_status" =~ ^[0-9]{3}$ ]] || public_status=000
public_ok=0
[[ "$public_status" == 200 ]] && public_ok=1

memory_available_kb="$(awk '/^MemAvailable:/ { print $2; exit }' /proc/meminfo 2>/dev/null || true)"
[[ "$memory_available_kb" =~ ^[0-9]+$ ]] || memory_available_kb=0

# One atomically replaced JSON state; no parallel scheduler or alert authority.
decision="$(python3 - "$STATE_FILE" "$CHECKED_AT" "$local_ok" "$local_status" \
    "$public_ok" "$public_status" "$memory_available_kb" "$MEMORY_THRESHOLD_KB" <<'PY'
import json
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
checked_at = sys.argv[2]
local_ok, local_status = int(sys.argv[3]), sys.argv[4]
public_ok, public_status = int(sys.argv[5]), sys.argv[6]
mem_kb, threshold_kb = int(sys.argv[7]), int(sys.argv[8])
try:
    previous = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(previous, dict):
        previous = {}
except (OSError, ValueError):
    previous = {}

healthy = bool(local_ok and public_ok)
failures = 0 if healthy else int(previous.get("consecutive_failures", 0)) + 1
failure_alert = int(not healthy and failures >= 2 and not previous.get("failure_alerted", False))
low_memory = mem_kb > 0 and mem_kb < threshold_kb
memory_alert = int(low_memory and not previous.get("memory_alerted", False))
state = {
    "checked_at": checked_at,
    "local_health": {"ok": bool(local_ok), "http_status": local_status},
    "public_team": {"ok": bool(public_ok), "http_status": public_status},
    "consecutive_failures": failures,
    "failure_alerted": bool(failure_alert or (not healthy and previous.get("failure_alerted", False))),
    "mem_available_kb": mem_kb,
    "memory_threshold_kb": threshold_kb,
    "memory_low": low_memory,
    "memory_alerted": bool(memory_alert or (low_memory and previous.get("memory_alerted", False))),
}
if healthy:
    state["failure_alerted"] = False
if not low_memory:
    state["memory_alerted"] = False
path.parent.mkdir(parents=True, exist_ok=True)
tmp = path.with_name(path.name + ".tmp." + str(os.getpid()))
with tmp.open("x", encoding="utf-8") as stream:
    json.dump(state, stream, ensure_ascii=False, indent=2)
    stream.write("\n")
os.replace(tmp, path)
print(f"{failures}\t{failure_alert}\t{memory_alert}")
PY
)" || {
    log_event "ERROR unable to update state file"
    exit 1
}
IFS=$'\t' read -r consecutive_failures failure_alert memory_alert <<< "$decision"
log_event "check local=$local_status public=$public_status mem_available_kb=$memory_available_kb consecutive_failures=$consecutive_failures"

alert_message=""
if [[ "$failure_alert" == 1 ]]; then
    alert_message="Veya availability alert: local /health HTTP ${local_status}; public /team HTTP ${public_status}; failed checks=${consecutive_failures} consecutive times."
fi
if [[ "$memory_alert" == 1 ]]; then
    [[ -n "$alert_message" ]] && alert_message+=" "
    alert_message+="Veya host memory warning: MemAvailable=${memory_available_kb} KiB, below ${MEMORY_THRESHOLD_KB} KiB (2 GiB default)."
fi

if [[ -n "$alert_message" ]]; then
    payload="$(python3 - "$alert_message" <<'PY'
import json
import sys
print(json.dumps({"title": "Veya operations alert", "content": sys.argv[1], "severity": "warning", "source": "veya-uptime"}, ensure_ascii=False))
PY
)"
    # Existing server/routes/integrations.py POST /integrations/notify API.
    # If the backend/channel is down, journald + the JSON state remain fallback.
    if curl --silent --show-error --fail --connect-timeout 2 --max-time 5 \
        --request POST "${NOTIFY_URL}?event=veya_uptime_alert" \
        --header 'Content-Type: application/json' --data-binary "$payload" \
        --output /dev/null 2>/dev/null; then
        log_event "alert delivered through existing Veya integrations API: $alert_message"
    else
        log_event "ALERT channel unavailable; persisted in state and journal: $alert_message"
    fi
fi

# A failed probe must not mark the timer service as stopped; state records it,
# while OnUnitActiveSec keeps the next check scheduled.
exit 0

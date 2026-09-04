#!/bin/sh
# Read-only health check. Safe to run while the logger is capturing. No sudo.
set -e
DIR=$(cd "$(dirname "$0")/.." && pwd)
eval "$(python3 "$DIR/config.py" --sh)"
LABEL=$SIGEN_LAUNCHD_LABEL
LOG=$SIGEN_LOG_DIR/sigen.log

echo "=== daemon ($LABEL) ==="
launchctl print "system/${LABEL}" 2>/dev/null \
  | grep -E "^	state|^	pid|last exit code" || echo "not loaded"
echo "=== heartbeat (last 5) ==="
grep -E "^  \[2[0-9]{3}-" "$LOG" 2>/dev/null | tail -5 || echo "none yet"
echo "=== warnings (gap/degraded/recovered) ==="
# grep -c already prints 0 on no match, and exits 1 while doing so; `|| echo 0`
# would append a SECOND zero and break the line in two.
count() {
  n=$(grep -cE "$1" "$LOG" 2>/dev/null) || n=0
  echo "${n:-0}"
}
printf "  gap: %s   degraded: %s   recovered: %s\n" \
  "$(count '\[gap\]')" "$(count '\[degraded\]')" "$(count '\[recovered\]')"
echo "=== viewer ($SIGEN_WEB_LAUNCHD_LABEL) ==="
# The viewer is a separate daemon and reads the archive only -- it is never a
# second Modbus client -- so "not loaded" here says nothing about capture health.
launchctl print "system/${SIGEN_WEB_LAUNCHD_LABEL}" 2>/dev/null \
  | grep -E "^	state|^	pid|last exit code" || echo "not loaded"
if nc -z 127.0.0.1 "$SIGEN_WEB_PORT" 2>/dev/null; then
  printf "  listening on %s:%s\n" "$SIGEN_WEB_BIND" "$SIGEN_WEB_PORT"
else
  printf "  nothing listening on port %s\n" "$SIGEN_WEB_PORT"
fi
tail -3 "$SIGEN_LOG_DIR/viewer.err" 2>/dev/null | sed 's/^/  err: /' || true

echo "=== watchdog ($SIGEN_WATCHDOG_LAUNCHD_LABEL) ==="
# "state = not running" is the HEALTHY state here: this is a StartInterval job that
# finishes, so between runs there is nothing to be running. The signal is the interval and
# the last exit code, not the state.
launchctl print "system/${SIGEN_WATCHDOG_LAUNCHD_LABEL}" 2>/dev/null \
  | grep -E "^	state|last exit code|run interval" \
  || echo "not loaded -- NOTHING is watching for a stalled capture"
# The state file is the point of the whole daemon: whether the last check thought capture
# was healthy, and since when if not.
tail -6 "$SIGEN_LOG_DIR/watchdog.state" 2>/dev/null | sed 's/^/  /' \
  || echo "  no watchdog.state yet -- it has not run"
if [ -f "$DIR/alert.conf" ]; then
  echo "  alert.conf present, so a breach can be reported"
else
  echo "  NO alert.conf -- a breach would be detected and never reach you"
fi

echo "=== polling ==="
# Which address the logger is ACTUALLY using. It differs from config.json's host once
# rediscovery has adopted a new one, and that difference is the thing you want to see
# before going to look at the wrong device. See discover.py and FINDINGS 43.
live=$(cat "$SIGEN_LOG_DIR/logger-host" 2>/dev/null) || live=''
if [ -z "$live" ]; then
  echo "  no logger-host file; assume config.json's host ($SIGEN_HOST)"
elif [ "$live" = "$SIGEN_HOST" ]; then
  echo "  $live (as configured)"
else
  echo "  $live  <- ADOPTED, config.json still says $SIGEN_HOST"
  echo "    Rediscovery moved it. Capture is fine, but config.json is what a restart"
  echo "    uses, so set host to the adopted address to survive one."
fi

echo "=== archive ==="
printf "  files: %s   size: %s\n" \
  "$(ls "$SIGEN_DATA_DIR"/*.bin* 2>/dev/null | wc -l | tr -d ' ')" \
  "$(du -sh "$SIGEN_DATA_DIR" 2>/dev/null | cut -f1)"
ls -lt "$SIGEN_DATA_DIR"/*.bin 2>/dev/null | head -1 \
  | awk '{print "  open file:", $5, "bytes", $9}'

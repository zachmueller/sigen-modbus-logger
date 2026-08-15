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

echo "=== archive ==="
printf "  files: %s   size: %s\n" \
  "$(ls "$SIGEN_DATA_DIR"/*.bin* 2>/dev/null | wc -l | tr -d ' ')" \
  "$(du -sh "$SIGEN_DATA_DIR" 2>/dev/null | cut -f1)"
ls -lt "$SIGEN_DATA_DIR"/*.bin 2>/dev/null | head -1 \
  | awk '{print "  open file:", $5, "bytes", $9}'

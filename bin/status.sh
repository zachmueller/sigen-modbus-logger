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
echo "=== archive ==="
printf "  files: %s   size: %s\n" \
  "$(ls "$SIGEN_DATA_DIR"/*.bin* 2>/dev/null | wc -l | tr -d ' ')" \
  "$(du -sh "$SIGEN_DATA_DIR" 2>/dev/null | cut -f1)"
ls -lt "$SIGEN_DATA_DIR"/*.bin 2>/dev/null | head -1 \
  | awk '{print "  open file:", $5, "bytes", $9}'

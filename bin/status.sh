#!/bin/sh
# Read-only health check. Safe to run while the logger is capturing. No sudo.
#
# With no local viewer any more, this and bin/latest.sh are the whole on-device health story:
# everything else you would want to know is in the hosted viewer, which is a question about
# whether the upload is working -- which the sync section below answers.
#
# EVERY SECTION SAYS SOMETHING, even when it has nothing to report. Each one captures its
# output and tests it, rather than piping straight to stdout: in a pipeline the exit status
# comes from the LAST stage, so `thing | grep x | sed y || echo "no thing"` never prints the
# fallback -- sed succeeds on empty input. The section then shows its header and nothing at
# all, which reads identically to "healthy and quiet" and is the exact ambiguity this script
# exists to remove.
set -e
DIR=$(cd "$(dirname "$0")/.." && pwd)
eval "$(python3 "$DIR/config.py" --sh)"
LOG=$SIGEN_LOG_DIR/sigen.log

# `say <text>` prints an indented block, or a stated absence.
say() { if [ -n "$1" ]; then printf '%s\n' "$1" | sed 's/^/  /'; else printf '  %s\n' "$2"; fi; }

# grep -c already prints 0 on no match, and exits 1 while doing so; `|| echo 0` would append
# a SECOND zero and break the line in two.
count() {
  n=$(grep -cE "$1" "$LOG" 2>/dev/null) || n=0
  echo "${n:-0}"
}

unit_state() {   # unit_state <unit>
  out=$(systemctl show -p ActiveState,SubState,Result,MainPID,NRestarts "$1" 2>/dev/null) || out=''
  say "$out" "not loaded"
  # An inactive unit whose condition did not hold is not a failure -- it is what an
  # unprovisioned host looks like. Saying which costs one line and saves an hour.
  if [ "$(systemctl show -p ConditionResult --value "$1" 2>/dev/null)" = "no" ]; then
    echo "  ConditionResult=no  <- start condition unmet; needs $SIGEN_INSTALL_DIR/config.json"
  fi
}

if ! command -v systemctl >/dev/null 2>&1; then
  echo "NOTE: no systemctl here, so every unit section below will say 'not loaded'."
  echo "      This script reports on a systemd capture host; run it on the Pi."
  echo
fi

echo "=== clock ==="
# First, because everything below is dated by it and a Pi has no RTC. An archive written
# before NTP answered is stamped from whenever fake-hwclock last wrote -- see FINDINGS 38.
if command -v timedatectl >/dev/null 2>&1; then
  say "$(timedatectl | grep -E 'Local time|Time zone|synchronized|NTP service' \
         | sed 's/^ *//')"
else
  say "" "no timedatectl; cannot say whether the clock is synchronised"
fi
# A synchronised clock RIGHT NOW is not evidence that the logger will WAIT for one at the
# next boot -- and the boot is the only time it matters. Those are two different facts and
# only the second one protects the archive, so both are reported. The installers enable this
# unit but do not fail if they cannot, so it really can be absent on a working-looking host.
if command -v systemctl >/dev/null 2>&1; then
  gate=$(systemctl is-enabled systemd-time-wait-sync.service 2>/dev/null) || gate=''
  case "$gate" in
    enabled|enabled-runtime|static|indirect)
      printf '  clock gate: systemd-time-wait-sync is %s\n' "$gate" ;;
    '')
      echo "  clock gate: systemd-time-wait-sync NOT FOUND. If this host uses chrony or"
      echo "    ntpsec instead of systemd-timesyncd, the logger's After= names a unit that"
      echo "    does not exist and orders against nothing -- substitute chrony-wait.service." ;;
    *)
      echo "  clock gate: systemd-time-wait-sync is '$gate' -- the logger's After= orders"
      echo "    against a unit that will not run, so at the next boot it can start before"
      echo "    the clock is right. See FINDINGS 38."
      echo "    sudo systemctl enable systemd-time-wait-sync" ;;
  esac
fi

echo "=== logger ($SIGEN_LOGGER_UNIT) ==="
unit_state "${SIGEN_LOGGER_UNIT}.service"

echo "=== heartbeat (last 5) ==="
say "$(grep -E "^  \[2[0-9]{3}-" "$LOG" 2>/dev/null | tail -5)" "none yet"

echo "=== warnings (gap/degraded/recovered) ==="
printf "  gap: %s   degraded: %s   recovered: %s\n" \
  "$(count '\[gap\]')" "$(count '\[degraded\]')" "$(count '\[recovered\]')"

echo "=== sync ($SIGEN_SYNC_UNIT) ==="
unit_state "${SIGEN_SYNC_UNIT}.service"
# The timer is the unit that is enabled; the service is oneshot and inactive between runs, so
# "inactive (dead)" above is the healthy state and the next-run line is the real signal.
say "$(systemctl list-timers --all "${SIGEN_SYNC_UNIT}.timer" --no-pager 2>/dev/null)" \
    "no timer; nothing will run the uploader"
say "$(tail -2 "$SIGEN_LOG_DIR/sync.log" 2>/dev/null)" "no sync.log yet"
# An EMPTY sync.err is the healthy state and worth stating, because an absent one and a clean
# one are the same silence otherwise.
err=$(tail -3 "$SIGEN_LOG_DIR/sync.err" 2>/dev/null) || err=''
say "$err" "sync.err is empty -- which is what it should be"

echo "=== archive ==="
size=$(du -sh "$SIGEN_DATA_DIR" 2>/dev/null | cut -f1)
printf "  files: %s   size: %s\n" \
  "$(ls "$SIGEN_DATA_DIR"/*.bin* 2>/dev/null | wc -l | tr -d ' ')" \
  "${size:--}"
say "$(ls -lt "$SIGEN_DATA_DIR"/*.bin 2>/dev/null | head -1 \
       | awk '{print "open file:", $5, "bytes", $9}')" \
    "no open .bin -- the logger is not writing"

echo "=== drive ==="
# The archive lives on flash and the open .bin is flushed every record -- about 28 MB a day.
# keep_days is what bounds it, and a filling drive is the symptom of that being wrong.
say "$(df -h "$SIGEN_DATA_DIR" 2>/dev/null)" "$SIGEN_DATA_DIR does not exist"

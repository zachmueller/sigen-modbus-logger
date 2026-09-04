#!/bin/sh
# Install the staleness check as a systemd service plus the timer that runs it.
#
# A third unit, separate from the logger for the reason that matters: a check inside the
# process it is checking cannot report that process being wedged. It reads the archive and
# writes only its own state file, and it never opens a connection to the inverter.
#
# Prerequisites, checked below:
#   - alert.conf present, or the check will run and have nowhere to report to
set -e
DIR=$(cd "$(dirname "$0")/../.." && pwd)
. "$DIR/deploy/systemd/_lib.sh"

require_root
require_systemd
load_config

SERVICE="${SIGEN_WATCHDOG_UNIT}.service"
TIMER="${SIGEN_WATCHDOG_UNIT}.timer"

# Not fatal, deliberately. A watchdog with no notification target still writes a state file
# and still answers bin/status.sh, which is worth having -- but silently installing something
# whose entire job is to tell you something, and which cannot, is the kind of gap this whole
# unit exists to close. So it installs and says so loudly.
if [ -f "$DIR/alert.conf" ]; then
  echo "alert.conf found; notifications will be sent"
  # Mode matters: the topic is a capability. Anyone who can read it can post to it.
  MODE=$(stat -c '%a' "$DIR/alert.conf" 2>/dev/null || stat -f '%Lp' "$DIR/alert.conf")
  case "$MODE" in
    600|400) ;;
    *) echo "WARNING: alert.conf is mode $MODE. It holds a capability, not just a name."
       echo "         chmod 600 $DIR/alert.conf" ;;
  esac
else
  echo "WARNING: no $DIR/alert.conf, so this check will detect staleness and have no way"
  echo "         to tell you about it. Create it with one line -- an ntfy topic or a full"
  echo "         URL -- and chmod 600. See alerts.py."
fi

# Prove it runs, AS THE USER THE UNIT WILL RUN AS, before installing a timer that would
# otherwise fail every fifteen minutes into a log nobody is watching yet. --dry-run so a
# host that is legitimately stale right now does not fire an alert during its own install.
echo "--- checking it runs as $SIGEN_RUN_AS_USER ---"
sudo -u "$SIGEN_RUN_AS_USER" "$SIGEN_PYTHON" "$DIR/bin/watchdog.py" --dry-run \
  | sed 's/^/  /'

make_dirs
install_unit sigen-watchdog.service.template "$SERVICE"
install_unit sigen-watchdog.timer.template "$TIMER"

systemctl daemon-reload
# The TIMER is what gets enabled, for the same reason as the uploader's: enabling the
# service would run it once per boot and never again.
systemctl enable --now "$TIMER"
sleep 5

report "$SERVICE" "$SIGEN_LOG_DIR/watchdog.log"
echo "--- timer ---"
systemctl list-timers --all "$TIMER" --no-pager | sed 's/^/  /'
condition_note "$SERVICE"

echo
echo "Installed as ${TIMER}, running ${SERVICE} every 15 minutes."
echo "It reports two states separately, because they have different causes:"
echo "  DEVICE  the logger is fine, the inverter is not answering"
echo "  LOGGER  no records are being written at all; the capture process is stuck"
echo "It notifies on a change of state, and re-nags at most once every 6 hours."
echo
echo "  check:      python3 $DIR/bin/watchdog.py --dry-run"
echo "  run now:    sudo systemctl start ${SERVICE}"
echo "  test push:  python3 $DIR/bin/watchdog.py --stale-after 1"
echo "  uninstall:  sudo $DIR/deploy/systemd/uninstall-watchdog.sh"

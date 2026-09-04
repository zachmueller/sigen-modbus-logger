#!/bin/sh
# Install the staleness check as a system LaunchDaemon on macOS.
#
# A fourth daemon, separate from the logger for the reason that matters: a check inside
# the process it is checking cannot report that process being wedged. It reads the archive
# and writes only its own state file, and it never opens a connection to the inverter.
#
# Prerequisites, checked below:
#   - alert.conf present, or the check will run and have nowhere to report to
set -e
DIR=$(cd "$(dirname "$0")/.." && pwd)

[ "$(id -u)" = 0 ] || { echo "must run with sudo: sudo $0"; exit 1; }

# config.py resolves ~ against SUDO_USER's home, not root's.
eval "$(python3 "$DIR/config.py" --sh)"
LABEL=$SIGEN_WATCHDOG_LAUNCHD_LABEL
PLIST=/Library/LaunchDaemons/${LABEL}.plist
TMP=$(mktemp -t sigen-watchdog-plist)
trap 'rm -f "$TMP"' EXIT

# config.py already refuses a duplicate label, but say which one plainly here too: this is
# where someone editing config.json finds out, and "one plist overwrote another" is not a
# failure that announces itself.
for other in "$SIGEN_LAUNCHD_LABEL" "$SIGEN_WEB_LAUNCHD_LABEL" "$SIGEN_SYNC_LAUNCHD_LABEL"; do
  [ "$LABEL" != "$other" ] || {
    echo "watchdog_launchd_label must differ from every other label ($LABEL)"; exit 1; }
done

# Not fatal, deliberately. A watchdog with no notification target still writes a state file
# and still answers bin/status.sh, which is worth having -- but silently installing
# something whose entire job is to tell you something, and which cannot, is the kind of gap
# this daemon exists to close. So it installs and says so loudly.
if [ -f "$DIR/alert.conf" ]; then
  echo "alert.conf found; notifications will be sent"
  # Mode matters: the topic is a capability. Anyone who can read it can post to it.
  MODE=$(stat -f '%Lp' "$DIR/alert.conf")
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

# Prove it runs AS THE USER THE DAEMON WILL RUN AS, before installing something that would
# otherwise fail every fifteen minutes into a log nobody is watching yet. --dry-run so a
# host that is legitimately stale right now does not fire an alert during its own install.
echo "--- checking it runs as $SIGEN_RUN_AS_USER ---"
sudo -u "$SIGEN_RUN_AS_USER" "$SIGEN_PYTHON" "$DIR/bin/watchdog.py" --dry-run \
  | sed 's/^/  /'

python3 "$DIR/config.py" --render "$DIR/deploy/watchdog.plist.template" > "$TMP"
install -d -o "$SIGEN_RUN_AS_USER" "$SIGEN_LOG_DIR"
install -m 644 -o root -g wheel "$TMP" "$PLIST"

launchctl bootout system "$PLIST" 2>/dev/null || true
launchctl bootstrap system "$PLIST"
sleep 5

echo "--- launchd state ---"
launchctl print "system/${LABEL}" 2>/dev/null \
  | grep -E "state|pid|last exit|run interval|program =" || echo "not found"
echo "--- first log lines ---"
tail -12 "$SIGEN_LOG_DIR/watchdog.log" 2>/dev/null || echo "(no log yet)"
echo
echo "Installed as ${LABEL}, running every 15 minutes."
echo "It reports two states separately, because they have different causes:"
echo "  DEVICE  the logger is fine, the inverter is not answering"
echo "  LOGGER  no records are being written at all; the capture process is stuck"
echo "It notifies on a change of state, and re-nags at most once every 6 hours."
echo
echo "Check it with:    python3 $DIR/bin/watchdog.py --dry-run"
echo "Force one push:   python3 $DIR/bin/watchdog.py --stale-after 1"
echo "Uninstall with:   sudo $DIR/deploy/uninstall-watchdog.sh"

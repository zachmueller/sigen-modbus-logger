#!/bin/sh
# Install the logger as a system LaunchDaemon on macOS.
#
# A daemon rather than an agent because the target host has no auto-login and no
# persistent console session: a user agent would not return after an unattended
# reboot. It still runs as an ordinary user -- nothing here needs privilege.
#
# Label, paths, python and the run-as user all come from config.json.
set -e
DIR=$(cd "$(dirname "$0")/.." && pwd)

[ "$(id -u)" = 0 ] || { echo "must run with sudo: sudo $0"; exit 1; }

# config.py resolves ~ against SUDO_USER's home, not root's.
eval "$(python3 "$DIR/config.py" --sh)"
LABEL=$SIGEN_LAUNCHD_LABEL
PLIST=/Library/LaunchDaemons/${LABEL}.plist
TMP=$(mktemp -t sigen-plist)
trap 'rm -f "$TMP"' EXIT

python3 "$DIR/config.py" --render "$DIR/deploy/launchd.plist.template" > "$TMP"
install -d -o "$SIGEN_RUN_AS_USER" "$SIGEN_DATA_DIR" "$SIGEN_LOG_DIR"
install -m 644 -o root -g wheel "$TMP" "$PLIST"

launchctl bootout system "$PLIST" 2>/dev/null || true
launchctl bootstrap system "$PLIST"
sleep 6

echo "--- launchd state ---"
launchctl print "system/${LABEL}" 2>/dev/null \
  | grep -E "state|pid|last exit|program =" || echo "not found"
echo "--- first log lines ---"
tail -12 "$SIGEN_LOG_DIR/sigen.log" 2>/dev/null || echo "(no log yet)"
echo
echo "Installed as ${LABEL}. It will start at every boot, no login required."
echo "Uninstall with: sudo $DIR/deploy/uninstall-daemon.sh"

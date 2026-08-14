#!/bin/sh
# Stop and remove the logger daemon. Leaves the archive untouched.
#
# The label comes from config.json, so if you change launchd_label, boot out the
# OLD label first -- otherwise the previous daemon keeps running unnoticed and two
# clients end up polling the inverter.
set -e
DIR=$(cd "$(dirname "$0")/.." && pwd)

[ "$(id -u)" = 0 ] || { echo "must run with sudo: sudo $0"; exit 1; }

eval "$(python3 "$DIR/config.py" --sh)"
LABEL=$SIGEN_LAUNCHD_LABEL
PLIST=/Library/LaunchDaemons/${LABEL}.plist

launchctl bootout system "$PLIST" 2>/dev/null || echo "(${LABEL} was not loaded)"
rm -f "$PLIST"
echo "Removed ${LABEL}. Archive in $SIGEN_DATA_DIR is untouched."

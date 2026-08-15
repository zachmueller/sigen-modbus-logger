#!/bin/sh
# Stop and remove the viewer daemon. Leaves the logger and the archive alone.
#
# The label comes from config.json, so if you change web_launchd_label, boot out
# the OLD label first -- otherwise the previous viewer keeps holding the port and
# the new one crash-loops on bind.
set -e
DIR=$(cd "$(dirname "$0")/.." && pwd)

[ "$(id -u)" = 0 ] || { echo "must run with sudo: sudo $0"; exit 1; }

eval "$(python3 "$DIR/config.py" --sh)"
LABEL=$SIGEN_WEB_LAUNCHD_LABEL
PLIST=/Library/LaunchDaemons/${LABEL}.plist

launchctl bootout system "$PLIST" 2>/dev/null || echo "(${LABEL} was not loaded)"
rm -f "$PLIST"
echo "Removed ${LABEL}. The logger and the archive in $SIGEN_DATA_DIR are untouched."

#!/bin/sh
# Stop and remove the staleness check daemon. Leaves the logger, the viewer, the uploader
# and the archive alone.
#
# The label comes from config.json, so if you change watchdog_launchd_label, boot out the
# OLD label first -- otherwise two checks run and both notify.
set -e
DIR=$(cd "$(dirname "$0")/.." && pwd)

[ "$(id -u)" = 0 ] || { echo "must run with sudo: sudo $0"; exit 1; }

eval "$(python3 "$DIR/config.py" --sh)"
LABEL=$SIGEN_WATCHDOG_LAUNCHD_LABEL
PLIST=/Library/LaunchDaemons/${LABEL}.plist

launchctl bootout system "$PLIST" 2>/dev/null || echo "(${LABEL} was not loaded)"
rm -f "$PLIST"
echo "Removed ${LABEL}. Capture and uploads continue."
echo
echo "NOTHING NOW WATCHES FOR A STALLED CAPTURE. That was the state on 2026-09-04, when an"
echo "outage ran for fourteen hours with the daemon reporting health -- so this is worth"
echo "saying rather than removing quietly. See docs/FINDINGS.md 43."
echo
echo "$SIGEN_LOG_DIR/watchdog.state is left in place, so reinstalling does not re-notify"
echo "about a state you have already been told about."

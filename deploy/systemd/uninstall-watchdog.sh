#!/bin/sh
# Stop and remove the staleness check's timer and service. Leaves the logger, the uploader
# and the archive alone.
#
# The unit name comes from config.json, so if you change watchdog_unit, disable the OLD name
# first -- otherwise two checks run and both notify.
set -e
DIR=$(cd "$(dirname "$0")/../.." && pwd)
. "$DIR/deploy/systemd/_lib.sh"

require_root
require_systemd
load_config

SERVICE="${SIGEN_WATCHDOG_UNIT}.service"
TIMER="${SIGEN_WATCHDOG_UNIT}.timer"

# The timer first: disabling the service while the timer still exists leaves something that
# will try to activate a unit that is gone.
systemctl disable --now "$TIMER" 2>/dev/null || echo "(${TIMER} was not loaded)"
systemctl stop "$SERVICE" 2>/dev/null || true
rm -f "$UNIT_DIR/$TIMER" "$UNIT_DIR/$SERVICE"
systemctl daemon-reload
systemctl reset-failed "$SERVICE" "$TIMER" 2>/dev/null || true

echo "Removed ${TIMER} and ${SERVICE}. Capture and uploads continue."
echo "NOTHING NOW WATCHES FOR A STALLED CAPTURE. That was the state on 2026-09-04, when an"
echo "outage ran for fourteen hours with the daemon reporting health -- so this is worth"
echo "saying rather than removing quietly."
echo "$SIGEN_LOG_DIR/watchdog.state is left in place, so reinstalling does not re-notify"
echo "about a state you have already been told about."

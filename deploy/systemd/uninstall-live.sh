#!/bin/sh
# Stop and remove the live sidecar. Leaves the logger, the uploader, the watchdog and the
# archive alone -- and needs no logger restart: the tap notices the socket has gone, says so
# once in sigen.log, and drops records from then on.
#
# To stop the logger offering records at all, set live_enabled false in config.json and
# restart the logger at a convenient moment. Until then the cost is one failed sendto() a tick.
set -e
DIR=$(cd "$(dirname "$0")/../.." && pwd)
. "$DIR/deploy/systemd/_lib.sh"

require_root
require_systemd
load_config

UNIT="${SIGEN_LIVE_UNIT}.service"

systemctl disable --now "$UNIT" 2>/dev/null || echo "(${UNIT} was not loaded)"
rm -f "$UNIT_DIR/$UNIT"
systemctl daemon-reload
systemctl reset-failed "$UNIT" 2>/dev/null || true

echo "Removed ${UNIT}. Capture and uploads continue; the live stream has stopped."
echo "$SIGEN_LOG_DIR/live.state and live.log are left in place."

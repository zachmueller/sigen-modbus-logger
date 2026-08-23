#!/bin/sh
# Stop and remove the uploader's timer and service. Leaves the logger, the archive and
# everything already in S3 alone.
#
# The unit name comes from config.json, so if you change sync_unit, disable the OLD name
# first -- otherwise two uploaders run and both scan the same archive.
set -e
DIR=$(cd "$(dirname "$0")/../.." && pwd)
. "$DIR/deploy/systemd/_lib.sh"

require_root
require_systemd
load_config

SERVICE="${SIGEN_SYNC_UNIT}.service"
TIMER="${SIGEN_SYNC_UNIT}.timer"

# The timer first: disabling the service while the timer still exists leaves something that
# will try to activate a unit that is gone.
systemctl disable --now "$TIMER" 2>/dev/null || echo "(${TIMER} was not loaded)"
systemctl stop "$SERVICE" 2>/dev/null || true
rm -f "$UNIT_DIR/$TIMER" "$UNIT_DIR/$SERVICE"
systemctl daemon-reload
systemctl reset-failed "$SERVICE" "$TIMER" 2>/dev/null || true

echo "Removed ${TIMER} and ${SERVICE}. Capture continues; nothing new goes offsite."
echo "The ledger at $SIGEN_DATA_DIR/.uploaded.json is left in place, so reinstalling"
echo "resumes rather than re-uploading the whole archive."

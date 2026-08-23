#!/bin/sh
# Stop and remove the logger unit. Leaves the archive untouched.
#
# The unit name comes from config.json, so if you change logger_unit, disable the OLD name
# first -- otherwise the previous unit keeps running unnoticed and two clients end up
# polling the inverter.
set -e
DIR=$(cd "$(dirname "$0")/../.." && pwd)
. "$DIR/deploy/systemd/_lib.sh"

require_root
require_systemd
load_config

UNIT="${SIGEN_LOGGER_UNIT}.service"

# SIGTERM, which log.py handles: it closes the archive and joins outstanding gzip threads
# rather than truncating the open .bin.
systemctl disable --now "$UNIT" 2>/dev/null || echo "(${UNIT} was not loaded)"
rm -f "$UNIT_DIR/$UNIT"
systemctl daemon-reload
systemctl reset-failed "$UNIT" 2>/dev/null || true

echo "Removed ${UNIT}. Archive in $SIGEN_DATA_DIR is untouched."
echo "Nothing is polling the inverter now, so nothing is being captured."

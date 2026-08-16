#!/bin/sh
# Stop and remove the uploader daemon. Leaves the logger, the viewer, the archive and
# everything already in S3 alone.
#
# The label comes from config.json, so if you change sync_launchd_label, boot out the
# OLD label first -- otherwise two uploaders run and both scan the same archive.
set -e
DIR=$(cd "$(dirname "$0")/.." && pwd)

[ "$(id -u)" = 0 ] || { echo "must run with sudo: sudo $0"; exit 1; }

eval "$(python3 "$DIR/config.py" --sh)"
LABEL=$SIGEN_SYNC_LAUNCHD_LABEL
PLIST=/Library/LaunchDaemons/${LABEL}.plist

launchctl bootout system "$PLIST" 2>/dev/null || echo "(${LABEL} was not loaded)"
rm -f "$PLIST"
echo "Removed ${LABEL}. Capture continues; nothing new will be copied offsite."
echo "The ledger at $SIGEN_DATA_DIR/.uploaded.json is left in place, so reinstalling"
echo "resumes rather than re-uploading the whole archive."

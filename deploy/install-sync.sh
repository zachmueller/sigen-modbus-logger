#!/bin/sh
# Install the offsite uploader as a system LaunchDaemon on macOS.
#
# A third daemon, separate from the logger and the viewer, so that restarting it cannot
# interrupt capture. It reads rotated archive files and writes only its own ledger.
#
# Prerequisites, checked below:
#   - boto3 installed for the Python this will run
#   - s3_bucket set and sync_enabled true in config.json
#   - a ~/.aws profile named by aws_profile, belonging to run_as_user
set -e
DIR=$(cd "$(dirname "$0")/.." && pwd)

[ "$(id -u)" = 0 ] || { echo "must run with sudo: sudo $0"; exit 1; }

# config.py resolves ~ against SUDO_USER's home, not root's.
eval "$(python3 "$DIR/config.py" --sh)"
LABEL=$SIGEN_SYNC_LAUNCHD_LABEL
PLIST=/Library/LaunchDaemons/${LABEL}.plist
TMP=$(mktemp -t sigen-sync-plist)
trap 'rm -f "$TMP"' EXIT

[ "$LABEL" != "$SIGEN_LAUNCHD_LABEL" ] || {
  echo "sync_launchd_label must differ from launchd_label ($LABEL)"; exit 1; }
[ "$LABEL" != "$SIGEN_WEB_LAUNCHD_LABEL" ] || {
  echo "sync_launchd_label must differ from web_launchd_label ($LABEL)"; exit 1; }
[ -n "$SIGEN_S3_BUCKET" ] || {
  echo "no s3_bucket in config.json; nothing to upload to"; exit 1; }
# config.py's emit_sh prints booleans through str(), so true arrives as "True". Checked
# here because sync.py exits on a false sync_enabled: without this the install succeeds,
# and the daemon then declines to upload every five minutes into a log nobody is watching.
[ "$SIGEN_SYNC_ENABLED" = "True" ] || {
  echo "sync_enabled is not true in config.json; the daemon would install and then refuse"
  echo "to upload every five minutes. Set it and re-run."; exit 1; }

# Check boto3 and the credential AS THE USER THE DAEMON WILL RUN AS. Checking as root
# would pass on root's environment and then fail every five minutes as someone else --
# and the failure would be in a log nobody is watching yet.
echo "--- checking prerequisites as $SIGEN_RUN_AS_USER ---"
sudo -u "$SIGEN_RUN_AS_USER" "$SIGEN_PYTHON" -c 'import boto3' 2>/dev/null || {
  echo "boto3 is not installed for $SIGEN_PYTHON as $SIGEN_RUN_AS_USER."
  echo "  sudo -u $SIGEN_RUN_AS_USER $SIGEN_PYTHON -m pip install --user boto3"
  exit 1; }
echo "boto3 ok"
sudo -u "$SIGEN_RUN_AS_USER" python3 "$DIR/sync.py" --status || true

python3 "$DIR/config.py" --render "$DIR/deploy/sync.plist.template" > "$TMP"
install -d -o "$SIGEN_RUN_AS_USER" "$SIGEN_LOG_DIR"
install -m 644 -o root -g wheel "$TMP" "$PLIST"

launchctl bootout system "$PLIST" 2>/dev/null || true
launchctl bootstrap system "$PLIST"
sleep 5

echo "--- launchd state ---"
launchctl print "system/${LABEL}" 2>/dev/null \
  | grep -E "state|pid|last exit|program =" || echo "not found"
echo "--- first log lines ---"
tail -12 "$SIGEN_LOG_DIR/sync.log" 2>/dev/null || echo "(no log yet)"
echo
echo "Installed as ${LABEL}, running every 5 minutes."
echo "It uploads rotated .bin.gz and manifests only -- never the open .bin, which is"
echo "still growing. The current hour therefore reaches S3 on the next rotation."
echo
echo "Check it with:   python3 $DIR/sync.py --status"
echo "Uninstall with:  sudo $DIR/deploy/uninstall-sync.sh"

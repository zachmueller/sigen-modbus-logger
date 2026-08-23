#!/bin/sh
# Install the offsite uploader as a systemd service plus the timer that runs it.
#
# A second unit, separate from the logger, so that restarting it cannot interrupt capture.
# It reads rotated archive files and writes only its own ledger.
#
# Prerequisites, checked below:
#   - boto3 installed for the Python this will run, AS the user it will run as
#   - s3_bucket set and sync_enabled true in config.json
#   - a ~/.aws profile named by aws_profile, belonging to run_as_user
set -e
DIR=$(cd "$(dirname "$0")/../.." && pwd)
. "$DIR/deploy/systemd/_lib.sh"

require_root
require_systemd
load_config

SERVICE="${SIGEN_SYNC_UNIT}.service"
TIMER="${SIGEN_SYNC_UNIT}.timer"

[ -n "$SIGEN_S3_BUCKET" ] || {
  echo "no s3_bucket in config.json; nothing to upload to" >&2; exit 1; }
# config.py's emit_sh prints booleans through str(), so true arrives as "True". Checked here
# because sync.py exits on a false sync_enabled: without this the install succeeds, and the
# timer then declines to upload every five minutes into a log nobody is watching.
[ "$SIGEN_SYNC_ENABLED" = "True" ] || {
  echo "sync_enabled is not true in config.json; the timer would install and then refuse" >&2
  echo "to upload every five minutes. Set it and re-run." >&2; exit 1; }

# Check boto3 and the credential AS THE USER THE UNIT WILL RUN AS. Checking as root would
# pass on root's environment and then fail every five minutes as someone else -- and the
# failure would be in a log nobody is watching yet.
echo "--- checking prerequisites as $SIGEN_RUN_AS_USER ---"
sudo -u "$SIGEN_RUN_AS_USER" "$SIGEN_PYTHON" -c 'import boto3' 2>/dev/null || {
  echo "boto3 is not installed for $SIGEN_PYTHON as $SIGEN_RUN_AS_USER." >&2
  echo "  sudo apt install python3-boto3" >&2
  echo "" >&2
  echo "  NOT pip: Debian refuses 'pip install --user' with" >&2
  echo "  'error: externally-managed-environment' (PEP 668), so the pip line that" >&2
  echo "  works on macOS sends you down a dead end here. See docs/FINDINGS.md 39." >&2
  exit 1; }
echo "boto3 ok"
sudo -u "$SIGEN_RUN_AS_USER" python3 "$DIR/sync.py" --status || true

make_dirs
install_unit sigen-sync.service.template "$SERVICE"
install_unit sigen-sync.timer.template "$TIMER"

systemctl daemon-reload
# The TIMER is what gets enabled. Enabling the service would run it once at every boot and
# never again, which looks like it is working for exactly one upload.
systemctl enable --now "$TIMER"
sleep 5

report "$SERVICE" "$SIGEN_LOG_DIR/sync.log"
echo "--- timer ---"
systemctl list-timers --all "$TIMER" --no-pager | sed 's/^/  /'
condition_note "$SERVICE"

echo
echo "Installed as ${TIMER}, running ${SERVICE} every 5 minutes."
echo "It uploads rotated .bin.gz and manifests only -- never the open .bin, which is still"
echo "growing. The current hour therefore reaches S3 on the next rotation."
echo
echo "  check:      python3 $DIR/sync.py --status"
echo "  run now:    sudo systemctl start ${SERVICE}"
echo "  uninstall:  sudo $DIR/deploy/systemd/uninstall-sync.sh"

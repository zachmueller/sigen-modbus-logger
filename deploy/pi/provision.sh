#!/bin/sh
# Take a Pi from "stock Raspberry Pi OS Lite with SSH" to "capturing and uploading".
#
# Run this ON YOUR WORKSTATION, not on the Pi:
#
#   deploy/pi/provision.sh pi@sigen-pi.local                  # code + secrets + bootstrap
#   deploy/pi/provision.sh --config-only pi@sigen-pi.local    # just the two secrets
#   deploy/pi/provision.sh --code-only   pi@sigen-pi.local    # just the code, then restart
#
# Idempotent. Re-running is how you deploy a change: it rsyncs, re-runs bootstrap.sh, and
# restarts the logger, which shuts down gracefully and comes back with the new code.
#
# --config-only is the step that finishes a freshly written golden image. The image carries
# the code, the units and the host tuning but neither secret, so a clone boots with both
# units skipped on their start condition and needs exactly this to come alive.
#
# WHAT COUNTS AS A SECRET HERE, and why they travel over SSH rather than in the image:
#   config.json         the inverter's LAN address, the bucket, the username
#   ~/.aws/credentials  an access key for the PutObject-only logger IAM user
# The image's boot partition is FAT32 and readable by anything it is plugged into, so
# nothing sensitive is ever written there.
set -e

CODE=1
SECRETS=1
case "$1" in
  --config-only) CODE=0; shift ;;
  --code-only)   SECRETS=0; shift ;;
  -h|--help|"")  cat >&2 <<USAGE
usage: $0 [--config-only|--code-only] USER@HOST

  e.g. $0 pi@sigen-pi.local

Pushes the working tree (minus data, logs and secrets), the local config.json, and the AWS
credential for the profile config.json names; then runs deploy/pi/bootstrap.sh over SSH.
USAGE
  exit 1 ;;
esac

TARGET=$1
[ -n "$TARGET" ] || { echo "no USER@HOST given; $0 --help" >&2; exit 1; }
case "$TARGET" in *" "*) echo "target must not contain spaces" >&2; exit 1 ;; esac

DIR=$(cd "$(dirname "$0")/../.." && pwd)
cd "$DIR"

[ -f config.json ] || {
  echo "no config.json in $DIR." >&2
  echo "  This pushes the config; it does not write one. Copy config.example.json and" >&2
  echo "  set host, run_as_user, install_dir, s3_bucket, aws_profile, sync_enabled." >&2
  exit 1; }

# Read the config the way every other script does, so the remote paths and the profile name
# are never restated here.
eval "$(python3 config.py --sh)"
REMOTE_DIR=$SIGEN_INSTALL_DIR
[ -n "$REMOTE_DIR" ] || { echo "config.py gave no install_dir" >&2; exit 1; }

echo "target      $TARGET"
echo "install_dir $REMOTE_DIR"
ssh -o BatchMode=yes -o ConnectTimeout=10 "$TARGET" true || {
  echo "cannot ssh to $TARGET with a key. Load your key, or use Raspberry Pi Imager's" >&2
  echo "  'Set authorized_keys' option when writing the drive." >&2
  exit 1; }

# --------------------------------------------------------------------- the code
if [ "$CODE" = 1 ]; then
  echo
  echo "=== code -> $TARGET:$REMOTE_DIR"
  ssh "$TARGET" "mkdir -p '$REMOTE_DIR'"
  # --delete so a file deleted here disappears there too -- otherwise a module removed from
  # the tree keeps running on the Pi, which is a very quiet way to debug the wrong code.
  # The excludes are what must never be overwritten or carried: the archive, the logs, and
  # the config, which is per-host and pushed separately below.
  rsync -a --delete --info=stats1 \
    --exclude 'config.json' \
    --exclude 'data/' \
    --exclude 'logs/' \
    --exclude '__pycache__/' \
    --exclude '.git/' \
    --exclude 'cloud/' \
    --exclude 'HANDOFF*.md' \
    --exclude '*.img' --exclude '*.img.xz' \
    ./ "$TARGET:$REMOTE_DIR/"
fi

# ------------------------------------------------------------------ the secrets
if [ "$SECRETS" = 1 ]; then
  echo
  echo "=== secrets -> $TARGET"
  # scp to a temp path then move with the right mode, so the file is never briefly
  # world-readable in the user's home.
  scp -q config.json "$TARGET:/tmp/sigen-config.json"
  ssh "$TARGET" "install -m 600 /tmp/sigen-config.json '$REMOTE_DIR/config.json' \
                 && rm -f /tmp/sigen-config.json"
  echo "  config.json (mode 600)"

  if [ -n "$SIGEN_AWS_PROFILE" ]; then
    # Only the one profile, not the whole credentials file: this Mac's ~/.aws also holds the
    # deploy credential, which can create and delete infrastructure. The Pi has no business
    # with it. The logger's key can PutObject and nothing else.
    python3 - "$SIGEN_AWS_PROFILE" > /tmp/sigen-aws-cred <<'PY'
import configparser, os, sys
profile = sys.argv[1]
path = os.path.expanduser("~/.aws/credentials")
cp = configparser.ConfigParser()
cp.read(path)
if profile not in cp:
    sys.exit(f"no [{profile}] in {path}; aws_profile in config.json names it")
out = configparser.ConfigParser()
out[profile] = {k: v for k, v in cp[profile].items()}
out.write(sys.stdout)
PY
    scp -q /tmp/sigen-aws-cred "$TARGET:/tmp/sigen-aws-cred"
    rm -f /tmp/sigen-aws-cred
    ssh "$TARGET" "install -d -m 700 ~/.aws \
                   && install -m 600 /tmp/sigen-aws-cred ~/.aws/credentials \
                   && rm -f /tmp/sigen-aws-cred"
    echo "  ~/.aws/credentials, profile [$SIGEN_AWS_PROFILE] only (mode 600)"
    echo "  region comes from config.json's s3_region, so no ~/.aws/config is needed"
  else
    echo "  no aws_profile in config.json, so no credential pushed."
    echo "  sync.py will refuse to upload, and the data will go nowhere."
  fi
fi

# ----------------------------------------------------------------- the host
echo
echo "=== bootstrap on $TARGET"
ssh -t "$TARGET" "sudo sh '$REMOTE_DIR/deploy/pi/bootstrap.sh'"

echo
echo "=== restart the logger with the new code"
# SIGTERM: log.py closes the archive and joins its gzip threads rather than truncating.
ssh "$TARGET" "sudo systemctl restart ${SIGEN_LOGGER_UNIT}.service || true"

echo
echo "=== state"
ssh "$TARGET" "sh '$REMOTE_DIR/bin/status.sh'" || true

cat <<NEXT

Next, on the Pi and only once nothing else is polling the inverter:
  ssh $TARGET '$REMOTE_DIR/bin/latest.sh'
  ssh $TARGET 'python3 $REMOTE_DIR/sync.py --status'
NEXT

#!/bin/sh
# Turn a stock Raspberry Pi OS Lite install into a capture host. Runs ON THE PI, with sudo.
#
#   sudo deploy/pi/bootstrap.sh
#
# Idempotent: safe to re-run after a `git pull`, and re-running is how you apply a change to
# any of the settings below.
#
# This is the ONE description of what a configured host looks like. deploy/pi/provision.sh
# calls it over SSH, and the golden image is captured from a host it has been run on, so
# there is no second list to keep in step with this one.
#
# It requires config.json to already be here, and it never writes one. config.json and the
# AWS credential are the two secrets, they are per-host, and provision.sh pushes them
# separately -- which is what lets a captured image be written to another drive without
# carrying either.
set -e
DIR=$(cd "$(dirname "$0")/../.." && pwd)

[ "$(id -u)" = 0 ] || { echo "must run with sudo: sudo $0" >&2; exit 1; }
command -v apt-get >/dev/null 2>&1 || {
  echo "this expects a Debian-family host (Raspberry Pi OS Lite)" >&2; exit 1; }
[ -f "$DIR/config.json" ] || {
  echo "no $DIR/config.json." >&2
  echo "  This script configures a host around a config; it does not invent one." >&2
  echo "  From your workstation:  deploy/pi/provision.sh --config-only <user>@<host>" >&2
  exit 1; }

# config.py resolves ~ against SUDO_USER's home, not root's.
eval "$(python3 "$DIR/config.py" --sh)"
[ -n "$SIGEN_RUN_AS_USER" ] || {
  echo "no run_as_user in config.json; the units would run as root" >&2; exit 1; }
id "$SIGEN_RUN_AS_USER" >/dev/null 2>&1 || {
  echo "run_as_user '$SIGEN_RUN_AS_USER' is not a user on this host" >&2; exit 1; }

# The timezone the archive is labelled in. It must match cloud.json's capture_tz, because the
# ingest Lambda runs with TZ set to that and series.tz_runs() reads localtime() -- a mismatch
# labels every axis in the wrong zone while the data itself stays correct, which is the kind
# of wrong that looks like a rendering bug for a week.
TZ_NAME=${SIGEN_TZ:-Pacific/Auckland}

# Bookworm moved the firmware config; both paths are still in the wild.
BOOTCFG=/boot/firmware/config.txt
[ -f "$BOOTCFG" ] || BOOTCFG=/boot/config.txt

say() { printf '\n=== %s\n' "$1"; }

# --------------------------------------------------------------------- packages
say "packages"
# python3-boto3 from apt, NOT pip: Debian refuses `pip install --user` with
# "error: externally-managed-environment" (PEP 668). See docs/FINDINGS.md 39.
#
# netcat-openbsd because bin/status.sh uses `nc -z`, and Pi OS Lite does not ship it.
DEBIAN_FRONTEND=noninteractive apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  git rsync python3 python3-boto3 netcat-openbsd logrotate >/dev/null
python3 -c 'import sys; assert sys.version_info >= (3, 9), sys.version'
printf '  python3 %s, boto3 %s\n' \
  "$(python3 -c 'import sys;print(".".join(map(str,sys.version_info[:3])))')" \
  "$(python3 -c 'import boto3;print(boto3.__version__)')"

# ------------------------------------------------------------------- the clock
say "clock"
# A Pi has no battery-backed RTC. Until NTP answers, the clock holds whatever fake-hwclock
# last wrote, which can be hours in the past -- and the archive is keyed on that clock, in
# the filename stamp decode.py sorts by, in its backwards-step check, and in the ingest
# Lambda's test for whether a tile span is closed. systemd-time-wait-sync is what the logger
# unit waits for, so enabling it is not optional. See docs/FINDINGS.md 38.
timedatectl set-timezone "$TZ_NAME"
systemctl enable --now systemd-timesyncd >/dev/null 2>&1 || true
systemctl enable systemd-time-wait-sync >/dev/null 2>&1 || true
timedatectl | sed 's/^/  /'

# ------------------------------------------------------------- flash and power
say "flash wear and power loss"
# The open .bin is flushed every record: about 28 MB a day, ~10 GB a year of writes to a
# thumb drive. None of the below is optional on flash.

# The journal is the largest avoidable writer on this host, and nothing here reads it --
# both units append to files under log_dir instead.
install -d /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/sigen-volatile.conf <<'CONF'
# Written by deploy/pi/bootstrap.sh. The journal lives in RAM: this host logs to files under
# log_dir, so a persistent journal would be pure write amplification on a flash drive.
[Journal]
Storage=volatile
RuntimeMaxUse=32M
CONF
systemctl restart systemd-journald
echo "  journal is volatile, capped at 32M"

# A swapfile on flash is the classic way to kill a drive, and log.py's memory is flat by
# design -- bounded buckets, a bounded drift deque -- so there is nothing to swap.
if systemctl list-unit-files dphys-swapfile.service >/dev/null 2>&1; then
  systemctl disable --now dphys-swapfile >/dev/null 2>&1 || true
  echo "  dphys-swapfile disabled"
fi

# noatime: without it, every read of the archive writes back an access time.
if grep -qE '^[^#]\S*\s+/\s+\S+\s+\S*noatime' /etc/fstab; then
  echo "  / already mounts noatime"
else
  cp /etc/fstab /etc/fstab.sigen-bak
  # Only the matched line is rebuilt; awk prints $0 verbatim for every line it does not
  # assign a field on, so the rest of the file keeps its own spacing.
  awk 'BEGIN{OFS="\t"} $1 !~ /^#/ && $2=="/" && $4 !~ /noatime/ {$4=$4",noatime"} {print}' \
    /etc/fstab.sigen-bak > /etc/fstab
  echo "  noatime added to / (backup at /etc/fstab.sigen-bak); takes effect at reboot"
fi

# The hardware watchdog, for a kernel that has stopped answering. A power cut needs nothing:
# a Pi boots by itself when power returns, which the Mac had to be told to do with
# `pmset -a autorestart 1`.
install -d /etc/systemd/system.conf.d
cat > /etc/systemd/system.conf.d/sigen-watchdog.conf <<'CONF'
# Written by deploy/pi/bootstrap.sh.
[Manager]
RuntimeWatchdogSec=15
RebootWatchdogSec=2min
CONF
grep -q '^dtparam=watchdog=on' "$BOOTCFG" || echo 'dtparam=watchdog=on' >> "$BOOTCFG"
echo "  watchdog armed in $BOOTCFG (takes effect at reboot)"

# ----------------------------------------------------------------- directories
# Before logrotate and before the units: the logger's ProtectSystem=strict names data_dir
# and log_dir in ReadWritePaths, and systemd fails to build the mount namespace for a path
# that is not there.
say "directories"
install -d -o "$SIGEN_RUN_AS_USER" -m 755 \
  "$SIGEN_INSTALL_DIR" "$SIGEN_DATA_DIR" "$SIGEN_LOG_DIR"
printf '  %s\n  %s\n  %s\n' "$SIGEN_INSTALL_DIR" "$SIGEN_DATA_DIR" "$SIGEN_LOG_DIR"

# ------------------------------------------------------------------- log files
say "log rotation"
# Both units append to these, and nothing rotated them on the Mac -- the heartbeat alone is
# about 100 KB a day. copytruncate because systemd holds the fd open: a renamed file would
# leave the unit writing to something no longer on the path anyone tails.
sed "s|@LOG_DIR@|$SIGEN_LOG_DIR|g; s|@USER@|$SIGEN_RUN_AS_USER|g" \
  "$DIR/deploy/pi/logrotate.sigen" > /etc/logrotate.d/sigen
chmod 644 /etc/logrotate.d/sigen
logrotate --debug /etc/logrotate.d/sigen >/dev/null
echo "  /etc/logrotate.d/sigen installed and parses"

# ----------------------------------------------------------------------- units
say "units"
"$DIR/deploy/systemd/install-logger.sh"
if [ "$SIGEN_SYNC_ENABLED" = "True" ] && [ -n "$SIGEN_S3_BUCKET" ]; then
  "$DIR/deploy/systemd/install-sync.sh"
else
  echo "  sync NOT installed: sync_enabled is not true, or no s3_bucket is set."
  echo "  There is no local viewer any more, so the upload is the only path the data has."
  echo "  Set both in config.json, then: sudo $DIR/deploy/systemd/install-sync.sh"
fi

say "done"
echo "noatime and the watchdog need a reboot:  sudo reboot"
echo "Then check:  $DIR/bin/status.sh"

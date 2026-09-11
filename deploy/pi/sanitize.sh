#!/bin/sh
# Strip a working capture host back to a shareable golden master. Runs ON THE PI, with sudo,
# immediately before shutting down to capture the drive.
#
#   sudo deploy/pi/sanitize.sh
#   sudo shutdown -h now      # then move the drive to your workstation
#
# THE POINT. An image captured from a host that has been running carries that host's
# identity and both of its secrets. Written to a second drive, it would be a clone that polls
# the same inverter with the same credential and answers to the same SSH host key. So the two
# secrets, the archive, and everything that identifies this particular machine come off
# first, and what is left is the part worth keeping: the OS, the packages, the host tuning,
# the units, and the code.
#
# What SURVIVES on purpose:
#   the repository checkout           the code is not a secret; this repository is public
#   the installed units               they carry ConditionPathExists, so a clone boots quiet
#   ~/.ssh/authorized_keys            a public key is not a secret, and it is how you get in
#   the host tuning from bootstrap    journald, noatime, watchdog, logrotate
#
# What a clone still needs, in one command from your workstation:
#   deploy/pi/provision.sh --config-only <user>@<host>
set -e
DIR=$(cd "$(dirname "$0")/../.." && pwd)

[ "$(id -u)" = 0 ] || { echo "must run with sudo: sudo $0" >&2; exit 1; }

if [ -f "$DIR/config.json" ]; then
  eval "$(python3 "$DIR/config.py" --sh)"
else
  echo "no config.json; assuming this host is already sanitized" >&2
  SIGEN_LOGGER_UNIT=${SIGEN_LOGGER_UNIT:-sigen-logger}
  SIGEN_SYNC_UNIT=${SIGEN_SYNC_UNIT:-sigen-sync}
fi

say() { printf '\n=== %s\n' "$1"; }

# ------------------------------------------------------------------- stop first
say "stopping capture"
# The timer before the service, and the logger with SIGTERM so it closes the archive and
# joins its gzip threads rather than leaving a truncated .bin in the image.
systemctl stop "${SIGEN_SYNC_UNIT}.timer" 2>/dev/null || true
systemctl stop "${SIGEN_SYNC_UNIT}.service" 2>/dev/null || true
systemctl stop "${SIGEN_LOGGER_UNIT}.service" 2>/dev/null || true
echo "  stopped (units stay installed and enabled -- that is the point)"

# --------------------------------------------------------------------- the data
say "archive and logs"
# BEFORE the secrets, because this is the one that needs the config to know where to look.
if [ -n "$SIGEN_DATA_DIR" ]; then
  # Including the ledger: a clone that inherited it would think a fresh archive had already
  # been uploaded and would never send the first hours it captured.
  rm -rf "${SIGEN_DATA_DIR:?}"/* "${SIGEN_DATA_DIR:?}"/.uploaded.json
  echo "  emptied $SIGEN_DATA_DIR (including .uploaded.json)"
fi
if [ -n "$SIGEN_LOG_DIR" ]; then
  rm -rf "${SIGEN_LOG_DIR:?}"/*
  echo "  emptied $SIGEN_LOG_DIR"
fi

# ------------------------------------------------------------------ the secrets
say "secrets"
rm -f "$DIR/config.json"
echo "  removed config.json"
# The notification topic. A capability, so it belongs in an image no more than a key does --
# and an image that kept it would have every clone posting to the original's alerts.
rm -f "$DIR/alert.conf"
echo "  removed alert.conf"
# Every user's, not just the run-as user's: a credential in root's home would travel too.
for home in /root /home/*; do
  [ -d "$home" ] || continue
  rm -rf "$home/.aws"
  rm -f "$home/.bash_history" "$home/.python_history" "$home/.wget-hsts"
done
echo "  removed ~/.aws and shell history for every user"
# Wifi credentials. This host runs on Ethernet -- p95 latency is a measured property here and
# wifi power save adds spikes to it -- so there should be nothing to find, but an image is
# not the place to be approximately right about a passphrase.
rm -f /etc/NetworkManager/system-connections/*
rm -f /etc/wpa_supplicant/wpa_supplicant*.conf
echo "  removed stored network connections"

# ------------------------------------------------------------------- identity
say "machine identity"
# Two hosts sharing an SSH host key means neither can be authenticated, and the second one
# to appear trips every client's known_hosts warning.
rm -f /etc/ssh/ssh_host_*
install -m 644 /dev/stdin /etc/systemd/system/sigen-firstboot.service <<'UNIT'
# Written by deploy/pi/sanitize.sh. Regenerates what a captured image cannot carry.
#
# The condition is the whole design: once the keys exist this unit is a no-op, so it needs no
# self-disabling step and stays harmless in every image made from this one.
[Unit]
Description=Regenerate SSH host keys on first boot of a written image
Before=ssh.service
ConditionPathExists=!/etc/ssh/ssh_host_ed25519_key

[Service]
Type=oneshot
ExecStart=/usr/bin/ssh-keygen -A
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
UNIT
systemctl enable sigen-firstboot.service >/dev/null 2>&1
echo "  host keys removed; sigen-firstboot.service will regenerate them"

# Empty, not absent: systemd writes a fresh one at boot for a zero-length file, and treats a
# missing one as an error on some paths.
: > /etc/machine-id
rm -f /var/lib/dbus/machine-id
echo "  machine-id cleared"

# ---------------------------------------------------------------------- tidying
say "logs and caches"
apt-get clean
find /var/log -type f -exec truncate -s0 {} +
rm -f /var/lib/systemd/random-seed
echo "  cleared /var/log, apt cache, random seed"

# --------------------------------------------------------------------- zero-fill
# This is what makes the image compress to roughly its used size rather than the drive's
# size. Deleted files leave their bytes on the medium, and xz cannot tell them from data.
say "zero-filling free space"
echo "  this takes a while and will report 'No space left on device' -- that is the"
echo "  success condition, not a failure."
dd if=/dev/zero of=/zerofill bs=1M status=none || true
sync
rm -f /zerofill
sync
df -h / | sed 's/^/  /'

cat <<'NEXT'

=== done

  sudo shutdown -h now

Then move the drive to your workstation and:
  deploy/pi/capture-image.sh <disk-identifier> sigen-pi-<date>.img.xz

This host is no longer configured. To bring THIS drive back into service, write it back or
run:  deploy/pi/provision.sh --config-only <user>@<host>
NEXT

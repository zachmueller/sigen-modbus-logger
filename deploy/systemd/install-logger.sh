#!/bin/sh
# Install the logger as a systemd system unit.
#
# A system unit rather than a --user one because the capture host has no login session: a
# user unit needs lingering enabled and is one more thing that can be absent after an
# unattended reboot. It still runs as an ordinary user -- nothing here needs privilege.
#
# Unit name, paths, python and the run-as user all come from config.json.
set -e
DIR=$(cd "$(dirname "$0")/../.." && pwd)
. "$DIR/deploy/systemd/_lib.sh"

require_root
require_systemd
load_config

UNIT="${SIGEN_LOGGER_UNIT}.service"

make_dirs
install_unit sigen-logger.service.template "$UNIT"

# The clock gate the unit declares only works if the thing it waits for is enabled. On a
# host with no RTC this is the difference between an archive stamped correctly and one
# stamped from whenever fake-hwclock last wrote -- see docs/FINDINGS.md 38.
systemctl enable systemd-time-wait-sync.service >/dev/null 2>&1 \
  || echo "  WARNING: could not enable systemd-time-wait-sync; check the clock by hand"

systemctl daemon-reload
systemctl enable --now "$UNIT"
sleep 6

report "$UNIT" "$SIGEN_LOG_DIR/sigen.log"
condition_note "$UNIT"

echo
echo "Installed as ${UNIT}. It starts at every boot, no login required."
echo "  status:     systemctl status ${UNIT}"
echo "  restart:    sudo systemctl restart ${UNIT}   # graceful; log.py joins its gzip threads"
echo "  uninstall:  sudo $DIR/deploy/systemd/uninstall-logger.sh"

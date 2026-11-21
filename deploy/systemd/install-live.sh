#!/bin/sh
# Install the live sidecar as a systemd system unit.
#
# A fourth unit, separate from the logger for the reason the whole feature rests on: nothing
# that goes wrong in the live path may reach the archive. The two share a Unix datagram
# socket and nothing else -- see livetap.py, and the comments in the unit template.
#
# Prerequisites, checked below:
#   - live_enabled true in config.json, or live.py would only ever exit saying it is off
#   - live.py --check passes AS THE RUN-AS USER: endpoint set, certificate and key readable
#     and a matching pair, paho-mqtt importable (sudo apt install python3-paho-mqtt)
#
# The logger only starts offering records once IT is restarted with live_enabled true;
# provision.sh restarts both. Until then this runs and receives nothing, which is harmless.
set -e
DIR=$(cd "$(dirname "$0")/../.." && pwd)
. "$DIR/deploy/systemd/_lib.sh"

require_root
require_systemd
load_config

UNIT="${SIGEN_LIVE_UNIT}.service"

# config.py --sh prints booleans through str(), so this is the literal "True". The same
# comparison install-sync.sh makes, for the same reason.
if [ "$SIGEN_LIVE_ENABLED" != "True" ]; then
  echo "live_enabled is not true in config.json, so the sidecar would only exit saying so." >&2
  echo "  Set it, and live_endpoint, and place the certificate; then re-run this." >&2
  exit 1
fi

echo "--- checking it can run as $SIGEN_RUN_AS_USER ---"
# Captured, not piped: in `live.py --check | sed` the status would be sed's, and a failing
# check would install anyway.
ok=1
out=$(sudo -u "$SIGEN_RUN_AS_USER" "$SIGEN_PYTHON" "$DIR/live.py" --check 2>&1) || ok=0
printf '%s\n' "$out" | sed 's/^/  /'
[ "$ok" = 1 ] || {
  echo "live.py --check failed; not installing a unit that would fail every 30 s." >&2
  exit 1; }

make_dirs
install_unit sigen-live.service.template "$UNIT"

systemctl daemon-reload
systemctl enable --now "$UNIT"
sleep 5

report "$UNIT" "$SIGEN_LOG_DIR/live.log"
condition_note "$UNIT"

echo
echo "Installed as ${UNIT}. It starts at every boot and does not depend on the logger."
echo "  socket:     $SIGEN_LIVE_SOCKET   (log.py offers each record here)"
echo "  status:     systemctl status ${UNIT};  cat $SIGEN_LOG_DIR/live.state"
echo "  stop:       sudo systemctl stop ${UNIT}   # capture is unaffected; the tap drops"
echo "  uninstall:  sudo $DIR/deploy/systemd/uninstall-live.sh"

#!/bin/sh
# Install the local web viewer as a system LaunchDaemon on macOS.
#
# Separate from the logger's daemon so that restarting the viewer -- which you will
# do whenever you change the page -- cannot interrupt capture. The viewer never
# opens a Modbus connection, so it is not a second client of the inverter.
#
# Label, port, bind address, paths and the run-as user all come from config.json.
set -e
DIR=$(cd "$(dirname "$0")/.." && pwd)

[ "$(id -u)" = 0 ] || { echo "must run with sudo: sudo $0"; exit 1; }

# config.py resolves ~ against SUDO_USER's home, not root's.
eval "$(python3 "$DIR/config.py" --sh)"
LABEL=$SIGEN_WEB_LAUNCHD_LABEL
PLIST=/Library/LaunchDaemons/${LABEL}.plist
TMP=$(mktemp -t sigen-viewer-plist)
trap 'rm -f "$TMP"' EXIT

[ "$LABEL" != "$SIGEN_LAUNCHD_LABEL" ] || {
  echo "web_launchd_label must differ from launchd_label ($LABEL)"; exit 1; }
[ "$SIGEN_WEB_PORT" -gt 1024 ] || {
  echo "web_port $SIGEN_WEB_PORT needs root to bind; pick something above 1024"; exit 1; }

python3 "$DIR/config.py" --render "$DIR/deploy/viewer.plist.template" > "$TMP"
install -d -o "$SIGEN_RUN_AS_USER" "$SIGEN_LOG_DIR"
install -m 644 -o root -g wheel "$TMP" "$PLIST"

launchctl bootout system "$PLIST" 2>/dev/null || true
launchctl bootstrap system "$PLIST"
sleep 3

echo "--- launchd state ---"
launchctl print "system/${LABEL}" 2>/dev/null \
  | grep -E "state|pid|last exit|program =" || echo "not found"
echo "--- first log lines ---"
tail -8 "$SIGEN_LOG_DIR/viewer.log" 2>/dev/null || echo "(no log yet)"
echo
case "$SIGEN_WEB_BIND" in
  127.0.0.1|localhost|::1)
    echo "Installed as ${LABEL}, bound to ${SIGEN_WEB_BIND}:${SIGEN_WEB_PORT}."
    echo "It is NOT reachable from other machines. Tunnel to it with:"
    echo "  ssh -N -L ${SIGEN_WEB_PORT}:127.0.0.1:${SIGEN_WEB_PORT} ${SIGEN_RUN_AS_USER}@$(hostname)"
    ;;
  *)
    IP=$(python3 -c 'import socket;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.connect(("192.0.2.1",9));print(s.getsockname()[0]);s.close()' 2>/dev/null)
    echo "Installed as ${LABEL}. Open it at:"
    [ -n "$IP" ] && echo "  http://${IP}:${SIGEN_WEB_PORT}/        <- an IP always works"
    echo "  http://$(hostname):${SIGEN_WEB_PORT}/   <- a .local name can hand a browser"
    echo "     an IPv6 address it has no route to (ERR_ADDRESS_UNREACHABLE)"
    echo "Anyone who can reach this host on port ${SIGEN_WEB_PORT} can read your"
    echo "telemetry. Set web_bind to 127.0.0.1 if that is not what you want."
    ;;
esac
echo "Uninstall with: sudo $DIR/deploy/uninstall-viewer.sh"

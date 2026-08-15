#!/bin/sh
# Reach the viewer through an SSH tunnel, from a machine that cannot route to the
# capture host's LAN address.
#
# Run this ON THE VIEWING MACHINE, not on the capture host:
#
#   bin/tunnel.sh youruser@yourhost.local          # then open http://localhost:8787/
#   bin/tunnel.sh youruser@yourhost.local 8787 9000  # remote 8787 -> local 9000
#
# Why this exists. A managed laptop -- corporate VPN, content filter, MDM proxy
# policy -- can refuse RFC1918 destinations outright, which a browser reports as
# ERR_ADDRESS_UNREACHABLE: the name resolved, the server is up, and the kernel or
# the filter says there is no route. The tunnel rides the SSH connection you
# already have, so the browser only ever talks to 127.0.0.1.
#
# It changes nothing on the capture host and needs no sudo. The viewer stays
# whatever web_bind says it is; this is an additional way in, not a replacement.
set -e

TARGET=$1
REMOTE_PORT=${2:-8787}
LOCAL_PORT=${3:-$REMOTE_PORT}

if [ -z "$TARGET" ]; then
  cat >&2 <<USAGE
usage: $0 USER@CAPTURE_HOST [REMOTE_PORT] [LOCAL_PORT]

  e.g. $0 youruser@yourhost.local
       $0 youruser@yourhost.local 8787 9000   # if 8787 is taken locally

Then open http://localhost:LOCAL_PORT/
USAGE
  exit 1
fi

# A local listener on the same port would make ssh's forward fail; say which,
# rather than leaving a tunnel that quietly forwards nothing.
if nc -z 127.0.0.1 "$LOCAL_PORT" 2>/dev/null; then
  echo "Local port $LOCAL_PORT is already in use:" >&2
  lsof -nP -iTCP:"$LOCAL_PORT" -sTCP:LISTEN 2>/dev/null | sed 1d | sed 's/^/  /' >&2
  echo "Pick another local port: $0 $TARGET $REMOTE_PORT 9000" >&2
  exit 1
fi

echo "Tunnelling ${TARGET}:${REMOTE_PORT} -> 127.0.0.1:${LOCAL_PORT}"
echo "  open  http://localhost:${LOCAL_PORT}/"
echo "  stop  Ctrl-C (the tunnel lives only as long as this command)"
echo

# -N: no remote command, just the forward.
# -o ExitOnForwardFailure=yes: fail loudly if the local bind is refused, instead
#    of connecting and forwarding nothing.
# -o ServerAliveInterval: notice a dropped link rather than hanging on a dead one.
exec ssh -N \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -L "${LOCAL_PORT}:127.0.0.1:${REMOTE_PORT}" \
  "$TARGET"

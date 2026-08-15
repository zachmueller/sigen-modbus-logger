#!/bin/sh
# Build two Spotlight-launchable apps: one opens the viewer through an SSH tunnel,
# one closes the tunnel again.
#
# Run this ON THE VIEWING MACHINE (a Mac):
#
#   deploy/install-launcher.sh youruser@yourhost.local
#   deploy/install-launcher.sh user@host 8787 9000 "Solar"     # ports + app name
#
# Then, from Spotlight (Cmd-Space):
#   "sigen"        -> Sigen Viewer       opens the tunnel if needed, opens the page
#   "sigen stop"   -> Sigen Viewer Stop  closes the tunnel
#
# The opener:
#   1. if ITS OWN tunnel is up (asked over an ssh control socket, not "is anything
#      on that port"), just opens the page;
#   2. if a tunnel someone else started is serving the viewer there, reuses it;
#   3. if the port is held by something that is NOT the viewer, says so;
#   4. otherwise opens the tunnel, then the page.
#
# WHY THERE IS NO IDLE AUTO-CLOSE. It was built, measured, and removed. launchd
# owns a GUI-launched app as a job and tears that job down when the app exits: a
# watcher process spawned by the app is killed within a second or two, even after
# setsid() puts it in its own session. It survived only when the app's executable
# was run from a terminal instead of from Spotlight. ControlPersist does not help
# either -- with a -N master its idle timer never starts, so the tunnel lived for
# ever. Doing it properly would mean installing a LaunchAgent on this Mac, which is
# a heavier footprint than a launcher deserves. So closing is explicit, and the
# tunnel also ends by itself when the network drops (ServerAliveInterval 30 x 3).
#
# The Stop app also registers the URL scheme sigen-stop://, which is what the
# viewer page's "Close tunnel" button uses. Only the closer gets a scheme: any web
# page you visit can trigger a registered scheme, and "close a localhost forward to
# my own machine" is a harmless thing to hand out, while "open an SSH connection to
# my home machine and pop a browser tab" is not.
#
# The apps are generated rather than committed: they are text files, and the host,
# user and ports belong to your installation. No sudo; delete the bundles to
# uninstall.
set -e

TARGET=$1
REMOTE_PORT=${2:-8787}
LOCAL_PORT=${3:-$REMOTE_PORT}
APP_NAME=${4:-Sigen Viewer}

if [ -z "$TARGET" ]; then
  cat >&2 <<USAGE
usage: $0 USER@CAPTURE_HOST [REMOTE_PORT] [LOCAL_PORT] [APP_NAME]

  e.g. $0 youruser@yourhost.local
       $0 youruser@yourhost.local 8787 9000 "Solar"

Creates ~/Applications/<APP_NAME>.app and "<APP_NAME> Stop.app", both launchable
from Spotlight.
USAGE
  exit 1
fi

case "$TARGET" in
  *" "*) echo "target must not contain spaces: $TARGET" >&2; exit 1 ;;
esac

DIR=$(cd "$(dirname "$0")/.." && pwd)
TMP=$(mktemp -d)
TMPI=$(mktemp -d)
trap 'rm -rf "$TMP" "$TMPI"' EXIT

# --------------------------------------------------------------------- the icon
# Best effort from web/favicon.svg: QuickLook renders it, sips resizes, iconutil
# packs it. A missing tool just means the default app icon, which is not worth
# failing an install over.
ICNS=""
if [ -f "$DIR/web/favicon.svg" ] && command -v iconutil >/dev/null 2>&1; then
  if qlmanage -t -s 1024 -o "$TMPI" "$DIR/web/favicon.svg" >/dev/null 2>&1 \
     && [ -f "$TMPI/favicon.svg.png" ]; then
    mkdir -p "$TMPI/icon.iconset"
    for s in 16 32 64 128 256 512; do
      sips -z $s $s "$TMPI/favicon.svg.png" \
        --out "$TMPI/icon.iconset/icon_${s}x${s}.png" >/dev/null 2>&1 || true
      d=$((s * 2))
      sips -z $d $d "$TMPI/favicon.svg.png" \
        --out "$TMPI/icon.iconset/icon_${s}x${s}@2x.png" >/dev/null 2>&1 || true
    done
    if iconutil -c icns "$TMPI/icon.iconset" -o "$TMPI/icon.icns" >/dev/null 2>&1; then
      ICNS="$TMPI/icon.icns"
      echo "  icon built from web/favicon.svg"
    fi
  fi
fi

# ------------------------------------------------------------------ bundle maker

make_app() {           # make_app <app path> <exec name> <body file> <title> [scheme]
  app=$1; execname=$2; body=$3; title=$4; scheme=$5
  urltypes=""
  if [ -n "$scheme" ]; then
    # A URL scheme is the only way a page in the browser can close a tunnel that
    # lives on THIS Mac: the viewer it is talking to runs on the capture host and
    # cannot reach this machine. A click on sigen-stop:// hands off to
    # LaunchServices, which runs the Stop app locally.
    urltypes=$(cat <<URLT
	<key>CFBundleURLTypes</key>
	<array>
		<dict>
			<key>CFBundleURLName</key>
			<string>${title}</string>
			<key>CFBundleURLSchemes</key>
			<array>
				<string>${scheme}</string>
			</array>
		</dict>
	</array>
URLT
)
  fi
  rm -rf "$app"
  mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
  # LSUIElement: these have no window and exit as soon as their work is done, so
  # they take no Dock slot and steal no focus. Spotlight indexes them anyway.
  cat > "$app/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>CFBundleName</key>
	<string>${title}</string>
	<key>CFBundleDisplayName</key>
	<string>${title}</string>
	<key>CFBundleIdentifier</key>
	<string>local.sigen.${execname}</string>
	<key>CFBundleExecutable</key>
	<string>${execname}</string>
	<key>CFBundlePackageType</key>
	<string>APPL</string>
	<key>CFBundleShortVersionString</key>
	<string>1.0</string>
	<key>CFBundleIconFile</key>
	<string>icon</string>
	<key>LSUIElement</key>
	<true/>
	<key>NSHighResolutionCapable</key>
	<true/>
${urltypes}
</dict>
</plist>
PLIST
  sed "s|@TITLE@|${title}|" "$body" > "$app/Contents/MacOS/$execname"
  chmod +x "$app/Contents/MacOS/$execname"
  [ -n "$ICNS" ] && cp "$ICNS" "$app/Contents/Resources/icon.icns"
  touch "$app"
  mdimport "$app" >/dev/null 2>&1 || true
}

# ------------------------------------------------------------- shared preamble

preamble() {
  cat <<HEAD
#!/bin/sh
# Generated by deploy/install-launcher.sh -- edit that, not this.
TARGET="${TARGET}"
REMOTE_PORT=${REMOTE_PORT}
LOCAL_PORT=${LOCAL_PORT}
HEAD
  cat <<'HEAD'
APP_NAME="@TITLE@"
SOCK="$HOME/.ssh/sigen-viewer-${LOCAL_PORT}.sock"
LOG="$HOME/Library/Logs/sigen-viewer.log"
URL="http://localhost:${LOCAL_PORT}/"

say() {
  printf '%s [%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$LOCAL_PORT" "$1" >> "$LOG"
}

note() {
  /usr/bin/osascript -e "display notification \"$1\" with title \"$APP_NAME\"" \
    >/dev/null 2>&1 || true
}

# A GUI app has no terminal, so a failure has to be shown. `giving up after` keeps
# it from waiting for someone who has walked away.
fail() {
  say "FAILED: $1"
  /usr/bin/osascript -e "display dialog \"$1\" with title \"$APP_NAME\" \
    buttons {\"OK\"} default button 1 with icon caution giving up after 20" \
    >/dev/null 2>&1 || true
  exit 1
}

listening() { /usr/bin/nc -z 127.0.0.1 "$LOCAL_PORT" 2>/dev/null; }

# Our own tunnel, asked over its control socket. This is the difference between
# "the port is busy" and "my tunnel is up": something else on that port is not a
# thing to hand a browser blindly.
mine() { /usr/bin/ssh -S "$SOCK" -O check "$TARGET" >/dev/null 2>&1; }

# Is whatever holds the port actually the viewer? Identified by its own API rather
# than by port number, so a tunnel opened by hand gets reused too.
is_viewer() {
  /usr/bin/curl -fsS -m 3 "http://127.0.0.1:${LOCAL_PORT}/api/stats" 2>/dev/null \
    | /usr/bin/grep -q '"uptime_s"'
}
HEAD
}

# ------------------------------------------------------------------ the opener

{
  preamble
  cat <<'BODY'

if mine; then
  say "reusing our tunnel; opening $URL"
  exec /usr/bin/open "$URL"
fi

if listening; then
  if is_viewer; then
    # Someone ran bin/tunnel.sh, or an earlier build of this app. It is the viewer,
    # so use it rather than fight over the port.
    say "port $LOCAL_PORT already serves the viewer (not ours); opening $URL"
    exec /usr/bin/open "$URL"
  fi
  fail "Port ${LOCAL_PORT} on this Mac is in use by something that is not the viewer.

Close whatever holds it, or rebuild the launcher on another port:
  deploy/install-launcher.sh ${TARGET} ${REMOTE_PORT} 9000"
fi

[ -S "$SOCK" ] && rm -f "$SOCK"      # stale socket from a killed master
say "opening tunnel $TARGET:$REMOTE_PORT -> 127.0.0.1:$LOCAL_PORT"
# -M -S: a control master, so this app and the Stop app can check and close it by
#        name rather than by guessing at process lists.
# -f: forks into the background, and provably outlives this launcher.
# BatchMode: never hang on a hidden password prompt -- fail visibly instead.
err=$(/usr/bin/ssh -M -S "$SOCK" -f -N \
        -o ExitOnForwardFailure=yes \
        -o BatchMode=yes \
        -o ConnectTimeout=8 \
        -o ServerAliveInterval=30 \
        -o ServerAliveCountMax=3 \
        -L "${LOCAL_PORT}:127.0.0.1:${REMOTE_PORT}" \
        "$TARGET" 2>&1) || fail "Could not reach ${TARGET}.

Are you on the same network as the capture host, and is your SSH key loaded?

ssh said: ${err:-(no output)}"

i=0
while [ "$i" -lt 40 ]; do
  listening && break
  sleep 0.25
  i=$((i + 1))
done
listening || fail "The tunnel started but nothing is listening on port ${LOCAL_PORT}.

Check ~/Library/Logs/sigen-viewer.log"

say "tunnel up; opening $URL"
exec /usr/bin/open "$URL"
BODY
} > "$TMP/open"

# ------------------------------------------------------------------ the closer

{
  preamble
  cat <<'BODY'

if mine; then
  say "closing our tunnel"
  /usr/bin/ssh -S "$SOCK" -O exit "$TARGET" >/dev/null 2>&1
  note "Tunnel closed."
  exit 0
fi

if listening; then
  # A tunnel from bin/tunnel.sh or an older build has no control socket to ask, so
  # match its forward on the command line instead.
  if /usr/bin/pkill -f "ssh.*-L ${LOCAL_PORT}:127.0.0.1:${REMOTE_PORT}" 2>/dev/null; then
    say "closed a tunnel that was not ours (no control socket)"
    note "Tunnel closed."
    exit 0
  fi
  say "port ${LOCAL_PORT} is in use, but not by a tunnel this can close"
  note "Port ${LOCAL_PORT} is in use by something else — left alone."
  exit 0
fi

say "no tunnel was open"
note "No tunnel was open."
BODY
} > "$TMP/close"

sh -n "$TMP/open"  || { echo "generated opener has a syntax error" >&2; exit 1; }
sh -n "$TMP/close" || { echo "generated closer has a syntax error" >&2; exit 1; }

APP="$HOME/Applications/${APP_NAME}.app"
STOP="$HOME/Applications/${APP_NAME} Stop.app"
echo "Building $APP"
make_app "$APP" "sigen-viewer" "$TMP/open" "${APP_NAME}"
echo "Building $STOP"
make_app "$STOP" "sigen-viewer-stop" "$TMP/close" "${APP_NAME} Stop" "sigen-stop"

# Register the scheme now, rather than whenever LaunchServices next notices the
# bundle -- otherwise the page's button does nothing until the app is launched once.
LSREG=/System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/LaunchServices.framework/Versions/A/Support/lsregister
if [ -x "$LSREG" ]; then
  "$LSREG" -f "$STOP" >/dev/null 2>&1 && echo "  registered sigen-stop:// -> $(basename "$STOP")"
fi

echo
echo "Installed:"
echo "  $APP"
echo "  $STOP"
echo
echo "Spotlight: Cmd-Space, then"
echo "  \"${APP_NAME%% *}\"        opens ${TARGET} at http://localhost:${LOCAL_PORT}/"
echo "  \"${APP_NAME%% *} stop\"   closes the tunnel"
echo
echo "The tunnel stays open until you close it, or until the network drops. It is"
echo "bound to 127.0.0.1 only. From a terminal: pkill -f sigen-viewer-${LOCAL_PORT}"
echo "Log: ~/Library/Logs/sigen-viewer.log"
echo "Uninstall: rm -rf \"$APP\" \"$STOP\""

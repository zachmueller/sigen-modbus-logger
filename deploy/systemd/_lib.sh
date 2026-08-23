# Sourced by the install/uninstall scripts in this directory. Not executable on its own.
#
# The render -> verify -> install -> enable -> prove-it sequence, in one place, because it
# is the same for every unit and the interesting part of each installer is its
# prerequisites, not this.
#
# Callers must set DIR to the repository root and run `set -e`.

UNIT_DIR=/etc/systemd/system

require_root() {
  [ "$(id -u)" = 0 ] || { echo "must run with sudo: sudo $0" >&2; exit 1; }
}

require_systemd() {
  command -v systemctl >/dev/null 2>&1 || {
    echo "no systemctl on this host; these units are for a systemd Linux" >&2; exit 1; }
}

# config.py resolves ~ against SUDO_USER's home, not root's -- so a config saying
# "~/sigen" installs a unit pointing at the directory that actually exists.
load_config() {
  eval "$(python3 "$DIR/config.py" --sh)"
  [ -n "$SIGEN_RUN_AS_USER" ] || {
    echo "no run_as_user in config.json. An empty systemd User= is not an error to" >&2
    echo "systemd: the unit would load and run as root, which is what these units" >&2
    echo "exist to avoid." >&2; exit 1; }
  id "$SIGEN_RUN_AS_USER" >/dev/null 2>&1 || {
    echo "run_as_user '$SIGEN_RUN_AS_USER' is not a user on this host" >&2; exit 1; }
}

# The archive and log directories must exist and be writable by the run-as user before the
# unit starts: the logger's ProtectSystem=strict names them in ReadWritePaths, and systemd
# fails to set up the mount namespace for a path that is not there.
make_dirs() {
  install -d -o "$SIGEN_RUN_AS_USER" -m 755 "$SIGEN_DATA_DIR" "$SIGEN_LOG_DIR"
}

# install_unit <template basename> <installed unit name>
#
# Verified BEFORE it is installed, not after: `systemd-analyze verify` on a file already in
# /etc/systemd/system reports on a unit that is live, which is too late to be a question.
#
# Its exit status is not the test. It exits non-zero for warnings that are true and
# harmless here -- most reliably "Unit ... not found" for a dependency that exists on the
# real host but not in a bare verify context. So the output is filtered and only what
# survives counts, which is also what gets printed on failure.
install_unit() {
  _template=$1; _unit=$2
  _tmp=$(mktemp -d)
  python3 "$DIR/config.py" --render "$DIR/deploy/systemd/$_template" > "$_tmp/$_unit"

  _complaints=$(systemd-analyze verify "$_tmp/$_unit" 2>&1 \
                | grep -vE 'not found|Unknown key|Ignoring' || true)
  if [ -n "$_complaints" ]; then
    echo "$_unit does not verify:" >&2
    printf '%s\n' "$_complaints" | sed 's/^/    /' >&2
    rm -rf "$_tmp"
    exit 1
  fi

  install -m 644 -o root -g root "$_tmp/$_unit" "$UNIT_DIR/$_unit"
  rm -rf "$_tmp"
  echo "  $_unit verified and installed to $UNIT_DIR"
}

# report <unit> [log file]. The properties print as NAME=value, so they label themselves.
report() {
  echo "--- systemd state ---"
  systemctl show -p ActiveState,SubState,Result,MainPID,ExecMainStatus,NRestarts \
    "$1" 2>/dev/null | sed 's/^/  /' || echo "  not found"
  if [ -n "$2" ]; then
    echo "--- first log lines ---"
    tail -12 "$2" 2>/dev/null || echo "  (no log yet)"
  fi
}

# A unit whose ConditionPathExists did not hold reads as "inactive", exactly like one that
# failed -- so say which, because on a freshly imaged host the first is expected and the
# second is not.
condition_note() {
  if [ "$(systemctl show -p ConditionResult --value "$1" 2>/dev/null)" = "no" ]; then
    echo
    echo "NOTE: ${1}'s start condition did not hold, so it is installed and enabled but"
    echo "  NOT running. That is what an unprovisioned host looks like -- the unit waits"
    echo "  for $SIGEN_INSTALL_DIR/config.json to exist. Once it does:"
    echo "    sudo systemctl start $1"
  fi
}

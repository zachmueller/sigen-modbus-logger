#!/usr/bin/env python3
"""
Configuration for the Sigenergy toolchain. Stdlib only, Python 3.9+.

Everything installation-specific lives in config.json, which is NOT committed --
inverter address, unit ids, install paths, launchd label, cadence. Copy
config.example.json to config.json and edit. Nothing here is secret in the
cryptographic sense, but a LAN address, a username and a device serial are
nobody else's business.

Resolution order, later wins:

    DEFAULTS  <  config file  <  SIGEN_* environment  <  CLI flags

The config file is looked up in this order:

    $SIGEN_CONFIG
    config.json beside this file
    ~/.config/sigen/config.json

Serving both Python and sh from one file is the point of the extra modes:

    config.py --show                 resolved config as JSON
    config.py --sh                   SIGEN_*='...' assignments, for eval in sh
    config.py --render FILE          substitute @LABEL@ etc. in a plist template

so bin/status.sh and deploy/install-daemon.sh never hardcode a path either.
"""
import json
import os
import pwd
import shlex
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLE = os.path.join(HERE, "config.example.json")

# 502 is the registered Modbus TCP port; 247 and 1 are Sigenergy's defaults for
# the plant (EMS) and the first inverter. All three are here rather than in lib.py
# so there is exactly one place a differing install is described.
DEFAULTS = {
    # --- the device -------------------------------------------------------
    "host": None,              # required: no sane default for someone else's LAN
    "port": 502,
    "timeout_s": 6.0,
    "plant_unit": 247,
    "inverter_unit": 1,

    # --- paths (~ expanded; data_dir/log_dir default under install_dir) ----
    "install_dir": None,       # defaults to the directory holding this file
    "data_dir": None,
    "log_dir": None,

    # --- deployment (macOS launchd) ---------------------------------------
    "python": "/usr/bin/python3",
    "launchd_label": "local.sigen-logger",
    "run_as_user": None,       # required only by deploy/install-daemon.sh

    # --- capture tuning ---------------------------------------------------
    "fast_period_s": 2,        # 2 = 0.5 Hz. Retunes all fast blocks together.
    "rotate_minutes": 60,
    "keep_days": 0,            # 0 = keep every archive; see README
    "degrade_after": 5,
    "degrade_probe_s": 30,
    "recycle_s": 3600,         # 0 disables the scheduled connection recycle
    "max_lag_s": 5,
    "gap_log_quiet_s": 60,
    "bucket_s": 300,

    # --- the local viewer (serve.py) --------------------------------------
    # The viewer reads the archive on disk and never opens a Modbus client, so
    # exposing it costs the inverter nothing. What it does expose is your
    # telemetry, hence web_show_identity.
    "web_port": 8787,
    "web_bind": "0.0.0.0",     # "127.0.0.1" to require an SSH tunnel
    "web_launchd_label": "local.sigen-viewer",
    "web_default_hours": 6,
    "web_show_identity": False,

    # --- privacy ----------------------------------------------------------
    # Manifests carry model/serial/firmware so an archive can be traced to the
    # unit that produced it. Set false before sharing raw archives.
    "manifest_identity": True,
}

INT_KEYS = ("port", "plant_unit", "inverter_unit", "fast_period_s",
            "rotate_minutes", "keep_days", "degrade_after", "degrade_probe_s",
            "recycle_s", "max_lag_s", "gap_log_quiet_s", "bucket_s",
            "web_port", "web_default_hours")
FLOAT_KEYS = ("timeout_s",)
BOOL_KEYS = ("manifest_identity", "web_show_identity")
PATH_KEYS = ("install_dir", "data_dir", "log_dir", "python")

# Keys exported to sh by --sh, as SIGEN_<UPPER>.
SH_KEYS = ("host", "port", "install_dir", "data_dir", "log_dir", "python",
           "launchd_label", "run_as_user", "fast_period_s",
           "web_port", "web_bind", "web_launchd_label")


class ConfigError(SystemExit):
    """Exits with a message rather than a traceback: this is always user error."""


def _home():
    """The invoking user's home, not root's.

    deploy/install-daemon.sh runs under sudo, where os.path.expanduser("~")
    resolves to /var/root -- so a config saying "~/sigen" would silently install
    a daemon pointing at a directory that does not exist.
    """
    who = os.environ.get("SUDO_USER")
    if who:
        try:
            return pwd.getpwnam(who).pw_dir
        except KeyError:
            pass
    return os.path.expanduser("~")


def expand(path):
    if not path:
        return path
    if path == "~" or path.startswith("~/"):
        path = _home() + path[1:]
    return os.path.abspath(os.path.expandvars(path))


def config_path():
    """Where the config file is, or would be. Does not require it to exist."""
    env = os.environ.get("SIGEN_CONFIG")
    if env:
        return expand(env)
    beside = os.path.join(HERE, "config.json")
    if os.path.exists(beside):
        return beside
    return os.path.join(_home(), ".config", "sigen", "config.json")


def _coerce(cfg):
    for k in INT_KEYS:
        if cfg.get(k) is not None:
            cfg[k] = int(cfg[k])
    for k in FLOAT_KEYS:
        if cfg.get(k) is not None:
            cfg[k] = float(cfg[k])
    for k in BOOL_KEYS:
        v = cfg.get(k)
        if isinstance(v, str):
            cfg[k] = v.strip().lower() in ("1", "true", "yes", "on")
    return cfg


def load(require_host=True, overrides=None):
    """Resolve the configuration. Raises ConfigError with actionable text."""
    cfg = dict(DEFAULTS)
    path = config_path()
    if os.path.exists(path):
        try:
            with open(path) as fh:
                loaded = json.load(fh)
        except ValueError as e:
            raise ConfigError(f"{path} is not valid JSON: {e}")
        # JSON has no comments, so treat _-prefixed keys as ones: config.example.json
        # uses them, and a copied-and-edited file must not then fail to load.
        loaded = {k: v for k, v in loaded.items() if not k.startswith("_")}
        unknown = sorted(set(loaded) - set(DEFAULTS))
        if unknown:
            raise ConfigError(f"{path}: unknown key(s) {', '.join(unknown)}\n"
                              f"Known keys: {', '.join(sorted(DEFAULTS))}")
        cfg.update({k: v for k, v in loaded.items() if v is not None})
    cfg["_config_path"] = path if os.path.exists(path) else None

    for key in DEFAULTS:
        env = os.environ.get("SIGEN_" + key.upper())
        if env not in (None, ""):
            cfg[key] = env
    if overrides:
        cfg.update({k: v for k, v in overrides.items() if v is not None})

    _coerce(cfg)

    cfg["install_dir"] = expand(cfg["install_dir"]) if cfg["install_dir"] else HERE
    cfg["data_dir"] = (expand(cfg["data_dir"]) if cfg["data_dir"]
                       else os.path.join(cfg["install_dir"], "data"))
    cfg["log_dir"] = (expand(cfg["log_dir"]) if cfg["log_dir"]
                      else os.path.join(cfg["install_dir"], "logs"))
    cfg["python"] = expand(cfg["python"])

    if require_host and not cfg["host"]:
        raise ConfigError(
            "no inverter host configured.\n"
            f"  Copy {os.path.basename(EXAMPLE)} to config.json and set \"host\",\n"
            "  or pass the address as the first argument, or set SIGEN_HOST.\n"
            f"  Looked for a config file at: {path}")
    if cfg["fast_period_s"] < 1:
        raise ConfigError("fast_period_s must be >= 1 (the scheduler ticks once "
                          "per second)")
    if cfg["degrade_after"] < 1:
        raise ConfigError("degrade_after must be >= 1")
    if not 1 <= cfg["web_port"] <= 65535:
        raise ConfigError(f"web_port {cfg['web_port']} is not a TCP port; "
                          "use something above 1024 so the viewer needs no root")
    if cfg["web_default_hours"] < 1:
        raise ConfigError("web_default_hours must be >= 1")
    return cfg


# ------------------------------------------------------------------------- CLI

def emit_sh(cfg):
    for key in SH_KEYS:
        v = cfg.get(key)
        print(f"SIGEN_{key.upper()}={shlex.quote('' if v is None else str(v))}")


def render(cfg, template):
    """Substitute @PLACEHOLDER@ tokens in a launchd plist template.

    Done here rather than with sed so that paths containing slashes -- i.e. all
    of them -- need no escaping.
    """
    with open(template) as fh:
        text = fh.read()
    # An empty <string></string> for UserName loads but runs the daemon as root,
    # which is exactly what the LaunchDaemon is meant to avoid. Fail loudly.
    if "@USER@" in text and not cfg["run_as_user"]:
        raise ConfigError(f'{os.path.basename(template)} needs run_as_user. Add '
                          '"run_as_user": "<your login>" to config.json.')
    subs = {
        "@LABEL@": cfg["launchd_label"],
        "@WEB_LABEL@": cfg["web_launchd_label"],
        "@PYTHON@": cfg["python"],
        "@INSTALL_DIR@": cfg["install_dir"],
        "@DATA_DIR@": cfg["data_dir"],
        "@LOG_DIR@": cfg["log_dir"],
        "@USER@": cfg["run_as_user"] or "",
    }
    for token, value in subs.items():
        text = text.replace(token, value)
    left = [t for t in subs if t in text]
    if left:
        raise ConfigError(f"{template}: unsubstituted {', '.join(left)}")
    return text


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__.strip())
        return
    mode = args[0]
    if mode == "--show":
        cfg = load(require_host=False)
        print(json.dumps(cfg, indent=1, sort_keys=True))
    elif mode == "--sh":
        emit_sh(load(require_host=False))
    elif mode == "--render":
        if len(args) < 2:
            raise ConfigError("--render needs a template path")
        sys.stdout.write(render(load(require_host=False), args[1]))
    elif mode == "--path":
        print(config_path() or "")
    else:
        raise ConfigError(f"unknown mode {mode!r}; try --show, --sh, --render, --path")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Configuration for the Sigenergy toolchain. Stdlib only, Python 3.9+.

Everything installation-specific lives in config.json, which is NOT committed --
inverter address, unit ids, install paths, systemd unit names, cadence. Copy
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
    config.py --render FILE          substitute @LOGGER_UNIT@ etc. in a unit template

so bin/status.sh and deploy/systemd/install-logger.sh never hardcode a path either.
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

    # --- deployment (systemd) ---------------------------------------------
    # The unit names are the labels of record: they name the files installed under
    # /etc/systemd/system, and they are what `systemctl` is given. Two units, one
    # per daemon, because restarting the uploader must not be able to interrupt
    # capture -- see deploy/systemd/.
    "python": "/usr/bin/python3",
    "logger_unit": "sigen-logger",
    "sync_unit": "sigen-sync",
    # Three daemons now: capture, upload, and the thing that notices capture has stopped.
    # The watchdog is separate from the logger deliberately -- a check that runs inside
    # the process it is checking cannot report that process being wedged, which is one of
    # the two states it exists to distinguish.
    "watchdog_unit": "sigen-watchdog",
    "run_as_user": None,       # required by deploy/systemd/install-logger.sh

    # --- capture tuning ---------------------------------------------------
    "fast_period_s": 2,        # 2 = 0.5 Hz. Retunes all fast blocks together.
    "rotate_minutes": 60,
    # 0 = keep every archive. Anything else deletes .bin.gz on rotation -- which is
    # the exact form sync.py uploads, so a non-zero value is only safe once uploads
    # are known to be working. Default stays 0 because losing an hour is worse than
    # filling a disk; the Pi's own config.json sets 7. See README, and FINDINGS 40.
    "keep_days": 0,
    "degrade_after": 5,
    "degrade_probe_s": 30,
    "recycle_s": 3600,         # 0 disables the scheduled connection recycle
    "max_lag_s": 5,
    "gap_log_quiet_s": 60,
    "bucket_s": 300,

    # --- offsite copy (sync.py) -------------------------------------------
    # The only path the data has off this host, since there is no local viewer: the
    # archive here is a spool, not a destination. It needs a dependency (boto3, from
    # apt on Debian -- see FINDINGS 39) and a network credential.
    #
    # Still off by default, because an archive is nobody's business until its owner
    # says so, and because a default that uploads would be a surprising one. The Pi's
    # own config.json sets it true; config.example.json shows that.
    #
    # Credentials come from a named ~/.aws profile, never from here.
    "s3_bucket": None,
    "s3_region": "us-east-1",
    "s3_prefix": "raw/",
    "aws_profile": None,
    "sync_enabled": False,

    # --- privacy ----------------------------------------------------------
    # Manifests carry model/serial/firmware so an archive can be traced to the
    # unit that produced it. Set false before sharing raw archives.
    "manifest_identity": True,
}

INT_KEYS = ("port", "plant_unit", "inverter_unit", "fast_period_s",
            "rotate_minutes", "keep_days", "degrade_after", "degrade_probe_s",
            "recycle_s", "max_lag_s", "gap_log_quiet_s", "bucket_s")
FLOAT_KEYS = ("timeout_s",)
BOOL_KEYS = ("manifest_identity", "sync_enabled")
PATH_KEYS = ("install_dir", "data_dir", "log_dir", "python")

# Keys exported to sh by --sh, as SIGEN_<UPPER>.
SH_KEYS = ("host", "port", "install_dir", "data_dir", "log_dir", "python",
           "logger_unit", "sync_unit", "watchdog_unit", "run_as_user", "fast_period_s",
           "s3_bucket", "s3_prefix", "aws_profile",
           # Exported so deploy/systemd/install-sync.sh can refuse to install a unit that
           # will only ever decline to upload: sync.py exits on a false sync_enabled, so an
           # installer blind to it succeeds and then fails every five minutes into a log
           # nobody is watching yet. s3_region for symmetry -- a wrong region is a
           # confusing failure at runtime and a cheap check here.
           "s3_region", "sync_enabled")


class ConfigError(SystemExit):
    """Exits with a message rather than a traceback: this is always user error."""


def _home():
    """The invoking user's home, not root's.

    deploy/systemd/install-logger.sh runs under sudo, where os.path.expanduser("~")
    resolves to root's home -- so a config saying "~/sigen" would silently install
    a unit pointing at a directory that does not exist.
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
    # Two units, and they may not be one. Checked here rather than in the installers
    # because both of them would have to check it, and a second uploader scanning the
    # same archive -- or a unit file overwritten by the other daemon's -- is the kind
    # of collision that only shows up as a puzzle later.
    unit_keys = ("logger_unit", "sync_unit", "watchdog_unit")
    names = [cfg[k] for k in unit_keys]
    if len(set(names)) != len(names):
        raise ConfigError(
            "logger_unit, sync_unit and watchdog_unit name three unit files and must "
            f"all differ; got {', '.join(f'{k}={cfg[k]!r}' for k in unit_keys)}")
    for key in unit_keys:
        # systemd derives the unit filename from this, so a name with a slash or a
        # suffix would install somewhere nobody looks. Refuse it here, where the
        # message can say why, rather than in `systemd-analyze verify`.
        if "/" in cfg[key] or cfg[key].endswith((".service", ".timer")):
            raise ConfigError(f"{key} {cfg[key]!r} must be a bare unit name with no "
                              "path and no .service/.timer suffix")
    if cfg["s3_prefix"] and not cfg["s3_prefix"].endswith("/"):
        # Otherwise every key would be "rawplan=..." -- a silently wrong prefix rather
        # than an error, and the ingest Lambda would never see the files.
        raise ConfigError(f"s3_prefix {cfg['s3_prefix']!r} must end with '/'")
    if cfg["sync_enabled"] and not cfg["s3_bucket"]:
        raise ConfigError("sync_enabled is true but no s3_bucket is set")
    return cfg


# ------------------------------------------------------------------------- CLI

def emit_sh(cfg):
    for key in SH_KEYS:
        v = cfg.get(key)
        print(f"SIGEN_{key.upper()}={shlex.quote('' if v is None else str(v))}")


def render(cfg, template):
    """Substitute @PLACEHOLDER@ tokens in a systemd unit template.

    Done here rather than with sed so that paths containing slashes -- i.e. all
    of them -- need no escaping.
    """
    with open(template) as fh:
        text = fh.read()
    # An empty `User=` is not an error to systemd: the unit loads and runs as root,
    # which is exactly what these units are meant to avoid. Fail loudly.
    if "@USER@" in text and not cfg["run_as_user"]:
        raise ConfigError(f'{os.path.basename(template)} needs run_as_user. Add '
                          '"run_as_user": "<your login>" to config.json.')
    subs = {
        "@LOGGER_UNIT@": cfg["logger_unit"],
        "@SYNC_UNIT@": cfg["sync_unit"],
        "@WATCHDOG_UNIT@": cfg["watchdog_unit"],
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

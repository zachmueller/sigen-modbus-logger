#!/usr/bin/env python3
"""
Push notifications for the capture daemons. Stdlib only.

One module so that exactly one place knows where the credential lives. The topic URL
is a capability -- anyone holding it can both read and post -- and this repository is
public, so it is never in a tracked file, a systemd unit or a launchd plist. It lives
in `alert.conf` beside config.json, mode 600, gitignored, and bin/leak-check.sh reads
it to add the topic to the patterns it scans for.

Fail-soft is the entire contract. `notify()` never raises and never blocks for long:
capture must not be affected by a notification service being down, slow, or
misconfigured, and the failure mode of a monitoring tool must never be an outage in
the thing it monitors.
"""
import os
import threading
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CONF_NAME = "alert.conf"

# Short, because notify() is called from inside the capture loop. This bounds the
# socket operations; MAX_BLOCK_S is what bounds the call as a whole.
TIMEOUT_S = 8.0
# Hard ceiling on how long a caller can be delayed. getaddrinfo() is not covered by a
# socket timeout and can hang well past one, which is the case this exists for.
MAX_BLOCK_S = 10.0

_warned = set()


def conf_path(install_dir=None):
    return os.path.join(install_dir or HERE, CONF_NAME)


def load_target(install_dir=None, log=None):
    """The notification URL, or None if alerting is not configured.

    Accepts a full URL or a bare ntfy topic name, so `echo my-topic > alert.conf`
    works as well as the full form. The first non-comment, non-empty line wins.
    """
    say = log or (lambda *_a, **_k: None)
    path = conf_path(install_dir)
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("http://") or line.startswith("https://"):
                    return line
                return "https://ntfy.sh/" + line.lstrip("/")
    except FileNotFoundError:
        # Said once per process, not once per attempt: an unconfigured host is a
        # legitimate state -- it is the default for anyone else running this repo --
        # and repeating it every 15 minutes would bury the log it is written to.
        if path not in _warned:
            _warned.add(path)
            say(f"  [alert] no {CONF_NAME} at {path} — notifications disabled")
        return None
    except Exception as e:
        if path not in _warned:
            _warned.add(path)
            say(f"  [alert] cannot read {path}: {type(e).__name__}: {e}")
        return None
    return None


def _post(url, title, body, priority, tags, result):
    """POST to the service, recording the outcome in `result` rather than raising.

    Runs on a worker thread. An exception here would print a traceback to stderr from
    a thread, which in sigen.err reads exactly like a capture fault -- the confusion
    FINDINGS 42 was about -- so it is captured and reported by notify() as one line.
    """
    try:
        req = urllib.request.Request(url, data=body.encode("utf-8"), method="POST")
        # ntfy takes these as headers on a plain body, which keeps the payload
        # readable in a log and needs no JSON schema agreement with the service.
        req.add_header("Title", title)
        req.add_header("Priority", priority)
        if tags:
            req.add_header("Tags", tags)
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            result["code"] = resp.status
    except Exception as e:                      # see the docstring
        result["error"] = f"{type(e).__name__}: {e}"


def notify(title, body, priority="default", tags=None, install_dir=None, log=None):
    """Send one notification. Returns True only if the service accepted it.

    Never raises. Every caller is either the capture loop or a periodic watchdog, and
    for both an alerting failure must be a logged nuisance rather than a crash --
    FINDINGS 28/42 are what a crash in an always-restart daemon looks like from the
    outside, which is health.
    """
    say = log or (lambda *_a, **_k: None)
    url = load_target(install_dir, log=log)
    if not url:
        return False

    result = {}
    t = threading.Thread(target=_post,
                         args=(url, title, body, priority, tags, result), daemon=True)
    t.start()
    t.join(MAX_BLOCK_S)
    if t.is_alive():
        # Abandoned, not killed: a daemon thread cannot outlive the process, and
        # waiting longer is what this cap exists to prevent.
        say(f"  [alert] notification did not complete within {MAX_BLOCK_S:.0f}s — "
            f"abandoned, capture continues")
        return False
    if "error" in result:
        say(f"  [alert] notification failed: {result['error']}")
        return False
    code = result.get("code")
    if code is None or not 200 <= code < 300:
        say(f"  [alert] notification rejected with HTTP {code}")
        return False
    return True

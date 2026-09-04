#!/usr/bin/env python3
"""
Notice that capture has stopped, and say so out loud. Read-only, stdlib only.

The gap this fills: on 2026-09-04 the inverter's address changed and the logger spent
fourteen hours polling a host that was not it. Nothing was broken in a way any
existing check could see -- the daemon was running, the archive was being written to,
and the heartbeat line said "logger is HEALTHY", because it was. The device was the
thing that had gone, and the only place that showed was a number nobody was looking at.

Two states are deliberately distinguished, because they have different causes and
different fixes:

  DEVICE  the logger is fine and writing empty probe records; the inverter is not
          answering. This is the 2026-09-04 failure.
  LOGGER  no record of any kind is being written; the capture process itself is
          stuck. series.latest() calls this `logger_stalled`.

No freshness arithmetic lives here. series.latest() already draws the data-freshness
vs record-freshness distinction that this whole incident turned on, and it is what the
hosted viewer reads, so a second implementation here could disagree with the page.
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import alerts          # noqa: E402  - path set above
import config          # noqa: E402
import discover        # noqa: E402  - for read_host_state only; opens no socket here
import series          # noqa: E402

STATE_NAME = "watchdog.state"
DEFAULT_STALE_AFTER_S = 1800
# A sustained outage should not be a notification every fifteen minutes. Re-nag slowly
# enough to be a reminder rather than a stream, and only while still broken.
RENAG_S = 6 * 3600


def read_state(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        # An absent or unreadable state file means "no history", which is the correct
        # starting point and not an error worth failing a health check over.
        return {}


def write_state(path, state):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(state, fh, indent=1)
            # Trailing newline: without it bin/status.sh's next line runs onto the closing
            # brace, which is how this was noticed. A text file should end with one anyway.
            fh.write("\n")
        os.replace(tmp, path)          # atomic: a truncated state file reads as "no history"
    except Exception as e:
        print(f"  cannot write {path}: {type(e).__name__}: {e}")


def polled_host(cfg):
    """The address the logger says it is using, falling back to the configured one.

    They differ exactly when rediscovery has adopted a new address, which is the case
    where naming the configured host in an alert would send you to the wrong place.
    """
    return discover.read_host_state(cfg["log_dir"]) or cfg.get("host") or "?"


def assess(cfg, stale_after_s):
    """(kind, summary, detail). kind is None when everything is fine."""
    data_dir = cfg["data_dir"]
    s = series.newest_series(data_dir)
    if s is None:
        return ("ARCHIVE", "no archive to check",
                f"No decodable series in {data_dir}. Either capture has never run here "
                f"or the manifests are missing.")

    lt = series.latest(s, [])
    host = polled_host(cfg)

    if lt.get("logger_stalled"):
        age = lt.get("record_age_s") or 0
        return ("LOGGER", f"capture stalled — no record for {age/60:.0f} min",
                f"No record of ANY kind for {age/60:.1f} min (threshold "
                f"{lt.get('stall_after_s')}s). The logger process, not the inverter, is "
                f"the thing to look at: check the daemon and logs/sigen.err.")

    if not lt.get("ok"):
        # series.latest() scans a bounded number of files (max_files=6). A long enough
        # outage leaves every one of them holding nothing but probes, so there is no
        # last-good sample to age. That is maximally stale, not an error: the
        # 2026-09-04 outage was 14 h and would have landed here.
        scanned = lt.get("files_scanned", "?")
        return ("DEVICE", "no data in the recent archive at all",
                f"The {scanned} newest archive files contain only empty probe records — "
                f"the inverter has not answered for longer than they span. "
                f"Polling {host}.")

    age = lt.get("data_age_s")
    if age is not None and age > stale_after_s:
        last = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(lt["data_ts"]))
        return ("DEVICE", f"no data for {age/60:.0f} min",
                f"Last good sample {last} ({age/60:.1f} min ago). "
                f"device_answering={lt.get('device_answering')}, "
                f"{lt.get('empties_since', '?')} consecutive empty probes. "
                f"Polling {host}. The logger is running and writing; the inverter is "
                f"not answering.")
    return (None, f"ok — data {(age or 0)/60:.1f} min old, polling {host}", "")


def main():
    args = sys.argv[1:]
    stale_after_s, dry_run = DEFAULT_STALE_AFTER_S, False
    i = 0
    while i < len(args):
        if args[i] == "--stale-after":
            stale_after_s = int(args[i + 1]); i += 2
        elif args[i] == "--dry-run":
            dry_run = True; i += 1
        elif args[i] in ("-h", "--help"):
            sys.exit(__doc__.strip())
        else:
            sys.exit(f"unknown argument {args[i]!r}. See --help.")

    cfg = config.load()
    state_path = os.path.join(cfg["log_dir"], STATE_NAME)
    prev = read_state(state_path)
    now = time.time()

    kind, summary, detail = assess(cfg, stale_after_s)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
    print(f"[{stamp}] {kind or 'OK'}: {summary}")

    was_bad = bool(prev.get("kind"))
    is_bad = kind is not None
    # Notify on a CHANGE of state, or as a slow re-nag while still broken. A watchdog
    # that pushes every run trains you to ignore it, which is the same outcome as not
    # having one.
    last_notified = prev.get("last_notified") or 0
    transition = is_bad != was_bad or (is_bad and kind != prev.get("kind"))
    renag = is_bad and (now - last_notified) >= RENAG_S
    should_notify = transition or renag

    state = {"kind": kind, "summary": summary,
             "since": prev.get("since") if (is_bad and was_bad) else now,
             "last_notified": last_notified}

    if should_notify and not dry_run:
        if is_bad:
            since = time.strftime("%Y-%m-%d %H:%M:%S",
                                  time.localtime(state["since"]))
            title = f"Sigen capture: {summary}"
            body = f"{detail}\n\nFirst seen {since}."
            sent = alerts.notify(title, body, priority="high", tags="warning",
                                 install_dir=cfg.get("install_dir"),
                                 log=lambda s: print(s))
        else:
            down_for = now - (prev.get("since") or now)
            title = "Sigen capture: recovered"
            body = (f"{summary}\n\nWas {prev.get('kind')}: {prev.get('summary')} "
                    f"for {down_for/60:.0f} min.")
            sent = alerts.notify(title, body, priority="default", tags="white_check_mark",
                                 install_dir=cfg.get("install_dir"),
                                 log=lambda s: print(s))
        if sent:
            state["last_notified"] = now
            print(f"  notified ({'renag' if renag and not transition else 'transition'})")
        else:
            # Leaving last_notified alone means the next run tries again rather than
            # treating an undelivered alert as delivered.
            print("  NOT notified — alerting failed or is not configured")
    elif should_notify:
        print("  --dry-run: would have notified")

    if not is_bad:
        state["since"] = None
    write_state(state_path, state)
    # Exit 0 even when stale: this is a reporter, and a non-zero exit would make
    # launchd and systemd log a failed job on top of the alert it just sent.
    return 0


if __name__ == "__main__":
    sys.exit(main())

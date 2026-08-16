#!/usr/bin/env python3
"""
Copy rotated archive files to S3. The ONLY module in this repository that needs boto3.

    sync.py [--once] [--dry-run] [--status] [--data-dir DIR] [--verbose]

    --once      upload what is pending and exit (what launchd runs)
    --dry-run   say what would be uploaded, touch nothing
    --status    what is uploaded, what is pending, when it last ran

Bucket, region, profile and prefix come from config.json; see config.py. Credentials come
from a named ~/.aws profile, so no secret sits in this tree.

**Why this is a separate daemon rather than a hook in log.py.** The logger has a two-second
tick to hit and an upload is a network operation of unbounded duration. log.py already
gzips off the capture thread, and adding an upload there would put a domestic broadband
stall inside the process that must not stall -- and would leave no way to catch up on a
file whose upload failed while the daemon was restarting. A separate launchd job also means
restarting the uploader cannot interrupt capture, the same reason the viewer is separate.

**Why a local ledger instead of asking S3 what is already there.** The credential this runs
under can PutObject and nothing else -- no Get, no List -- so that a key lifted off the
capture host cannot read the archive back or enumerate it. Reconciling against the
destination would need s3:ListBucket, which is exactly the permission worth not having. The
ledger is the trade: a file recording what has been uploaded, which is cheap to keep and
harmless to lose (losing it re-uploads, which is idempotent).

**What is NOT uploaded.** The open `.bin`. It grows every couple of seconds, so uploading it
would mean re-uploading a partial file repeatedly to no end -- log.py gzips it on rotation
and the `.bin.gz` is what travels. The cost is that the current hour is not offsite yet,
which is the freshness the hosted viewer is designed around anyway.

Read-only with respect to the archive: this opens files for reading and writes only its own
ledger. It never constructs a Modbus client; tests/ asserts that, and asserts that boto3
stays confined to this module.
"""
import argparse
import json
import os
import random
import sys
import time

import config

LEDGER = ".uploaded.json"
LEDGER_VERSION = 1

# Bounded, jittered backoff. Domestic broadband and a garage Mac: a failure is far more
# likely to be a router rebooting than anything that retrying fast would fix.
RETRIES = 4
BACKOFF_BASE_S = 2.0


def is_record(name):
    return name.endswith(".bin.gz")


def is_manifest(name):
    return name.endswith(".manifest.json")


def wanted(name):
    """Rotated records and manifests. Not the open .bin -- see the module docstring."""
    return is_record(name) or is_manifest(name)


def stranded(names):
    """Un-rotated `.bin` files that rotation has already moved past, oldest first.

    log.py gzips the open `.bin` when it rotates, and only the `.bin.gz` travels -- see
    wanted() and the module docstring. Nothing gzips a file afterwards, so a logger killed
    mid-hour leaves its `.bin` behind and **that hour never reaches S3 by this route**. It
    is reported rather than uploaded because uploading a file that might still be growing
    is precisely what this daemon must not do.

    What separates a stranded file from the open one is not its name or its age but the
    existence of a NEWER `.bin.gz`: that is the proof rotation has happened since, so this
    file is finished and was simply never compressed. Without that test the current hour
    would be reported as stranded every five minutes, for the whole hour, every hour.

    Stems compare as strings because the stamp is fixed-width and directly follows
    `sigen-`, so lexical order is chronological order. That keeps this function free of
    decode's regexes, and sync.py free of the import -- tests/ asserts that confinement.
    """
    gz_stems = {n[:-len(".bin.gz")] for n in names if is_record(n)}
    if not gz_stems:
        return []                      # nothing has rotated yet; the only .bin is open
    newest_gz = max(gz_stems)
    out = []
    for name in sorted(names):
        if not name.endswith(".bin") or is_record(name):
            continue
        stem = name[:-len(".bin")]
        if stem not in gz_stems and stem < newest_gz:
            out.append(name)
    return out


def plan_of(name):
    """The plan hash in a filename, or None.

    Both of log.py's naming shapes carry it, and both have exactly three dash-separated
    components: sigen-<stamp>-<hash>.bin[.gz] and sigen-<day>-<hash>.manifest.json.

    The component count is what makes this safe, and it is not decoration. A DATE is eight
    valid hex characters, so "sigen-20260814.manifest.json" -- the shape a manifest had
    before plan-hash fingerprinting -- reads as plan hash "20260814" if you only check the
    charset. It would then be filed under plan=20260814/ where nothing could ever decode
    against it, silently. Requiring three components rejects it, because the legacy names
    have two.

    A file with no hash cannot be decoded by anything downstream, so it is skipped and
    reported rather than filed under a guess. cloud/backfill.py is what repairs those.
    """
    stem = name.split(".")[0]
    parts = stem.split("-")
    if len(parts) != 3:
        return None
    tail = parts[2]
    return tail if len(tail) == 8 and all(c in "0123456789abcdef" for c in tail) else None


class Ledger:
    """What has been uploaded, by name -> (size, mtime).

    Keyed on size and mtime rather than a hash: a rotated .bin.gz never changes, and a
    manifest is rewritten only when the day rolls over. Hashing every file on every run
    would read the whole archive once a minute to learn nothing.
    """

    def __init__(self, path):
        self.path = path
        self.entries = {}
        self.last_run = None
        self.load()

    def load(self):
        try:
            with open(self.path) as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            return                     # absent or corrupt: re-uploading is idempotent
        if d.get("v") != LEDGER_VERSION:
            return
        self.entries = d.get("entries") or {}
        self.last_run = d.get("last_run")

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"v": LEDGER_VERSION, "last_run": self.last_run,
                       "entries": self.entries}, fh)
        os.replace(tmp, self.path)     # atomic: a kill mid-write cannot truncate it

    def done(self, name, size, mtime):
        e = self.entries.get(name)
        return bool(e) and e[0] == size and abs(e[1] - mtime) < 1.0

    def record(self, name, size, mtime):
        self.entries[name] = [size, mtime]


def pending(data_dir, ledger):
    """[(name, size, mtime)] still to upload, oldest first."""
    out = []
    try:
        names = os.listdir(data_dir)
    except OSError as e:
        raise SystemExit(f"cannot read {data_dir}: {e}")
    for name in sorted(names):
        if not wanted(name):
            continue
        path = os.path.join(data_dir, name)
        try:
            st = os.stat(path)
        except OSError:
            continue                   # rotated or pruned between listdir and stat
        if not ledger.done(name, st.st_size, st.st_mtime):
            out.append((name, st.st_size, st.st_mtime))
    return out


def client(cfg):
    """The S3 client. boto3 is imported HERE, not at module scope.

    So that --status and --dry-run work on a machine that has never installed it, and so
    that importing this module cannot pull boto3 into a process that does not need it.
    """
    try:
        import boto3
    except ImportError:
        raise SystemExit(
            "sync.py needs boto3:  python3 -m pip install --user boto3\n"
            "  It is the one dependency in this repository, and only this module uses it "
            "-- capture, decode and the local viewer stay stdlib-only.")
    session = boto3.Session(profile_name=cfg["aws_profile"] or None,
                            region_name=cfg["s3_region"])
    return session.client("s3")


def upload(s3, cfg, data_dir, name, verbose=False):
    plan = plan_of(name)
    if plan is None:
        print(f"  [skip] {name}: no plan hash in the name, so nothing could decode it",
              flush=True)
        return False
    key = f"{cfg['s3_prefix']}plan={plan}/{name}"
    path = os.path.join(data_dir, name)
    for attempt in range(RETRIES):
        try:
            s3.upload_file(path, cfg["s3_bucket"], key)
            if verbose:
                print(f"  [put] {key}", flush=True)
            return True
        except Exception as e:
            if attempt == RETRIES - 1:
                print(f"  [fail] {name}: {e}", flush=True)
                return False
            # Jittered, so a whole archive's worth of pending files does not retry in
            # lockstep after an outage.
            delay = BACKOFF_BASE_S * (2 ** attempt) * (0.5 + random.random())
            time.sleep(delay)
    return False


def run_once(cfg, data_dir, dry_run=False, verbose=False):
    ledger = Ledger(os.path.join(data_dir, LEDGER))
    todo = pending(data_dir, ledger)
    if not todo:
        if verbose:
            print(f"  [{time.strftime('%Y-%m-%d %H:%M:%S')}] nothing pending "
                  f"({len(ledger.entries)} already uploaded)", flush=True)
        ledger.last_run = time.time()
        if not dry_run:
            ledger.save()
        return 0, 0
    total = sum(sz for _, sz, _ in todo)
    # Mentioned only on a run that has something to do, so an idle daemon stays silent
    # rather than repeating the same line every five minutes forever. `--status` lists them.
    try:
        n_stranded = len(stranded(os.listdir(data_dir)))
    except OSError:
        n_stranded = 0
    print(f"  [{time.strftime('%Y-%m-%d %H:%M:%S')}] {len(todo)} pending, "
          f"{total/1e6:.1f} MB"
          + (f" ({n_stranded} un-rotated .bin stranded; see --status)"
             if n_stranded else ""), flush=True)
    if dry_run:
        for name, sz, _ in todo:
            plan = plan_of(name)
            print(f"  would put  {sz/1024:9.1f} KB  "
                  f"{cfg['s3_prefix']}plan={plan}/{name}" if plan
                  else f"  would SKIP {name}: no plan hash")
        return 0, len(todo)

    s3 = client(cfg)
    ok = failed = 0
    for name, sz, mtime in todo:
        if upload(s3, cfg, data_dir, name, verbose):
            ledger.record(name, sz, mtime)
            ok += 1
            # Save as we go. A crash halfway through a backlog must not re-upload the
            # half that succeeded.
            ledger.save()
        else:
            failed += 1
    ledger.last_run = time.time()
    ledger.save()
    print(f"  uploaded {ok}, failed {failed}, "
          f"{len(ledger.entries)} total in the ledger", flush=True)
    return ok, failed


def status(cfg, data_dir):
    ledger = Ledger(os.path.join(data_dir, LEDGER))
    todo = pending(data_dir, ledger)
    print(f"archive    {data_dir}")
    print(f"bucket     s3://{cfg['s3_bucket']}/{cfg['s3_prefix']}"
          f"  (profile {cfg['aws_profile'] or 'default'}, {cfg['s3_region']})")
    print(f"uploaded   {len(ledger.entries)} files")
    print(f"pending    {len(todo)} files, "
          f"{sum(sz for _, sz, _ in todo)/1e6:.1f} MB")
    if ledger.last_run:
        age = time.time() - ledger.last_run
        print(f"last run   {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ledger.last_run))}"
              f"  ({age/60:.0f} min ago)")
    else:
        print("last run   never")
    for name, sz, _ in todo[:8]:
        print(f"  pending  {sz/1024:9.1f} KB  {name}")
    if len(todo) > 8:
        print(f"  ... and {len(todo) - 8} more")

    # Reported, never counted as pending, and deliberately NOT reflected in the exit
    # status: a file already carried offsite by cloud/backfill.py is stranded here forever,
    # so failing on it would pin --status to 1 for good, and a permanently-red signal says
    # less than silence. What it needs is a human deciding whether the hour is anywhere
    # else, which is what this text asks for.
    try:
        orphans = stranded(os.listdir(data_dir))
    except OSError:
        orphans = []
    if orphans:
        print(f"stranded   {len(orphans)} un-rotated .bin file(s) rotation has moved past; "
              f"this daemon never uploads a .bin.")
        print(f"           Check they reached S3 another way (cloud/backfill.py) -- "
              f"otherwise those hours are on one disk.")
        for name in orphans[:8]:
            print(f"  stranded {name}")
        if len(orphans) > 8:
            print(f"  ... and {len(orphans) - 8} more")
    return 0 if not todo else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--data-dir")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    # require_host=False: uploading needs no inverter address, so this runs on a machine
    # that has an archive and no device.
    cfg = config.load(require_host=False,
                      overrides={"data_dir": args.data_dir})
    data_dir = cfg["data_dir"]

    if not cfg["s3_bucket"]:
        sys.exit("no s3_bucket configured. Add \"s3_bucket\" to config.json; see "
                 "config.example.json.")
    if args.status:
        sys.exit(status(cfg, data_dir))
    if not cfg["sync_enabled"] and not args.dry_run:
        sys.exit("sync_enabled is false in config.json; refusing to upload.")

    ok, failed = run_once(cfg, data_dir, args.dry_run, args.verbose)
    # Non-zero on failure so launchd's last-exit-status says something useful, and so a
    # cron-style wrapper can notice.
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()

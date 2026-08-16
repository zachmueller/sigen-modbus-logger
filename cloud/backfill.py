#!/usr/bin/env python3
"""
Get an existing local archive into S3, once. Needs boto3.

    cloud/backfill.py --data-dir ~/sigen/data [--dry-run]
    cloud/backfill.py --data-dir ./copy --repair-1hz 1hz-20260814 [--dry-run]

Run it from a workstation against a copy of the archive, or on the capture host itself.
Either way it only READS the archive: the repair described below writes nothing locally,
because the local copy is the original and the whole point of this exercise is to stop it
being the only one.

Configuration comes from cloud.json (bucket, profile, region) -- the same file the CDK app
reads. See cloud.example.json.

What it uploads:

  raw/plan=<hash>/...          every .bin/.bin.gz and every manifest, flat per plan.
                               Flat because series.discover() is non-recursive and wants
                               the manifests beside the records, which is also how they
                               sit on the capture host.
  raw/unindexed/...            files that cannot be decoded at all, preserved as bytes.
                               Nothing triggers on this prefix and no tile is built from
                               it. See below.

Tiles are NOT uploaded. The ingest Lambda builds them, and after a backfill it should be
asked to do so in one pass:

    aws lambda invoke --function-name <IngestFunctionName> \\
        --payload '{"rebuild":"<planhash>"}' --cli-binary-format raw-in-base64-out /dev/stdout

One pass rather than letting the upload's S3 events race each other over the same day
tiles -- see the note on reserved concurrency in data-stack.ts.

**Repairing a series written before plan-hash fingerprinting.** An early series can have
no hash in its filenames and `plan_hash: null` in its manifest, which makes it undecodable
by everything downstream: decode.check_plan_hash() treats a missing hash as a mismatch, and
series.discover() will not even see the files. It is recoverable, though, and the hash is
DERIVED rather than guessed -- the manifest records every field log.plan_hash() consumes
(unit, addr, count, fc, period, offset per block), so feeding its blocks through the same
function produces the hash it would have had. --repair-1hz does that, verifies the
derivation by reproducing a known-good hash from another manifest in the same archive, and
uploads the renamed files with a repaired manifest. The local files are left alone.
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import decode        # noqa: E402
import log           # noqa: E402
import series        # noqa: E402

RAW = "raw/"
UNINDEXED = RAW + "unindexed/"


def load_cloud_config():
    path = os.path.join(os.path.dirname(HERE), "cloud.json")
    if not os.path.exists(path):
        sys.exit(f"no cloud.json at {path}; copy cloud.example.json and edit it")
    with open(path) as fh:
        cfg = {k: v for k, v in json.load(fh).items() if not k.startswith("_")}
    for key in ("bucket", "region", "profile"):
        if not cfg.get(key):
            sys.exit(f"cloud.json is missing {key}")
    return cfg


def plan_hash_of_manifest(manifest):
    """The hash this manifest's block plan would have produced.

    Same spec string log.plan_hash() builds, from the same fields. The manifest records
    them all, which is what makes the repair a derivation rather than a guess.
    """
    tiers = [(b["label"], b["unit"], b["addr"], b["count"], b["fc"],
              b["period_s"], b["offset_s"]) for b in manifest["blocks"]]
    return log.plan_hash(tiers)


def verify_derivation(data_dir):
    """Reproduce a KNOWN hash before trusting a derived one.

    Finds a manifest that already records its plan_hash, recomputes it from that
    manifest's blocks, and insists they match. If this fails, the spec string has changed
    since the archive was written and every derived hash would be wrong -- so refuse
    rather than rename files to a plausible-looking hash nothing can decode against.
    """
    for name in sorted(os.listdir(data_dir)):
        if not name.endswith(".manifest.json"):
            continue
        with open(os.path.join(data_dir, name)) as fh:
            man = json.load(fh)
        recorded = man.get("plan_hash")
        if not recorded:
            continue
        derived = plan_hash_of_manifest(man)
        if derived != recorded:
            sys.exit(f"cannot trust a derived plan hash: {name} records {recorded} but "
                     f"recomputing from its own blocks gives {derived}. log.plan_hash()'s "
                     f"spec format must have changed since this archive was written.")
        return name, recorded
    return None, None


class Uploader:
    def __init__(self, cfg, dry_run=False):
        self.bucket = cfg["bucket"]
        self.dry_run = dry_run
        self.count = self.bytes = 0
        if dry_run:
            self.s3 = None
        else:
            import boto3
            self.s3 = boto3.Session(profile_name=cfg["profile"],
                                    region_name=cfg["region"]).client("s3")

    def put(self, local, key, body=None):
        size = len(body) if body is not None else os.path.getsize(local or key)
        print(f"  {'would put' if self.dry_run else 'put':>9}  {size/1024:9.1f} KB  {key}")
        if not self.dry_run:
            if body is not None:
                self.s3.put_object(Bucket=self.bucket, Key=key, Body=body)
            else:
                self.s3.upload_file(local, self.bucket, key)
        self.count += 1
        self.bytes += size


def upload_series(up, data_dir, plan_hash, paths, manifests):
    prefix = f"{RAW}plan={plan_hash}/"
    for p in manifests:
        up.put(p, prefix + os.path.basename(p))
    for p in sorted(paths):
        up.put(p, prefix + os.path.basename(p))


def repair_1hz(up, data_dir, subdir):
    """Upload an unhashed series under its derived hash, renaming as it goes."""
    src = os.path.join(data_dir, subdir)
    if not os.path.isdir(src):
        sys.exit(f"no such directory: {src}")
    names = sorted(os.listdir(src))
    mans = [n for n in names if n.endswith(".manifest.json")]
    bins = [n for n in names if n.endswith(".bin") or n.endswith(".bin.gz")]
    if len(mans) != 1:
        sys.exit(f"{src}: expected exactly one manifest, found {len(mans)}")
    with open(os.path.join(src, mans[0])) as fh:
        man = json.load(fh)
    if man.get("plan_hash"):
        sys.exit(f"{mans[0]} already records plan_hash {man['plan_hash']}; "
                 f"upload it as a normal series instead")
    derived = plan_hash_of_manifest(man)
    # The cadence was never recorded either; it is the fast blocks' period.
    fast = min(b["period_s"] for b in man["blocks"])
    repaired = dict(man, plan_hash=derived, fast_period_s=fast)
    print(f"  derived plan hash {derived}, fast_period_s {fast} "
          f"({len(bins)} record files)")

    prefix = f"{RAW}plan={derived}/"
    # One manifest, named the way log.py would have named it.
    day = mans[0].replace("sigen-", "").replace(".manifest.json", "")
    up.put(None, prefix + f"sigen-{day}-{derived}.manifest.json",
           body=json.dumps(repaired, indent=1).encode())
    for n in bins:
        stem, _, ext = n.partition(".bin")
        up.put(os.path.join(src, n), prefix + f"{stem}-{derived}.bin{ext}")
    return derived


def upload_unindexed(up, data_dir, subdirs, extra_files):
    """Bytes we cannot decode, kept anyway.

    The ask was not to lose any original capture. A file with no manifest has no format of
    record, so inventing one would be worse than admitting it is bytes -- and this prefix
    triggers nothing and produces no tiles, so nothing downstream has to pretend otherwise.
    """
    for sub in subdirs:
        d = os.path.join(data_dir, sub)
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            p = os.path.join(d, name)
            if os.path.isfile(p):
                up.put(p, f"{UNINDEXED}{sub}/{name}")
    for name in extra_files:
        p = os.path.join(data_dir, name)
        if os.path.isfile(p):
            up.put(p, f"{UNINDEXED}{name}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", required=True, help="the archive to upload (read-only)")
    ap.add_argument("--dry-run", action="store_true", help="print, upload nothing")
    ap.add_argument("--repair-1hz", metavar="SUBDIR", action="append", default=[],
                    help="a subdirectory holding a series written before plan-hash "
                         "fingerprinting; derive its hash and upload it decodable")
    ap.add_argument("--unindexed", metavar="SUBDIR", action="append", default=[],
                    help="a subdirectory to preserve as undecodable bytes")
    args = ap.parse_args()

    data_dir = os.path.expanduser(args.data_dir)
    cfg = load_cloud_config()
    up = Uploader(cfg, args.dry_run)
    print(f"archive  {data_dir}")
    print(f"bucket   s3://{cfg['bucket']}/  (profile {cfg['profile']}, {cfg['region']})")
    if args.dry_run:
        print("DRY RUN: nothing will be uploaded\n")

    ref, recorded = verify_derivation(data_dir)
    if ref:
        print(f"derivation check: recomputed {recorded} from {ref} — matches\n")

    found = series.discover(data_dir)
    if not found:
        print("no hashed series directly in the archive")
    for plan_hash, s in sorted(found.items()):
        print(f"plan {plan_hash}  ({len(s.paths)} files, {s.fast_period_s}s fast tier)")
        upload_series(up, data_dir, plan_hash, s.paths, s.manifest_paths)

    for sub in args.repair_1hz:
        print(f"\nrepairing {sub}")
        if not ref:
            sys.exit("refusing to derive a plan hash with no known-good hash in the "
                     "archive to check the derivation against")
        repair_1hz(up, data_dir, sub)

    if args.unindexed:
        print("\npreserving undecodable bytes")
        upload_unindexed(up, data_dir, args.unindexed, ["topology-baseline.json"])

    print(f"\n{up.count} objects, {up.bytes/1e6:.1f} MB"
          + (" (dry run)" if args.dry_run else ""))
    if not args.dry_run:
        print("\nNow rebuild the tiles in one pass rather than letting the upload's S3 "
              "events race:\n"
              "  aws lambda invoke --function-name <IngestFunctionName> \\\n"
              "      --payload '{\"rebuild\":\"<planhash>\"}' \\\n"
              "      --cli-binary-format raw-in-base64-out /dev/stdout")


if __name__ == "__main__":
    main()

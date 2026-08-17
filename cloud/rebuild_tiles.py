#!/usr/bin/env python3
"""
Rebuild every tile for a plan, from a workstation. Needs boto3.

    python3 cloud/rebuild_tiles.py --data-dir ~/sigen/data [--dry-run]
    python3 cloud/rebuild_tiles.py --from-s3 --plan <planhash> [--invalidate]

**Why this is not a Lambda.** It used to be: `{"rebuild": "<planhash>"}` on the ingest
function. A whole-archive rebuild costs time proportional to the archive -- measured in
Lambda at ~88 s per archive-day -- so every fixed timeout is a deadline it eventually
crosses, and it crossed 300 s in the archive's first week. Raising the limit to Lambda's
900 s ceiling bought about six days. There is no third raise, so the job moved to where
nothing is watching the clock. See docs/FINDINGS.md 37.

Nothing about the tiles changes by moving: this calls the same `ingest.run()` the Lambda
called, and uploads with the same `_upload()`, imported from the handler rather than
restated. The hourly path is untouched and stays in Lambda, where it belongs -- it is
bounded (three local dates, ~62 s) and has to run unattended.

**Two jobs, both rare.** A backfill, where one pass is safer than letting a burst of S3
events race each other over the same day tile; and a tile-format change, where every tile
has to be rewritten and no raw file is arriving to trigger it. A change that only alters
`meta.json` -- `PANELS`, `ENERGY_TILES` -- needs neither: replay one S3 event, or wait for
the next rotation, since `write_documents()` is FRESH every time.

**Rewriting S3 is not enough for a closed span.** A finished tile is published
`immutable`, so CloudFront can hold the old bytes for a year -- FINDINGS 30. Pass
`--invalidate`, or run the command this prints at the end.

Configuration comes from cloud.json (bucket, profile, region), the same file the CDK app
reads.
"""
import argparse
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, HERE)

from backfill import load_cloud_config      # noqa: E402


def load_handler(cfg, work_dir):
    """The ingest Lambda's own module, pointed at this machine.

    Imported rather than reimplemented, so the objects this writes are the objects the
    Lambda wrote: the same keys, the same Cache-Control, the same deliberate refusal to
    upload `index.json` directly. It reads its configuration from the environment at import
    time, hence the assignments first; its module-level client uses the default
    credentials, so it is re-pointed at the profile cloud.json names; and WORK is a
    module global naming /tmp, which is the one thing about it that assumed a Lambda.
    """
    os.environ["BUCKET"] = cfg["bucket"]
    os.environ.setdefault("RAW_PREFIX", "raw/")
    os.environ.setdefault("AGG_PREFIX", "agg/")
    # Not cosmetic and not spelled TZ: the handler copies this into TZ and calls tzset()
    # before importing ingest, because raw filenames and axis labels are the capture host's
    # clock. Getting it wrong here would rebuild every tile twelve hours out.
    os.environ.setdefault("CAPTURE_TZ", cfg.get("capture_tz", "UTC"))
    sys.path.insert(0, os.path.join(HERE, "lambda", "ingest"))
    import handler                                   # noqa: E402
    import boto3
    handler.s3 = boto3.Session(profile_name=cfg["profile"],
                               region_name=cfg["region"]).client("s3")
    handler.WORK = work_dir
    return handler


def distribution_id(cfg):
    """The site distribution, from the SigenSite stack rather than from a config key.

    Nothing else needs it, so it is not in cloud.json; asking CloudFormation keeps it that
    way and cannot drift.
    """
    import boto3
    cf = boto3.Session(profile_name=cfg["profile"],
                       region_name=cfg["region"]).client("cloudformation")
    try:
        # By TYPE, not by logical id: a CDK logical id embeds a hash of the construct path
        # and is not ours to depend on. There is exactly one distribution in this stack.
        res = cf.describe_stack_resources(StackName="SigenSite")["StackResources"]
        found = [r["PhysicalResourceId"] for r in res
                 if r["ResourceType"] == "AWS::CloudFront::Distribution"]
        if len(found) != 1:
            print(f"  (expected one distribution in SigenSite, found {len(found)})")
            return None
        return found[0]
    except Exception as e:                           # noqa: BLE001 -- it is advisory
        print(f"  (could not find the distribution: {e})")
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--data-dir", help="a local archive to rebuild from (read-only)")
    src.add_argument("--from-s3", action="store_true",
                     help="download the plan's raw prefix from S3 first; needs --plan")
    ap.add_argument("--plan", help="plan hash; required with --from-s3")
    ap.add_argument("--dry-run", action="store_true", help="build locally, upload nothing")
    ap.add_argument("--invalidate", action="store_true",
                    help="invalidate /agg/* afterwards, without which a closed span's old "
                         "bytes can be served for a year")
    ap.add_argument("--keep", action="store_true", help="keep the working directory")
    args = ap.parse_args()
    if args.from_s3 and not args.plan:
        ap.error("--from-s3 needs --plan: one plan at a time is what bounds the download")

    cfg = load_cloud_config()
    work = tempfile.mkdtemp(prefix="sigen-rebuild-")
    handler = load_handler(cfg, work)
    out_dir = os.path.join(work, "agg")
    print(f"bucket   s3://{cfg['bucket']}/  (profile {cfg['profile']}, {cfg['region']})")
    print(f"work     {work}")
    try:
        if args.from_s3:
            print(f"reading  s3://{cfg['bucket']}/raw/plan={args.plan}/ (every date)")
            data_dir = handler._download_plan(args.plan, want_dates=None)
        else:
            data_dir = os.path.expanduser(args.data_dir)
            print(f"reading  {data_dir}")

        # The same call the Lambda made. It writes every tile AND the documents, for every
        # plan it finds -- which is why --from-s3 takes one plan at a time.
        import ingest                                # noqa: E402
        written = ingest.run(data_dir, out_dir)
        tiles = [w for w in written if w[0] != ingest.INDEX]
        print(f"built    {len(tiles)} object(s)")
        if args.dry_run:
            for rel, cc in sorted(tiles):
                print(f"  would put  {rel}  ({cc.split(',')[0]})")
            print(f"\n{len(tiles)} object(s) (dry run)")
            return

        # _upload() skips index.json deliberately and _rewrite_index() derives it from S3
        # instead, because a rebuild sees only the plans it was given -- and an index built
        # from those alone would silently drop every other plan. That happened once: the
        # recovered 1 Hz series vanished the moment the main plan rebuilt.
        n = handler._upload(out_dir, written)
        handler._rewrite_index()
        print(f"uploaded {n} object(s) under agg/, and rewrote index.json from S3")

        dist = distribution_id(cfg) if args.invalidate else None
        if args.invalidate and dist:
            import boto3
            cf = boto3.Session(profile_name=cfg["profile"],
                               region_name=cfg["region"]).client("cloudfront")
            inv = cf.create_invalidation(
                DistributionId=dist,
                InvalidationBatch={"Paths": {"Quantity": 1, "Items": ["/agg/*"]},
                                   "CallerReference": f"rebuild-{n}-{os.getpid()}"})
            print(f"invalidated /agg/*  ({inv['Invalidation']['Id']})")
        elif not args.invalidate:
            print("\nA closed span's tile is published immutable, so CloudFront can serve "
                  "the old bytes\nfor a year (FINDINGS 30). Finish the job:\n"
                  "  aws cloudfront create-invalidation --paths '/agg/*' \\\n"
                  f"      --distribution-id <id> --profile {cfg['profile']}\n"
                  "  (or re-run this with --invalidate)")
    finally:
        if args.keep:
            print(f"kept     {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()

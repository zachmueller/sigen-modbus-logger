#!/usr/bin/env python3
"""
Give every share made before pinning its own copy of the viewer page, once. Needs boto3.

    python3 cloud/backfill_share_pages.py [--dry-run] [--version <hash>]

**Why this exists.** A share used to be served by one `site/share-view` object shared by every
share id, which loaded `/app.js` and `/style.css` from the bucket root -- all of them overwritten
in place on every deploy. `cdk deploy SigenSite` now publishes an immutable
`site/v/<version>/share-view.html` whose asset references are pinned, the share Lambda copies
that into each new share as `page.html`, and `/p/<uid>` serves the share's own page. Shares that
already existed have no such object, so this writes one for them.

They are pinned to the bundle being deployed NOW, not to the one they were made with -- that one
was never recorded, and is in any case the same code they have been rendering with all along.
The gain is from here on: they stop moving.

**Run it BEFORE the deploy that switches `/p/*` routing**, with the bundle published and the old
route still in place. In that order no link is ever broken; in the other order every existing
share 404s until this finishes. See cloud/README.md.

Configuration comes from cloud.json (bucket, profile, region) -- the same file the CDK app
reads. The published bundle is DISCOVERED by listing `site/v/`, not recomputed: the version is a
hash of the web/ sources computed in TypeScript by site-stack.ts, and a second implementation of
it here is exactly the kind of restatement that drifts.
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, HERE)

from backfill import load_cloud_config      # noqa: E402


def keys_under(s3, bucket, prefix):
    """Every key under `prefix`. Listed, not probed, for the reason the share handler's
    _keys_under() gives at length: without s3:ListBucket, S3 answers 403 for a key that does not
    exist, so a probe cannot tell absence from refusal."""
    out = set()
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            out.add(obj["Key"])
    return out


def child_names(s3, bucket, prefix):
    """The immediate sub-prefix names under `prefix`, without their trailing slash."""
    out = []
    for page in s3.get_paginator("list_objects_v2").paginate(
            Bucket=bucket, Prefix=prefix, Delimiter="/"):
        for cp in page.get("CommonPrefixes", []):
            out.append(cp["Prefix"][len(prefix):].rstrip("/"))
    return sorted(out)


def pick_version(s3, bucket, site_prefix, asked):
    """Which published bundle to pin to.

    Refuses to choose between several rather than picking the newest by timestamp: more than one
    bundle means more than one deploy has published, and which of them a link should render with
    is a decision, not a sort order.
    """
    found = child_names(s3, bucket, site_prefix + "v/")
    if not found:
        sys.exit(f"no published viewer bundle under s3://{bucket}/{site_prefix}v/ -- deploy "
                 f"SigenSite first, so there is something for a share to be pinned to")
    if asked:
        if asked not in found:
            sys.exit(f"no bundle {asked}; the bucket holds {', '.join(found)}")
        return asked
    if len(found) > 1:
        sys.exit(f"{len(found)} bundles are published ({', '.join(found)}); say which with "
                 f"--version. The newest is not necessarily the one you mean.")
    return found[0]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="print, copy nothing")
    ap.add_argument("--version", metavar="HASH",
                    help="the bundle to pin to; required only if several are published")
    args = ap.parse_args()

    cfg = load_cloud_config()
    import boto3
    s3 = boto3.Session(profile_name=cfg["profile"],
                       region_name=cfg["region"]).client("s3")
    bucket = cfg["bucket"]
    version = pick_version(s3, bucket, "site/", args.version)

    # The Lambda's own module, so the copy here is the copy it makes -- the same headers, the
    # same key names, one implementation. It reads its configuration from the environment at
    # import time, hence the assignments above the import; and its module-level client uses the
    # default credentials, so it is re-pointed at the profile cloud.json names. It is imported
    # for its two functions and not invoked, so nothing else about it runs.
    os.environ["BUCKET"] = bucket
    os.environ["SITE_PREFIX"] = "site/"
    os.environ["SHARE_PREFIX"] = "share/"
    os.environ["VIEWER_VERSION"] = version
    os.environ.setdefault("CAPTURE_TZ", cfg.get("capture_tz", "UTC"))
    sys.path.insert(0, os.path.join(HERE, "lambda", "share"))
    import handler                                    # noqa: E402
    handler.s3 = s3

    src = handler.page_key(version)
    if src not in keys_under(s3, bucket, src):
        sys.exit(f"the bundle {version} has no {src} -- it is published but incomplete")

    print(f"bucket   s3://{bucket}/  (profile {cfg['profile']}, {cfg['region']})")
    print(f"pinning  {src}")
    if args.dry_run:
        print("DRY RUN: nothing will be copied\n")

    uids = child_names(s3, bucket, handler.SHARE_PREFIX)
    if not uids:
        print("\nno existing shares, so nothing to backfill -- the routing change is safe to "
              "deploy on its own")
        return
    done = skipped = 0
    for uid in uids:
        dst = f"{handler.SHARE_PREFIX}{uid}/{handler.PAGE}"
        if dst in keys_under(s3, bucket, dst):
            print(f"  {'has a page':>11}  /p/{uid}")
            skipped += 1
            continue
        print(f"  {'would copy' if args.dry_run else 'copied':>11}  /p/{uid}  -> {dst}")
        if not args.dry_run:
            handler._copy(src, dst, content_type=handler.PAGE_TYPE)
        done += 1
    print(f"\n{done} share(s) pinned to {version}, {skipped} already had a page"
          + (" (dry run)" if args.dry_run else ""))


if __name__ == "__main__":
    main()

#!/bin/sh
# Does anything installation-specific appear in a TRACKED file, now or in history?
#
# Run before every push. This repository is public, and everything that identifies the
# installation -- the AWS account, the domain, the Cognito ids, the Google client id and
# other people's email addresses -- lives in gitignored files (cloud.json,
# cloud/.google-secret, cdk.out/). This is the check that they stayed there.
#
#   bin/leak-check.sh                 patterns from cloud.json plus the fixed list
#   bin/leak-check.sh <extra>...      additional patterns
#
# Exit 0 means clean. Exit 1 means a pattern was found and the push must not happen.
#
# ---------------------------------------------------------------------------------------
# WHY THIS IS A SCRIPT AND NOT A SNIPPET IN A HANDOFF NOTE
#
# It used to be pasted between sessions, and the pasted version was broken:
#
#     git grep -nI -E "$pat" $BASE..HEAD -- 2>/dev/null | wc -l
#
# `git grep` searches TREES, not commit ranges. Given `48070e3..HEAD` it fails with
# "fatal: unable to resolve revision", which `2>/dev/null` swallowed, so `wc -l` counted an
# empty stream and every pattern reported zero. The check could not fail. Several sessions
# recorded "checked, came back zero" on the strength of it.
#
# So: no stderr redirection over anything that can fail, a self-test that proves the search
# can find something before trusting that it found nothing, and a non-zero exit that a
# pre-push hook can act on.
# ---------------------------------------------------------------------------------------
set -eu
DIR=$(cd "$(dirname "$0")/.." && pwd)
cd "$DIR"

# Fixed patterns: credential shapes, and anything a leak would look like regardless of who
# deploys this. Kept literal on purpose -- a regex that matches too much trains people to
# ignore the output.
#
# Space-separated, so no pattern may CONTAIN a space: the loop below word-splits this, and
# "BEGIN[ _]PRIVATE" split into three broken regexes that git rejected. Use `.` where a
# space is wanted.
PATTERNS='GOCSPX AKIA ASIA aws_secret_access_key BEGIN.PRIVATE.KEY xoxb-'

# Installation-specific patterns, read out of the gitignored config so this script itself
# names nothing. Without cloud.json only the fixed patterns above are checked.
if [ -f cloud.json ]; then
  EXTRA=$(python3 - <<'PY'
import json, re
try:
    d = json.load(open("cloud.json"))
except Exception:
    raise SystemExit(0)
out = []
for key in ("account_id", "bucket", "domain", "auth_domain_prefix",
            "certificate_arn", "google_client_id",
            "cognito_user_pool_id", "cognito_client_id"):
    v = d.get(key)
    if not isinstance(v, str) or not v.strip():
        continue
    v = v.strip()
    # The distinctive part, not the whole value: an ARN mostly consists of words that
    # legitimately appear in the tree ("arn", "aws", "acm", "certificate").
    if key == "certificate_arn":
        v = v.rsplit("/", 1)[-1].split("-")[0]
    elif key == "google_client_id":
        v = v.split("-")[0]
    elif key == "domain":
        # The registrable parent too: a leak of "muxu.nz" matters as much as the subdomain.
        parts = v.split(".")
        if len(parts) > 2:
            out.append(".".join(parts[-2:]))
    out.append(v)
# Email addresses, and their local parts -- "someone@gmail.com" and "someone" are both
# identifying, and the local part is what would survive being pasted into a comment.
for e in d.get("allowed_emails") or []:
    if isinstance(e, str) and "@" in e:
        out.append(e.strip())
        out.append(e.split("@")[0].strip())
seen, uniq = set(), []
for p in out:
    if p and p not in seen and len(p) >= 4:
        seen.add(p)
        uniq.append(p)
print(" ".join(uniq))
PY
)
  PATTERNS="$PATTERNS $EXTRA"
fi

# Every pattern the caller added.
for p in "$@"; do PATTERNS="$PATTERNS $p"; done

# --- self-test -------------------------------------------------------------------------
# Prove the two searches can find something before believing they found nothing. The old
# snippet's whole failure was reporting zero because it was broken, not because the tree was
# clean, and a check that cannot fail is worse than no check -- it is a false assurance.
CANARY='sigen-modbus-logger'
if [ -z "$(git grep -lI -E "$CANARY" HEAD -- 2>/dev/null)" ]; then
  echo "leak-check: SELF-TEST FAILED -- 'git grep <pattern> HEAD' found nothing for a"
  echo "  string known to be in the tree, so a clean result here would mean nothing."
  exit 2
fi
if [ -z "$(git log --all -S"$CANARY" --pickaxe-regex --oneline -- 2>/dev/null)" ]; then
  echo "leak-check: SELF-TEST FAILED -- the history search found nothing for a string"
  echo "  known to be in the history."
  exit 2
fi

# --- the check -------------------------------------------------------------------------
# Run one search and DISTINGUISH "no match" from "the search broke".
#
# This is the crux. git grep exits 0 for a match, 1 for no match, and 2+ for an error such
# as an invalid regex. Collapsing 2 into "0 matches" is precisely how the old snippet
# reported clean while doing nothing, so an error here aborts the whole script rather than
# counting as a pass.
#
# Three places, because they answer three different questions:
#
#   staged  -- what THIS commit would add. The only column that can stop a leak before it
#              exists; HEAD and history are both too late by then. The first version of this
#              script checked only the other two, which made it useless at the moment of
#              committing -- the same shape of mistake as the snippet it replaced.
#   tree    -- what is in the published files right now.
#   history -- what was ever committed. A hit here means the value must be treated as public
#              and rotated, even if it has since been deleted.
# This file lists credential SHAPES ("GOCSPX", "AKIA") as its patterns, so once it is tracked
# it matches itself on every one of them -- three columns of false positives, which is how a
# check gets ignored. It is excluded from its own search, and only it.
#
# The gap that leaves is real but narrow: a secret pasted into THIS file would not be found by
# THIS file. It is the one path nobody has a reason to put a value in, and the alternative --
# obfuscating the patterns so they do not match themselves -- makes the list unreadable, which
# is worse for a file whose job is to be checked by eye.
SELF=':(exclude)bin/leak-check.sh'

search() {   # search <kind> <pattern> -> prints a count, or exits 2
  kind=$1; pat=$2; out=''; rc=0
  if [ "$kind" = staged ]; then
    # --cached BEFORE the pattern. After it, git parses it as a revision and dies with
    # "unable to resolve revision: --cached" -- which this function surfaced rather than
    # swallowed, which is the entire reason it refuses to redirect stderr.
    out=$(git grep --cached -nI -E "$pat" -- "$SELF" 2>&1) || rc=$?
  elif [ "$kind" = tree ]; then
    out=$(git grep -nI -E "$pat" HEAD -- "$SELF" 2>&1) || rc=$?
  else
    out=$(git log --all -S"$pat" --pickaxe-regex --oneline -- "$SELF" 2>&1) || rc=$?
  fi
  if [ "$rc" -gt 1 ]; then
    echo "leak-check: the $kind search FAILED for pattern '$pat' (exit $rc):" >&2
    echo "$out" | sed 's/^/    /' >&2
    echo "  A failed search is not a clean result. Fix the pattern and re-run." >&2
    exit 2
  fi
  # git log exits 0 with empty output when there is no match, so count lines, not status.
  if [ -z "$out" ]; then echo 0; else printf '%s\n' "$out" | wc -l | tr -d ' '; fi
}

printf '%-34s %8s %8s %8s\n' 'pattern' 'staged' 'tree' 'history'
found=0
for pat in $PATTERNS; do
  staged=$(search staged "$pat")
  tree=$(search tree "$pat")
  hist=$(search history "$pat")
  flag=''
  if [ "$staged" != "0" ] || [ "$tree" != "0" ] || [ "$hist" != "0" ]; then
    flag='  <-- LEAK'; found=1
  fi
  printf '%-34s %8s %8s %8s%s\n' "$pat" "$staged" "$tree" "$hist" "$flag"
done

echo
if [ "$found" = "1" ]; then
  echo "LEAK FOUND. Do not commit or push."
  echo "  staged  = about to be committed; unstage it and take the value out."
  echo "  tree    = already in a tracked file at HEAD; remove it and amend."
  echo "  history = in an earlier commit; the history needs rewriting, and if it was ever"
  echo "            pushed, treat the value as PUBLIC and rotate it."
  exit 1
fi
echo "clean: no pattern appears in the staged content, in a tracked file at HEAD, or"
echo "       anywhere in history."

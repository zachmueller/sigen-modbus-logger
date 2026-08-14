#!/bin/sh
# Show the most recent captured values. Read-only; safe while the logger runs.
#
# Passes the newest few archive files, not just the open one: during an outage the
# current file can contain nothing but empty probe records, and the last known good
# values then live in the previous file.
set -e
DIR=$(cd "$(dirname "$0")/.." && pwd)
eval "$(python3 "$DIR/config.py" --sh)"
D=$SIGEN_DATA_DIR

NEWEST=$(ls -t "$D"/*.bin "$D"/*.bin.gz 2>/dev/null | head -1)
[ -n "$NEWEST" ] || { echo "no archive files found in $D"; exit 1; }

# Everything is keyed off the plan fingerprint in the newest archive filename, so
# that one invocation never mixes plans. A plain manifest glob breaks the moment a
# second manifest exists -- at the next date rollover, or after a retune -- because
# decode.py reads argv[1] as the manifest and everything after it as data, so
# manifest #2 arrives as a data file and trips the plan-hash check.
HASH=$(basename "$NEWEST" | sed -E 's/.*-([0-9a-f]{8})\.bin(\.gz)?$/\1/')
MANIFEST=$(ls -t "$D"/*-"$HASH".manifest.json 2>/dev/null | head -1)
[ -n "$MANIFEST" ] || { echo "no manifest for plan $HASH in $D"; exit 1; }
FILES=$(ls -t "$D"/*-"$HASH".bin "$D"/*-"$HASH".bin.gz 2>/dev/null | head -3)

# shellcheck disable=SC2086
exec python3 "$DIR/decode.py" "$MANIFEST" $FILES --last "$@"

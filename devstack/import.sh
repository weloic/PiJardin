#!/usr/bin/env bash
# Load a /export file (gzipped line protocol) into the local InfluxDB.
#
# Idempotent by construction: a point's identity is measurement + tag set + timestamp, so
# re-importing the same file overwrites the same points rather than duplicating them. Import
# the same window twice, or two overlapping windows, without cleaning anything first.
set -euo pipefail

cd "$(dirname "$0")"
# shellcheck source=dev.env
source ./dev.env

HOST="http://localhost:8086"

## Lines per request. The write endpoint caps request size, and a rejected batch has to be
## readable: a 5 000-line chunk that fails names one bad line among five thousand, where a
## single 400 000-line POST would just say "no".
BATCH=5000

FILE="${1:-}"
if [ -z "$FILE" ]; then
    echo "Usage: ./import.sh <pijardin_*.lp.gz>" >&2
    exit 1
fi
if [ ! -f "$FILE" ]; then
    echo "No such file: $FILE" >&2
    exit 1
fi
if ! curl -sf "$HOST/health" > /dev/null; then
    echo "InfluxDB is not answering on $HOST — run ./up.sh first." >&2
    exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# Decompress into fixed-size chunks on disk rather than accumulating them in the shell: a
# 90-day export is a few hundred thousand lines, and appending those to a bash variable is
# quadratic.
gunzip -c "$FILE" | split -l "$BATCH" - "$TMP/chunk."

posted=0
for chunk in "$TMP"/chunk.*; do
    lines=$(wc -l < "$chunk")
    response="$(curl -sS -w '\n%{http_code}' \
        --data-binary "@$chunk" \
        -H "Authorization: Token $DEV_TOKEN" \
        -H "Content-Type: text/plain; charset=utf-8" \
        "$HOST/api/v2/write?org=$DEV_ORG&bucket=$DEV_BUCKET&precision=ns")"

    code="${response##*$'\n'}"
    if [ "$code" != "204" ]; then
        # Printed in full, never swallowed: on a type conflict InfluxDB replies 422 naming the
        # field and the offending line, and that message is the entire diagnosis.
        echo >&2
        echo "HTTP $code on $(basename "$chunk"):" >&2
        echo "${response%$'\n'*}" >&2
        exit 1
    fi

    posted=$((posted + lines))
    printf '\r  %d lines written...' "$posted"
done
printf '\r  %d lines written.   \n' "$posted"

# Ask the database what it now holds rather than trusting that 204s meant what they looked
# like. `lenght_median` is the series six of the seven panels are drawn from, so a count of
# zero here means a blank dashboard no matter how many lines were accepted above.
count="$(curl -sS \
    -H "Authorization: Token $DEV_TOKEN" \
    -H "Content-Type: application/vnd.flux" \
    -H "Accept: application/csv" \
    --data-binary "from(bucket: \"$DEV_BUCKET\")
  |> range(start: -100y)
  |> filter(fn: (r) => r._measurement == \"height_measure\" and r._field == \"lenght_median\")
  |> count()" \
    "$HOST/api/v2/query?org=$DEV_ORG" | tr -d '\r' | awk -F, 'NR==2 {print $NF}')"

echo "  ${count:-0} well-level readings now in the database."
echo
echo "-> http://localhost:3000  (dashboard \"PiJardin\")"

#!/usr/bin/env bash
# merge_backfill.sh — Fold a build_backfill.py side file into the live log with
# the recorder stopped for as short a time as possible.
#
#   merge_backfill.sh <backfill.jsonl> [--dry-run-only]
#
# Sequence: stop recorder → dry run → apply ONLY if the dry run shows exactly
# "+N new, 0 already present" (N = records in the side file) and none of the new
# records appears in a warning → restart recorder → verify.
#
# The recorder is restarted on every exit path (trap), so a failed check or an
# error can't leave it down past RESUME_MAX_GAP_S and truncate an open session.
# If the recorder wasn't running to begin with, it is left stopped.
set -euo pipefail

SIDE="${1:?usage: merge_backfill.sh <backfill.jsonl> [--dry-run-only]}"
MODE="${2:-}"
SIDE="$(realpath "$SIDE")"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
cd "$REPO"
LOG=data/activity_log.jsonl

N=$(grep -c . "$SIDE")
[ "$N" -gt 0 ] || { echo "side file $SIDE is empty"; exit 1; }
# Start timestamps of the new records — any of these showing up in the merge
# report (overlap, collapsed, superseded) means the backfill isn't clean.
mapfile -t STARTS < <(grep -o '"start":"[^"]*"' "$SIDE" | cut -d'"' -f4)

merge() {
  docker compose run --rm --no-deps \
    -v "$REPO/backend/merge_activity_logs.py:/app/merge_activity_logs.py:ro" \
    -v "$SIDE:/import/backfill.jsonl:ro" \
    recorder python merge_activity_logs.py \
      --from /import/backfill.jsonl --log /data/activity_log.jsonl "$@" 2>&1 \
    | grep -v ' Container '
}

WAS_RUNNING=$(docker compose ps --status running -q recorder)
restart() {
  if [ -n "$WAS_RUNNING" ]; then
    docker compose start recorder >/dev/null 2>&1 && echo "== recorder restarted" \
      || echo "!! FAILED to restart recorder — run: docker compose start recorder"
  fi
}
trap restart EXIT

BEFORE=$(grep -c . "$LOG")
[ -n "$WAS_RUNNING" ] && docker compose stop recorder >/dev/null 2>&1 && echo "== recorder stopped"

echo "== dry run"
DRY=$(merge --dry-run) || { echo "$DRY"; echo "!! dry run failed; nothing applied"; exit 1; }
echo "$DRY"

if ! grep -qF "(+$N new, 0 already present)" <<<"$DRY"; then
  echo "!! expected +$N new, 0 already present; nothing applied"; exit 1
fi
for s in "${STARTS[@]}"; do
  if grep -qF "$s" <<<"$DRY"; then
    echo "!! new record starting $s appears in a merge warning; nothing applied"; exit 1
  fi
done

if [ "$MODE" = "--dry-run-only" ]; then
  echo "== dry run clean; --dry-run-only, nothing applied"; exit 0
fi

echo "== apply"
OUT=$(merge) || { echo "$OUT"; echo "!! merge failed"; exit 1; }
echo "$OUT"
grep -q "verified: read-back matches the merge" <<<"$OUT" || { echo "!! merge did not verify"; exit 1; }

restart; trap - EXIT
sleep 3

echo "== verify"
AFTER=$(grep -c . "$LOG")
echo "log lines: $BEFORE -> $AFTER (expected +$N)"
for s in "${STARTS[@]}"; do
  grep -n -F "\"start\":\"$s\"" "$LOG" | cut -c1-160
done
[ -n "$WAS_RUNNING" ] && docker compose logs --tail 4 recorder
[ "$AFTER" -eq $((BEFORE + N)) ] || { echo "!! line count off"; exit 1; }

#!/usr/bin/env python3
"""
build_backfill.py — Turn human-described missed sessions into recorder-shaped
JSONL records, and refuse to if they don't fit the live log.

Read-only against the live log: it writes only the --out side file, which is
then folded in with backend/merge_activity_logs.py (see SKILL.md).

Each --entry is: ACTIVITY START END [TZ]
  ACTIVITY  a key from config/activity_card_id_map.json (e.g. exercise)
  START/END local wall-clock ISO, e.g. 2026-09-21T20:00 (an explicit offset
            like 2026-09-25T16:30-07:00 is honoured as-is)
  TZ        optional IANA zone for this entry; defaults to --tz

Checks (any failure → exit 1, nothing written):
  * activity is in the card map
  * end > start, and end is not in the future
  * no overlap with a live-log record, the recorder's open session, or
    another entry — the bar runs one timer, so a real log never overlaps
  * no identity collision (start, card_id, phase, interval_index) with the log

Example:
  python build_backfill.py --out /tmp/x.jsonl \\
      --entry exercise 2026-09-21T20:00 2026-09-21T21:52 \\
      --entry exercise 2026-09-25T13:30 2026-09-25T13:54 America/Los_Angeles
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[4]  # .claude/skills/<name>/scripts/ -> repo root
UTC = timezone.utc

# Activities the bar runs as INTERVAL timers (work/rest pomodoro). Everything
# else is SIMPLE/INFINITE → phase "focus", interval_index null. Derived from the
# live log at runtime; this is only the fallback if the log has no example.
INTERVAL_FALLBACK = {"work", "development", "perfect_form"}


def iso_ms(dt: datetime) -> str:
    """Same rendering the recorder uses: UTC, millisecond precision, 'Z'."""
    dt = dt.astimezone(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def parse_local(s: str, tz: ZoneInfo) -> datetime:
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=tz)


def parse_z(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def load_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--entry", nargs="+", action="append", required=True, metavar="ARG",
                   help="ACTIVITY START END [TZ]; repeatable")
    p.add_argument("--out", required=True, help="side-file to write (put it in the scratchpad)")
    p.add_argument("--tz", default="America/New_York", help="default zone (compose TZ default)")
    p.add_argument("--log", default=str(REPO / "data" / "activity_log.jsonl"))
    p.add_argument("--state", default=str(REPO / "data" / "recorder_state.json"))
    p.add_argument("--card-map", default=str(REPO / "config" / "activity_card_id_map.json"))
    args = p.parse_args()

    card_map = json.loads(Path(args.card_map).read_text(encoding="utf-8"))
    key_to_card = {v: k for k, v in card_map.items()}
    live = load_jsonl(Path(args.log))
    interval_keys = {r["activity"] for r in live if r.get("phase") in ("work", "rest")} or INTERVAL_FALLBACK
    identities = {(r.get("start"), r.get("card_id"), r.get("phase"), r.get("interval_index")) for r in live}
    now = datetime.now(UTC)

    # The recorder's in-progress session counts as occupied time from its start to now.
    occupied = [(parse_z(r["start"]), parse_z(r["end"]), r) for r in live if r.get("start") and r.get("end")]
    try:
        state = json.loads(Path(args.state).read_text(encoding="utf-8"))
        o = state.get("open")
        if o:
            start = datetime.fromtimestamp(o["start_ms"] / 1000, UTC)
            occupied.append((start, now, {"activity": o["key"], "phase": o["phase"], "start": iso_ms(start),
                                          "end": "(open now)"}))
            print(f"note: recorder has an OPEN {o['key']} session since "
                  f"{start.astimezone(ZoneInfo(args.tz)):%a %b %d %I:%M %p %Z}")
    except (OSError, ValueError):
        pass

    errors: list[str] = []
    records: list[dict] = []
    spans: list[tuple[datetime, datetime, dict]] = []

    for raw in args.entry:
        if len(raw) not in (3, 4):
            errors.append(f"--entry {' '.join(raw)}: expected ACTIVITY START END [TZ]")
            continue
        activity, s, e = raw[:3]
        tz = ZoneInfo(raw[3] if len(raw) == 4 else args.tz)
        label = f"{activity} {s}→{e} ({tz.key})"
        if activity not in key_to_card:
            errors.append(f"{label}: unknown activity; known: {', '.join(sorted(key_to_card))}")
            continue
        try:
            start, end = parse_local(s, tz), parse_local(e, tz)
        except ValueError as exc:
            errors.append(f"{label}: {exc}")
            continue
        if end <= start:
            errors.append(f"{label}: end is not after start")
            continue
        if end > now:
            errors.append(f"{label}: ends in the future (now {now.astimezone(tz):%a %b %d %I:%M %p %Z}) "
                          "— likely a wrong time zone, AM/PM, or date")
            continue

        interval = activity in interval_keys
        rec = {
            "activity": activity,
            "phase": "work" if interval else "focus",
            "start": iso_ms(start),
            "end": iso_ms(end),
            "duration_s": round((end - start).total_seconds(), 1),
            "interval_index": 0 if interval else None,
            "card_id": key_to_card[activity],
            "partial": False,
            "truncated": False,
        }
        if interval:
            rec["overtime_s"] = 0.0  # recorder emits this on work records (FLOW_OVERTIME)

        if (rec["start"], rec["card_id"], rec["phase"], rec["interval_index"]) in identities:
            errors.append(f"{label}: a record with this identity is already in the log")
        for a, b, other in occupied + spans:
            if start < b and a < end:
                errors.append(f"{label}: overlaps {other['activity']} {other['phase']} "
                              f"{other['start']} → {other['end']}")
        spans.append((start, end, rec))
        records.append(rec)

        local = ZoneInfo(args.tz)
        print(f"{activity:<13} {start.astimezone(tz):%a %b %d %I:%M %p}–{end.astimezone(tz):%I:%M %p %Z}"
              f"  (= {start.astimezone(local):%a %I:%M %p}–{end.astimezone(local):%I:%M %p %Z})"
              f"  {rec['start']} → {rec['end']}  {rec['duration_s']}s")

    if errors:
        print("\nREFUSED — fix these and rerun; nothing written:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for rec in sorted(records, key=lambda r: r["start"]):
            fh.write(json.dumps(rec, separators=(",", ":")) + "\n")  # byte-identical to SessionWriter
    print(f"\nwrote {len(records)} record(s) to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

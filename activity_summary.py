#!/usr/bin/env python3
"""Summarize Busy Bar activity sessions for a single day.

Usage:
    ./activity_summary.py 7/21/26          # M/D/YY (or M/D/YYYY)
    ./activity_summary.py 7/21/26 --log path/to/activity_log.jsonl

Prints two tables:
  1. every session that day, with its local start time and duration
  2. total time spent in each activity

Sessions shorter than one minute are filtered out. A "day" runs from
03:00 to 03:00 local time (not midnight), so late-night work still lands
on the day it started.
"""
import argparse
import datetime as dt
import json
import os
import sys

DAY_CUTOFF_HOUR = 3           # a day runs [03:00, next 03:00) local
MIN_DURATION_S = 60           # drop sessions shorter than this
DEFAULT_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "activity_log.jsonl")


def parse_day_arg(s):
    """Parse 'M/D/YY' or 'M/D/YYYY' into a date."""
    parts = s.strip().split("/")
    if len(parts) != 3:
        raise ValueError(f"expected M/D/YY, got {s!r}")
    month, day, year = (int(p) for p in parts)
    if year < 100:
        year += 2000
    return dt.date(year, month, day)


def logical_day(local_dt):
    """The day a local datetime belongs to, given the 3 AM cutoff."""
    return (local_dt - dt.timedelta(hours=DAY_CUTOFF_HOUR)).date()


def fmt_duration(seconds):
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    out = ""
    if h:
        out += f"{h}h"
    if h or m:
        out += f"{m}m"
    out += f"{sec}s"
    return out


def load_sessions(path):
    if not os.path.exists(path):
        sys.exit(f"log file not found: {path}")
    sessions = []
    with open(path) as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                print(f"warning: skipping malformed line {lineno}: {e}", file=sys.stderr)
                continue
            # ISO-8601 with trailing Z (UTC); convert to local time
            start_utc = dt.datetime.fromisoformat(rec["start"].replace("Z", "+00:00"))
            rec["_start_local"] = start_utc.astimezone()
            rec["_duration_s"] = float(rec.get("duration_s", 0.0))
            sessions.append(rec)
    return sessions


def render_table(headers, rows, aligns):
    """Render a simple monospace table. aligns: '<' or '>' per column."""
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def line(cells):
        return "  ".join(
            f"{c:{a}{w}}" for c, a, w in zip(cells, aligns, widths)
        ).rstrip()

    out = [line(headers), "  ".join("-" * w for w in widths)]
    out += [line(row) for row in rows]
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description="Summarize a day of Busy Bar sessions.")
    ap.add_argument("day", nargs="?",
                    help="day to summarize, e.g. 7/21/26 (default: today)")
    ap.add_argument("--log", default=DEFAULT_LOG, help="path to activity_log.jsonl")
    args = ap.parse_args()

    if args.day is None:
        # "today" respects the 3 AM cutoff, so late-night work counts as today
        target = logical_day(dt.datetime.now().astimezone())
    else:
        try:
            target = parse_day_arg(args.day)
        except ValueError as e:
            sys.exit(f"bad day argument: {e}")

    sessions = load_sessions(args.log)

    kept = [
        s for s in sessions
        if logical_day(s["_start_local"]) == target
        and s["_duration_s"] >= MIN_DURATION_S
    ]
    kept.sort(key=lambda s: s["_start_local"])

    tzname = dt.datetime.now().astimezone().tzname() or "local"
    print(f"Busy Bar — {target:%a %-m/%-d/%Y}  "
          f"(day = 03:00→03:00 {tzname}, sessions <1m hidden)\n")

    if not kept:
        print("No sessions of one minute or longer for that day.")
        return

    # Table 1: every session
    rows = []
    for s in kept:
        label = s["activity"]
        if s.get("phase") and s["phase"] not in ("focus",):
            label += f" ({s['phase']})"
        rows.append([
            s["_start_local"].strftime("%H:%M:%S"),
            label,
            fmt_duration(s["_duration_s"]),
        ])
    print("Sessions")
    print(render_table(["start", "activity", "duration"], rows, ["<", "<", ">"]))

    # Table 2: total per activity, with rest split into its own shared category
    totals = {}
    counts = {}
    for s in kept:
        cat = "rest" if s.get("phase") == "rest" else s["activity"]
        totals[cat] = totals.get(cat, 0.0) + s["_duration_s"]
        counts[cat] = counts.get(cat, 0) + 1
    trows = [
        [act, str(counts[act]), fmt_duration(totals[act])]
        for act in sorted(totals, key=lambda a: -totals[a])
    ]
    trows.append(["TOTAL", str(len(kept)), fmt_duration(sum(totals.values()))])
    print("\nTotal per activity")
    print(render_table(["activity", "sessions", "total"], trows, ["<", ">", ">"]))


if __name__ == "__main__":
    main()

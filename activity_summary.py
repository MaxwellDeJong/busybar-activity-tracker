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
import sys

from dashboard_data import (
    DEFAULT_LOG,
    MIN_DURATION_S,
    fmt_duration,
    load_records,
    logical_day,
    parse_day_arg,
)


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

    try:
        sessions = load_records(args.log)
    except FileNotFoundError:
        sys.exit(f"log file not found: {args.log}")

    kept = [
        s for s in sessions
        if logical_day(s["start_local"]) == target
        and s["duration_s"] >= MIN_DURATION_S
    ]
    kept.sort(key=lambda s: s["start_local"])

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
            s["start_local"].strftime("%H:%M:%S"),
            label,
            fmt_duration(s["duration_s"]),
        ])
    print("Sessions")
    print(render_table(["start", "activity", "duration"], rows, ["<", "<", ">"]))

    # Table 2: total per activity, with rest split into its own shared category
    totals = {}
    counts = {}
    for s in kept:
        cat = "rest" if s.get("phase") == "rest" else s["activity"]
        totals[cat] = totals.get(cat, 0.0) + s["duration_s"]
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

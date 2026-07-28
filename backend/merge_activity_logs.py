#!/usr/bin/env python3
"""
merge_activity_logs.py — Fold an out-of-band session log into the live one.

One-shot migration tool. When tracking moved from the USB-tethered laptop
(HTTP/WS transport) to the homelab (MQTT transport), the laptop's log kept the
only copy of every session before the cutover. Both logs were produced by the
same SessionTracker against the same device clock, so records line up exactly —
this merges them into the single log the recorder and dashboard read.

Append-only discipline
----------------------
The live log is append-only: the recorder never rewrites it, only opens it "a".
A merge is the one operation that must insert *before* the end, so it is done
without ever mutating the file in place:

  * records are never edited — the merge only ever chooses between two whole
    records that describe the same session, so every surviving line is one some
    recorder actually wrote;
  * the pre-merge log is copied to an archive (mode 444) first;
  * the merged log is written to a temp file in the same directory and swapped
    in with os.replace(), so a concurrent reader (the dashboard) sees either the
    whole old file or the whole new one, never a partial write.

Stop the recorder for the swap so no append lands between read and replace; its
MQTT session is durable (fixed client_id, clean_session=False), so the broker
buffers QoS-1 snapshots meanwhile and replays them on restart. See README.

Session identity
----------------
A session is keyed by (start, card_id, phase, interval_index). `start` is the
device's own snapshot_timestamp_ms rendered as ISO-8601, not the recorder's
arrival time, so two transports watching the same bar agree to the millisecond.

When both logs hold the same session, the more complete observation wins:

  1. non-truncated beats truncated — a truncated `end` is only a lower bound,
     recorded where that recorder's stream dropped mid-session;
  2. then the longer duration;
  3. then non-partial beats partial — `partial` means that recorder connected
     mid-session and cannot vouch for the start;
  4. ties keep the live log's copy (fewest lines changed).

Usage
-----
    python merge_activity_logs.py --from historic_activity_log.jsonl --dry-run
    python merge_activity_logs.py --from historic_activity_log.jsonl
    python merge_activity_logs.py --self-test      # offline logic check
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # backend/ -> repo root
DEFAULT_LOG = os.environ.get("ACTIVITY_LOG") or os.path.join(_ROOT, "data", "activity_log.jsonl")

# Fields that identify a session. Everything else (end, duration_s, partial,
# truncated, overtime_s) is an *observation* of it and may legitimately differ
# between two recorders that watched the same session.
IDENTITY = ("start", "card_id", "phase", "interval_index")


# ---------------------------------------------------------------------------
# Merge logic (pure; no I/O, unit-testable)
# ---------------------------------------------------------------------------
def record_key(rec: dict) -> tuple:
    return tuple(rec.get(f) for f in IDENTITY)


def completeness(rec: dict) -> tuple:
    """Rank one observation of a session; higher is more complete. See §Session
    identity in the module docstring for why the fields are ordered this way."""
    return (
        0 if rec.get("truncated") else 1,
        float(rec.get("duration_s") or 0.0),
        0 if rec.get("partial") else 1,
    )


def sort_key(rec: dict) -> tuple:
    """Chronological, matching the order the recorder appends in. Ties broken on
    the rest of the identity so the output is deterministic."""
    return (rec.get("start") or "", rec.get("end") or "", str(rec.get("card_id")), str(rec.get("phase")))


def merge_records(live: list[dict], incoming: list[dict]) -> tuple[list[dict], dict]:
    """Merge `incoming` into `live`, returning (merged, stats).

    `live` is the authoritative log; a record from `incoming` displaces its
    counterpart only by being a strictly more complete observation of the same
    session. Duplicates *within* either input collapse the same way.
    """
    chosen: dict[tuple, dict] = {}
    stats = {"added": [], "kept": [], "superseded": [], "collapsed": []}

    def offer(rec: dict, source: str) -> None:
        key = record_key(rec)
        prev = chosen.get(key)
        if prev is None:
            chosen[key] = rec
            if source == "incoming":
                stats["added"].append(rec)
            return
        if source == "live":
            # A second copy of one session inside a single file.
            stats["collapsed"].append(rec)
            if completeness(rec) > completeness(prev):
                chosen[key] = rec
            return
        if completeness(rec) > completeness(prev):
            chosen[key] = rec
            stats["superseded"].append((prev, rec))
        else:
            stats["kept"].append(rec)

    for rec in live:
        offer(rec, "live")
    for rec in incoming:
        offer(rec, "incoming")

    return sorted(chosen.values(), key=sort_key), stats


def find_overlaps(records: list[dict]) -> list[tuple[dict, dict]]:
    """Sessions whose spans overlap. The bar runs one timer, so a real log has
    none; any hit means two records describe the same span under different
    identities and the merge needs a human look."""
    out = []
    for a, b in zip(records, records[1:]):
        if a.get("end") and b.get("start") and a["end"] > b["start"]:
            out.append((a, b))
    return out


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------
def load_jsonl(path: Path) -> list[dict]:
    records = []
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path}:{lineno}: malformed JSON ({exc})")
            missing = [f for f in ("start", "end", "card_id") if f not in rec]
            if missing:
                raise SystemExit(f"{path}:{lineno}: record missing {', '.join(missing)}")
            records.append(rec)
    return records


def dump_line(rec: dict) -> str:
    # Byte-identical to SessionWriter.write, so merged and appended lines match.
    return json.dumps(rec, separators=(",", ":"))


def archive(log: Path, backup_dir: Path) -> Path:
    """Copy the pre-merge log aside, read-only, before anything replaces it."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = backup_dir / f"{log.stem}.{stamp}.pre-merge.jsonl"
    shutil.copy2(log, dest)
    dest.chmod(0o444)
    return dest


def atomic_write(log: Path, records: list[dict]) -> None:
    """Write the merged log beside the original and swap it in one rename."""
    mode = log.stat().st_mode & 0o777
    fd, tmp_name = tempfile.mkstemp(dir=str(log.parent), prefix=log.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for rec in records:
                fh.write(dump_line(rec) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        tmp.chmod(mode)
        os.replace(tmp, log)  # atomic: readers see old or new, never a mix
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    # Durably record the rename itself, not just the bytes.
    dir_fd = os.open(str(log.parent), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------
def _rec(start, end, dur, *, partial=False, truncated=False, card="ID1", phase="work", index=0):
    return {
        "activity": "development", "phase": phase, "start": start, "end": end,
        "duration_s": dur, "interval_index": index, "card_id": card,
        "partial": partial, "truncated": truncated,
    }


def self_test() -> int:
    ok = True

    def check(name, got, want):
        nonlocal ok
        if got == want:
            print(f"  PASS  {name}")
        else:
            ok = False
            print(f"  FAIL  {name}\n        got : {got}\n        want: {want}")

    a1 = _rec("T1", "T1b", 60.0)
    a2 = _rec("T2", "T2b", 30.0)
    b1 = _rec("T0", "T0b", 90.0)

    merged, stats = merge_records([a1, a2], [b1])
    check("disjoint records merge chronologically",
          [r["start"] for r in merged], ["T0", "T1", "T2"])
    check("disjoint → all incoming counted as added", len(stats["added"]), 1)

    # Identical session in both logs → one line, nothing added.
    merged, stats = merge_records([a1], [dict(a1)])
    check("same session in both logs → deduped", len(merged), 1)
    check("same session → not counted as added", (len(stats["added"]), len(stats["kept"])), (0, 1))

    # A truncated observation must not displace a complete one, even though the
    # live log is only the tie-breaker: this is the cutover case, where the
    # laptop force-closed the session at the moment it was unplugged.
    live = _rec("T5", "T5+140", 140.0)
    stale = _rec("T5", "T5", 0.0, truncated=True)
    merged, stats = merge_records([live], [stale])
    check("truncated incoming loses to complete live",
          (len(merged), merged[0]["duration_s"], len(stats["superseded"])), (1, 140.0, 0))

    # ...and the reverse: the live log's copy is the truncated one.
    merged, stats = merge_records([stale], [live])
    check("complete incoming supersedes truncated live",
          (len(merged), merged[0]["duration_s"], len(stats["superseded"])), (1, 140.0, 1))

    # `partial` marks an unreliable start: the recorder that observed a real stop
    # first wins, even with an identical span.
    part = _rec("T6", "T6b", 42.0, partial=True)
    full = _rec("T6", "T6b", 42.0, partial=False)
    merged, _ = merge_records([part], [full])
    check("non-partial incoming supersedes partial live", merged[0]["partial"], False)
    merged, _ = merge_records([full], [part])
    check("partial incoming does not displace non-partial live", merged[0]["partial"], False)

    # Same start, different card → genuinely different sessions.
    merged, _ = merge_records([_rec("T7", "T7b", 10.0, card="ID1")],
                              [_rec("T7", "T7c", 10.0, card="ID2")])
    check("same start, different card → both kept", len(merged), 2)

    # Same card and start, different phase → work and rest are distinct sessions.
    merged, _ = merge_records([_rec("T8", "T8b", 10.0, phase="work")],
                              [_rec("T8", "T8c", 10.0, phase="rest")])
    check("same start, different phase → both kept", len(merged), 2)

    # A duplicated line inside one file collapses to its better copy.
    merged, stats = merge_records([stale, live], [])
    check("intra-file duplicate collapses to the complete copy",
          (len(merged), merged[0]["duration_s"], len(stats["collapsed"])), (1, 140.0, 1))

    check("overlap detector flags a straddling pair",
          len(find_overlaps([_rec("A", "C", 1.0), _rec("B", "D", 1.0)])), 1)
    check("overlap detector accepts back-to-back sessions",
          len(find_overlaps([_rec("A", "B", 1.0), _rec("B", "D", 1.0)])), 0)

    print("\nself-test:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _brief(rec: dict) -> str:
    return (
        f"{rec.get('activity'):<14} {rec.get('phase'):<5} "
        f"{float(rec.get('duration_s') or 0):>7.1f}s  {rec.get('start')} → {rec.get('end')}"
        + ("  [partial]" if rec.get("partial") else "")
        + ("  [truncated]" if rec.get("truncated") else "")
    )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--from", dest="source", help="log to fold in (the laptop-era log)")
    p.add_argument("--log", default=DEFAULT_LOG, help=f"live log to merge into (default {DEFAULT_LOG})")
    p.add_argument("--backup-dir", default=None, help="archive dir (default <log dir>/archive)")
    p.add_argument("-n", "--dry-run", action="store_true", help="report the merge, write nothing")
    p.add_argument("--self-test", action="store_true", help="offline logic check, no files touched")
    args = p.parse_args()

    if args.self_test:
        return self_test()
    if not args.source:
        p.error("--from is required (or use --self-test)")

    log = Path(args.log)
    source = Path(args.source)
    for path in (log, source):
        if not path.is_file():
            raise SystemExit(f"no such file: {path}")

    live = load_jsonl(log)
    incoming = load_jsonl(source)
    merged, stats = merge_records(live, incoming)

    print(f"live     {log}       {len(live):>4} records")
    print(f"incoming {source}    {len(incoming):>4} records")
    print(f"merged                                  {len(merged):>4} records "
          f"(+{len(stats['added'])} new, {len(stats['kept'])} already present)")

    if stats["collapsed"]:
        print(f"\n{len(stats['collapsed'])} duplicate line(s) collapsed within an input:")
        for rec in stats["collapsed"]:
            print(f"    {_brief(rec)}")
    if stats["superseded"]:
        print(f"\n{len(stats['superseded'])} live record(s) replaced by a more complete observation:")
        for old, new in stats["superseded"]:
            print(f"  - {_brief(old)}\n  + {_brief(new)}")

    # Every live record must survive verbatim unless explicitly superseded.
    replaced = {record_key(old) for old, _ in stats["superseded"]}
    kept_lines = {dump_line(r) for r in merged}
    lost = [r for r in live if record_key(r) not in replaced and dump_line(r) not in kept_lines]
    if lost:
        print(f"\nABORT: {len(lost)} live record(s) would be dropped without being superseded:")
        for rec in lost[:10]:
            print(f"    {_brief(rec)}")
        return 1

    overlaps = find_overlaps(merged)
    if overlaps:
        print(f"\nwarning: {len(overlaps)} overlapping session pair(s) — review before trusting totals:")
        for a, b in overlaps[:10]:
            print(f"    {_brief(a)}\n    {_brief(b)}")

    if args.dry_run:
        print("\ndry run: nothing written")
        return 0

    backup_dir = Path(args.backup_dir) if args.backup_dir else log.parent / "archive"
    kept = archive(log, backup_dir)
    print(f"\narchived pre-merge log (read-only): {kept}")
    atomic_write(log, merged)
    print(f"wrote {len(merged)} records to {log}")

    written = load_jsonl(log)
    if [dump_line(r) for r in written] != [dump_line(r) for r in merged]:
        print("ABORT: verification read-back does not match; restore from the archive")
        return 1
    print("verified: read-back matches the merge")
    return 0


if __name__ == "__main__":
    sys.exit(main())

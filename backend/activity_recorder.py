#!/usr/bin/env python3
"""
activity_recorder.py — Passive activity-session recorder for the Busy Bar.

Runs on the machine physically connected to the bar. It is READ-ONLY with
respect to the device: it opens the /api/status/ws stream, watches the timer
snapshots, and appends one JSONL line per completed activity session to a log
file. It never changes device settings and never participates in selection —
selection happens entirely on the device (see firmware-activity-selection-plan.md).

  stream snapshot  ──►  SessionTracker (state machine)  ──►  JSONL session log
        │                        │
   card_id, phase,          maps card_id → activity key via
   is_paused, ts            activity_card_id_map.json

Session model
-------------
A *session* is one continuous span during which the timer is running for a
given activity and phase. It ends when the timer stops running (pause, abandon,
completion) or the activity/phase changes. The end timestamp is the moment the
timer stopped running (Request 1).

Flow overtime (--flow-overtime)
-------------------------------
With interval autostart disabled (all shipped activities), when work time
expires the firmware advances to the Rest phase but *waits, paused* for you to
start the break. In flow mode the recorder keeps the WORK session open through
that waiting period and closes it only when the break actually starts running —
so time worked past the timer is counted as work (Request 2). A genuine
mid-work pause is still closed immediately, because it stays in the work phase
rather than flipping to a not-yet-started rest phase.

Usage
-----
    pip install busylib
    python activity_recorder.py --addr 10.0.4.20                  # standard pomodoro accounting
    python activity_recorder.py --addr 10.0.4.20 --flow-overtime  # count work past the timer
    python activity_recorder.py --self-test                       # offline logic check, no hardware
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

RUNNING_TYPES = ("INFINITE", "SIMPLE", "INTERVAL")

# Defaults come from the environment first (so the container can point at the
# shared /data + /config mounts) and fall back to the repo's config/ + data/ dirs
# for a plain local run. CLI flags still override both.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # backend/ -> repo root
DEFAULT_MAP = os.environ.get("CARD_MAP") or os.path.join(_ROOT, "config", "activity_card_id_map.json")
DEFAULT_OUT = os.environ.get("ACTIVITY_LOG") or os.path.join(_ROOT, "data", "activity_log.jsonl")
DEFAULT_ADDR = os.environ.get("BUSY_ADDR", "10.0.4.20")


# ---------------------------------------------------------------------------
# Snapshot → (running, key, phase, index) derivation
# ---------------------------------------------------------------------------
def derive_state(snap: dict, card_id_map: dict) -> dict:
    """Reduce a raw timer snapshot to the fields the tracker reasons about."""
    snap_type = snap.get("type")
    card_id = snap.get("card_id")
    is_paused = bool(snap.get("is_paused", False))

    running = (snap_type in RUNNING_TYPES) and not is_paused

    if snap_type == "INTERVAL":
        index = int(snap.get("current_interval", 0))
        phase = "work" if index % 2 == 0 else "rest"
    elif snap_type in ("SIMPLE", "INFINITE"):
        index = None
        phase = "focus"
    else:  # NOT_STARTED / unknown
        index = None
        phase = None

    key = card_id_map.get(card_id, card_id) if card_id else None

    return {
        "running": running,
        "key": key,
        "phase": phase,
        "index": index,
        "card_id": card_id,
        "type": snap_type,
    }


def _iso(ms: int) -> str:
    return (
        datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


# ---------------------------------------------------------------------------
# Session state machine (pure; no I/O, unit-testable)
# ---------------------------------------------------------------------------
class SessionTracker:
    def __init__(self, card_id_map: dict, flow_overtime: bool = False):
        self.map = card_id_map
        self.flow_overtime = flow_overtime
        self.open: dict | None = None
        self.overtime_start_ms: int | None = None
        self._observed_stop = False  # have we ever seen a non-running snapshot?

    def _open(self, st: dict, ts_ms: int) -> None:
        self.open = {
            "key": st["key"],
            "phase": st["phase"],
            "index": st["index"],
            "card_id": st["card_id"],
            "start_ms": ts_ms,
            # A session opened before we ever observed a stopped state may have
            # begun before the recorder connected: its start time is unreliable.
            "partial": not self._observed_stop,
        }

    def _close(self, ts_ms: int, overtime_ms: int = 0, truncated: bool = False) -> dict:
        o = self.open
        assert o is not None
        record = {
            "activity": o["key"],
            "phase": o["phase"],
            "start": _iso(o["start_ms"]),
            "end": _iso(ts_ms),
            "duration_s": round(max(0, ts_ms - o["start_ms"]) / 1000, 1),
            "interval_index": o["index"],
            "card_id": o["card_id"],
            "partial": o["partial"],
            # True when the session did not end on an observed stop but was force
            # closed at the last snapshot we saw (e.g. the stream dropped). Its end
            # is a lower bound: the real stop may have been later, during the gap.
            "truncated": truncated,
        }
        if self.flow_overtime and o["phase"] == "work":
            record["overtime_s"] = round(max(0, overtime_ms) / 1000, 1)
        self.open = None
        self.overtime_start_ms = None
        return record

    def close_open(self, ts_ms: int) -> list[dict]:
        """Force-close any open session at ts_ms, flagged truncated. Called when
        the stream drops so we never extend a session across an unobserved gap."""
        if self.open is None:
            return []
        overtime = ts_ms - self.overtime_start_ms if self.overtime_start_ms else 0
        return [self._close(ts_ms, overtime_ms=overtime, truncated=True)]

    def on_snapshot(self, snap: dict, ts_ms: int) -> list[dict]:
        """Feed one snapshot; return any sessions completed by it (0 or 1)."""
        st = derive_state(snap, self.map)
        if not st["running"]:
            self._observed_stop = True

        out: list[dict] = []

        if self.open is None:
            if st["running"]:
                self._open(st, ts_ms)
            return out

        o = self.open
        same_activity = st["key"] == o["key"]

        # Still the same running session → just note it and continue.
        if st["running"] and same_activity and st["phase"] == o["phase"]:
            return out

        if self.flow_overtime and o["phase"] == "work" and same_activity and st["phase"] == "rest":
            # Work has expired into the rest phase.
            if not st["running"]:
                # Post-work waiting period: keep the work session open (overtime).
                if self.overtime_start_ms is None:
                    self.overtime_start_ms = ts_ms
                return out
            # Rest is now running → the break was manually started. Close work at
            # this moment (absorbing any overtime), then open the rest session.
            overtime = ts_ms - self.overtime_start_ms if self.overtime_start_ms else 0
            out.append(self._close(ts_ms, overtime_ms=overtime))
            self._open(st, ts_ms)
            return out

        # General case: the open session ended (paused, abandoned, completed, or
        # the activity/phase changed). It ends at this snapshot's timestamp.
        overtime = ts_ms - self.overtime_start_ms if self.overtime_start_ms else 0
        out.append(self._close(ts_ms, overtime_ms=overtime))
        if st["running"]:
            self._open(st, ts_ms)
        return out


# ---------------------------------------------------------------------------
# JSONL sink
# ---------------------------------------------------------------------------
class SessionWriter:
    def __init__(self, path: Path):
        self.path = path

    def write(self, record: dict) -> None:
        line = json.dumps(record, separators=(",", ":"))
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        print(
            f"  logged: {record['activity']:<14} {record['phase']:<5} "
            f"{record['duration_s']:>6.0f}s  {record['start']} → {record['end']}"
            + (
                f"  (+{record['overtime_s']:.0f}s overtime)"
                if record.get("overtime_s")
                else ""
            )
            + ("  [partial]" if record.get("partial") else "")
        )


# ---------------------------------------------------------------------------
# Live streaming loop
# ---------------------------------------------------------------------------
def load_map(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must be a JSON object mapping card_id → key")
    return data


async def run_stream(
    addr: str,
    token: str | None,
    tracker: SessionTracker,
    writer: SessionWriter,
    raw_log: Path | None,
) -> None:
    # Reuse the proven client + protobuf-json decode from the discovery probe.
    from busy_probe import make_client, _decode_json_wrapper, TIMER_KEY

    backoff = 1
    # Persists across reconnects: the stream replays the last (possibly stale)
    # snapshot on connect, so we drop any snapshot not newer than the last we
    # acted on. This keeps a stale replay from spuriously closing a live session.
    last_ts_ms = -1
    while True:
        bb = make_client(async_=True, addr=addr, token=token)
        try:
            print(f"connected to {addr}; streaming timer snapshots (Ctrl-C to stop)")
            backoff = 1
            async for item in bb.stream_status_ws(enable=True, decode_protobuf=True):
                if not isinstance(item, dict):
                    continue
                for update in item.get("updates", []) or []:
                    if not isinstance(update, dict) or TIMER_KEY not in update:
                        continue
                    parsed, _note = _decode_json_wrapper(update[TIMER_KEY])
                    if not isinstance(parsed, dict):
                        continue
                    snap = parsed.get("snapshot", parsed)
                    ts_ms = int(parsed.get("snapshot_timestamp_ms", 0))
                    if ts_ms <= last_ts_ms:
                        continue  # stale/duplicate replay — ignore
                    last_ts_ms = ts_ms
                    if raw_log is not None:
                        with raw_log.open("a", encoding="utf-8") as f:
                            f.write(json.dumps(parsed, separators=(",", ":")) + "\n")
                    for record in tracker.on_snapshot(snap, ts_ms):
                        writer.write(record)
        except (KeyboardInterrupt, asyncio.CancelledError):
            raise
        except Exception as exc:  # noqa: BLE001 — keep the recorder alive across drops
            # Bound the damage: close any in-progress session at the last snapshot
            # we actually saw, rather than letting it run across the outage.
            for record in tracker.close_open(last_ts_ms):
                writer.write(record)
            print(f"  stream error ({type(exc).__name__}: {exc}); reconnecting in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
        finally:
            try:
                await bb.close()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Offline self-test — validates the session logic without hardware
# ---------------------------------------------------------------------------
def _snap(t, card_id="ID2", paused=False, index=0, left=1500000, total=1500000):
    s = {"type": t, "card_id": card_id, "is_paused": paused}
    if t == "INTERVAL":
        s.update(
            current_interval=index,
            current_interval_time_left_ms=left,
            current_interval_time_total_ms=total,
        )
    elif t == "SIMPLE":
        s["time_left_ms"] = left
    return s


def _run(seq, flow):
    tr = SessionTracker({"ID2": "tech_reading"}, flow_overtime=flow)
    out = []
    for snap, ts in seq:
        out.extend(tr.on_snapshot(snap, ts))
    return out


def self_test() -> int:
    ok = True

    def check(name, got, want):
        nonlocal ok
        if got == want:
            print(f"  PASS  {name}")
        else:
            ok = False
            print(f"  FAIL  {name}\n        got : {got}\n        want: {want}")

    S = 1000  # 1 second in ms

    # 1. Plain work run then pause → one work session ending at the pause.
    seq = [
        (_snap("NOT_STARTED"), 0),
        (_snap("INTERVAL", index=0), 10 * S),
        (_snap("INTERVAL", index=0), 100 * S),
        (_snap("INTERVAL", index=0, paused=True), 130 * S),  # paused at t=130
    ]
    res = _run(seq, flow=False)
    check(
        "work then pause → 1 session, ends at pause",
        [(r["activity"], r["phase"], r["duration_s"]) for r in res],
        [("tech_reading", "work", 120.0)],
    )

    # 2. Pause splits into two sessions (Request 1).
    seq = [
        (_snap("NOT_STARTED"), 0),
        (_snap("INTERVAL", index=0), 10 * S),
        (_snap("INTERVAL", index=0, paused=True), 40 * S),  # pause at 40
        (_snap("INTERVAL", index=0), 70 * S),  # resume at 70
        (_snap("INTERVAL", index=0, paused=True), 100 * S),  # pause at 100
    ]
    res = _run(seq, flow=False)
    check(
        "mid-work pause → two work sessions",
        [(r["phase"], r["duration_s"]) for r in res],
        [("work", 30.0), ("work", 30.0)],
    )

    # 3. Standard mode: work expires → rest waits → break starts. Work ends at
    #    the boundary (rest appears), not at break-start.
    seq = [
        (_snap("NOT_STARTED"), 0),
        (_snap("INTERVAL", index=0), 10 * S),
        (_snap("INTERVAL", index=1, paused=True), 1500 * S),  # work expired at 1500
        (_snap("INTERVAL", index=1), 1800 * S),  # break started at 1800
    ]
    res = _run(seq, flow=False)
    check(
        "standard: work ends at boundary",
        [(r["phase"], r["duration_s"]) for r in res],
        [("work", 1490.0)],  # started at first running snapshot t=10s, boundary t=1500s
    )

    # 4. Flow mode, same sequence: work absorbs the overtime and ends at break-start.
    res = _run(seq, flow=True)
    check(
        "flow: work extends to break-start with overtime",
        [(r["phase"], r["duration_s"], r.get("overtime_s")) for r in res],
        [("work", 1790.0, 300.0)],  # start t=10s → break-start t=1800s; overtime 1500→1800
    )

    # 5. Flow mode: mid-work pause still splits (does not get absorbed).
    seq = [
        (_snap("NOT_STARTED"), 0),
        (_snap("INTERVAL", index=0), 10 * S),
        (_snap("INTERVAL", index=0, paused=True), 40 * S),  # genuine work pause
        (_snap("INTERVAL", index=0), 70 * S),
        (_snap("INTERVAL", index=1, paused=True), 1500 * S),  # expire → wait
        (_snap("INTERVAL", index=1), 1600 * S),  # break at 1600
    ]
    res = _run(seq, flow=True)
    check(
        "flow: mid-work pause splits, final span absorbs overtime",
        [(r["phase"], r["duration_s"], r.get("overtime_s")) for r in res],
        [("work", 30.0, 0.0), ("work", 1530.0, 100.0)],
    )

    # 6. Flow mode: abandon during overtime wait (never start break).
    seq = [
        (_snap("NOT_STARTED"), 0),
        (_snap("INTERVAL", index=0), 10 * S),
        (_snap("INTERVAL", index=1, paused=True), 1500 * S),  # expire → wait
        (_snap("NOT_STARTED"), 1700 * S),  # abandoned at 1700
    ]
    res = _run(seq, flow=True)
    check(
        "flow: abandon during overtime closes work at abandon",
        [(r["phase"], r["duration_s"], r.get("overtime_s")) for r in res],
        [("work", 1690.0, 200.0)],  # start t=10s → abandon t=1700s; overtime 1500→1700
    )

    # 7. INFINITE activity: runs until stopped.
    seq = [
        (_snap("NOT_STARTED"), 0),
        (_snap("INFINITE", card_id="ID2"), 10 * S),
        (_snap("INFINITE", card_id="ID2"), 500 * S),
        (_snap("NOT_STARTED"), 900 * S),
    ]
    res = _run(seq, flow=False)
    check(
        "infinite: single focus session until stop",
        [(r["phase"], r["duration_s"]) for r in res],
        [("focus", 890.0)],
    )

    # 8. Unmapped card_id falls back to the raw UUID.
    tr = SessionTracker({}, flow_overtime=False)
    res = []
    res += tr.on_snapshot(_snap("NOT_STARTED"), 0)
    res += tr.on_snapshot(_snap("SIMPLE", card_id="unknown-uuid"), 10 * S)
    res += tr.on_snapshot(_snap("NOT_STARTED"), 60 * S)
    check("unmapped card_id → raw uuid key", [r["activity"] for r in res], ["unknown-uuid"])

    # 9. Disconnect mid-session → close_open truncates at the last-good timestamp.
    tr = SessionTracker({"ID2": "tech_reading"}, flow_overtime=False)
    tr.on_snapshot(_snap("NOT_STARTED"), 0)
    tr.on_snapshot(_snap("INTERVAL", index=0), 10 * S)
    res = tr.close_open(50 * S)  # stream dropped; last snapshot seen was t=50s
    check(
        "disconnect → truncated session ends at last-good ts",
        [(r["phase"], r["duration_s"], r["truncated"]) for r in res],
        [("work", 40.0, True)],
    )
    check("close_open with nothing open → no record", tr.close_open(60 * S), [])

    # 10. Normal closes are not flagged truncated.
    res = _run(
        [
            (_snap("NOT_STARTED"), 0),
            (_snap("INTERVAL", index=0), 10 * S),
            (_snap("INTERVAL", index=0, paused=True), 40 * S),
        ],
        flow=False,
    )
    check("normal close is not truncated", [r["truncated"] for r in res], [False])

    print("\nself-test:", "ALL PASS" if ok else "FAILURES")
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--addr", default=DEFAULT_ADDR, help=f"device address (default {DEFAULT_ADDR})")
    p.add_argument("--token", default=None, help="API token, if the device requires one")
    p.add_argument("--map", default=DEFAULT_MAP, help=f"card_id→key map (default {DEFAULT_MAP})")
    p.add_argument("--out", default=DEFAULT_OUT, help=f"session JSONL log (default {DEFAULT_OUT})")
    p.add_argument(
        "--flow-overtime",
        action="store_true",
        default=os.environ.get("FLOW_OVERTIME", "").lower() not in ("", "0", "false", "no"),
        help="count work done past the timer until the break is manually started "
             "(also enabled by FLOW_OVERTIME=1)",
    )
    p.add_argument("--raw-log", default=None, help="also append every raw snapshot to this file (debug)")
    p.add_argument("--self-test", action="store_true", help="run offline logic checks and exit")
    args = p.parse_args()

    # Line-buffer stdout so a long-running foreground recorder shows progress
    # live rather than only when the buffer flushes.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    if args.self_test:
        return self_test()

    map_path = Path(args.map)
    if not map_path.exists():
        print(f"error: map file not found: {map_path}", file=sys.stderr)
        return 2
    card_id_map = load_map(map_path)
    print(f"loaded {len(card_id_map)} activity mappings from {map_path}")
    print(f"flow overtime: {'ON' if args.flow_overtime else 'off'}")
    print(f"writing sessions to {args.out}")

    tracker = SessionTracker(card_id_map, flow_overtime=args.flow_overtime)
    writer = SessionWriter(Path(args.out))
    raw_log = Path(args.raw_log) if args.raw_log else None

    try:
        asyncio.run(run_stream(args.addr, args.token, tracker, writer, raw_log))
    except KeyboardInterrupt:
        print("\nstopped.")
        if tracker.open is not None:
            print(f"note: an in-progress {tracker.open['phase']} session for "
                  f"{tracker.open['key']} was not closed (still running at exit).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

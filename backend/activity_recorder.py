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


def _env_flag(name: str) -> bool:
    """Interpret an env var as a boolean flag (unset/0/false/no → False)."""
    return os.environ.get(name, "").strip().lower() not in ("", "0", "false", "no")


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


def process_parsed(
    parsed: dict,
    state: dict,
    tracker: SessionTracker,
    writer: SessionWriter,
    raw_log: Path | None,
) -> None:
    """Feed one decoded snapshot payload through the tracker.

    Shared by both transports. `parsed` is the top-level object
    ``{"snapshot": {...}, "snapshot_timestamp_ms": <ms>}`` — the WS yields it
    after protobuf-JSON decode, MQTT after ``json.loads``. `state` is a mutable
    dict carrying ``last_ts_ms`` across reconnects within the process: both
    transports can replay/redeliver the last snapshot (WS replays on connect,
    MQTT QoS-1 can redeliver), so we drop anything not strictly newer. This
    keeps a stale replay from spuriously closing a live session.
    """
    if not isinstance(parsed, dict):
        return
    snap = parsed.get("snapshot", parsed)
    ts_ms = int(parsed.get("snapshot_timestamp_ms", 0))
    if ts_ms <= state["last_ts_ms"]:
        return  # stale/duplicate replay or redelivery — ignore
    state["last_ts_ms"] = ts_ms
    if raw_log is not None:
        with raw_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(parsed, separators=(",", ":")) + "\n")
    for record in tracker.on_snapshot(snap, ts_ms):
        writer.write(record)


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
    # Persists across reconnects (see process_parsed for the dedupe rationale).
    state = {"last_ts_ms": -1}
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
                    process_parsed(parsed, state, tracker, writer, raw_log)
        except (KeyboardInterrupt, asyncio.CancelledError):
            raise
        except Exception as exc:  # noqa: BLE001 — keep the recorder alive across drops
            # Bound the damage: close any in-progress session at the last snapshot
            # we actually saw, rather than letting it run across the outage.
            for record in tracker.close_open(state["last_ts_ms"]):
                writer.write(record)
            print(f"  stream error ({type(exc).__name__}: {exc}); reconnecting in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
        finally:
            try:
                await bb.close()
            except Exception:  # noqa: BLE001
                pass


def run_mqtt(
    broker: str,
    port: int,
    topic: str,
    tracker: SessionTracker,
    writer: SessionWriter,
    raw_log: Path | None,
    client_id: str,
    tls: bool = False,
    tls_insecure: bool = False,
    ca_certs: str | None = None,
    username: str | None = None,
    password: str | None = None,
) -> None:
    """Record from an MQTT broker the bar publishes snapshots to.

    Unlike the live-only WebSocket, the broker buffers QoS-1 messages for a
    durable session (fixed client_id, clean_session=False) while the recorder is
    briefly offline, so restarts/crashes don't lose events. paho's threaded loop
    delivers each snapshot to on_message; the tracker/writer are synchronous, so
    no asyncio is needed here.
    """
    import paho.mqtt.client as mqtt

    # Persists across reconnects within this process (see process_parsed).
    state = {"last_ts_ms": -1}

    def on_connect(client, userdata, flags, reason_code, properties=None):
        if reason_code.is_failure:
            print(f"  mqtt connect failed: {reason_code}")
            return
        print(f"connected to mqtt://{broker}:{port}; subscribing {topic!r} (qos 1)")
        client.subscribe(topic, qos=1)

    def on_disconnect(client, userdata, flags=None, reason_code=None, properties=None):
        # Bound the damage exactly like the WS path: close any open session at the
        # last snapshot we actually saw rather than across the outage. With a
        # durable session most drops lose nothing, so `truncated` records are rare.
        for record in tracker.close_open(state["last_ts_ms"]):
            writer.write(record)
        print(f"  mqtt disconnected (rc={reason_code}); paho will auto-reconnect")

    def on_message(client, userdata, msg):
        try:
            parsed = json.loads(msg.payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        process_parsed(parsed, state, tracker, writer, raw_log)

    # Durable session so the broker buffers QoS-1 messages across recorder restarts.
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=client_id,
        clean_session=False,
    )
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    if username:
        client.username_pw_set(username, password)
    if tls:
        # §3.1 fallback: the device's MQTT stack may insist on TLS. A self-signed
        # broker cert works with tls_insecure (skip hostname/CA verification).
        client.tls_set(ca_certs=ca_certs)
        if tls_insecure:
            client.tls_insecure_set(True)
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    client.connect(broker, port, keepalive=60)
    client.loop_forever(retry_first_connection=True)


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

    # MQTT transport (durable, buffered). When --mqtt / MQTT_BROKER is set the
    # recorder subscribes to the broker instead of opening the device WebSocket.
    p.add_argument(
        "--mqtt",
        metavar="HOST[:PORT]",
        default=os.environ.get("MQTT_BROKER"),
        help="record from an MQTT broker instead of the WebSocket "
             "(also set by MQTT_BROKER; default port 1883)",
    )
    p.add_argument("--mqtt-topic",
                   default=os.environ.get("MQTT_TOPIC", "sessions/+/up/v1/busy/snapshot"),
                   help="snapshot topic to subscribe to. The firmware publishes snapshots on "
                        "the session scope once linked, so the default matches any session "
                        "(default sessions/+/up/v1/busy/snapshot)")
    p.add_argument("--mqtt-client-id", default=os.environ.get("MQTT_CLIENT_ID", "busybar-recorder"),
                   help="durable client id; keep stable so the broker buffers across restarts")
    p.add_argument("--mqtt-tls", action="store_true",
                   default=_env_flag("MQTT_TLS"),
                   help="connect over TLS (mqtts); use if the device refuses plain mqtt")
    p.add_argument("--mqtt-tls-insecure", action="store_true",
                   default=_env_flag("MQTT_TLS_INSECURE"),
                   help="skip broker cert/hostname verification (self-signed broker cert)")
    p.add_argument("--mqtt-ca", default=os.environ.get("MQTT_CA"),
                   help="path to a CA cert bundle to verify the broker (implies TLS)")
    p.add_argument("--mqtt-username", default=os.environ.get("MQTT_USERNAME"),
                   help="broker username (if the broker requires auth)")
    p.add_argument("--mqtt-password", default=os.environ.get("MQTT_PASSWORD"),
                   help="broker password")
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
        if args.mqtt:
            host, _, port = args.mqtt.partition(":")
            use_tls = args.mqtt_tls or bool(args.mqtt_ca)
            print(f"transport: MQTT ({'mqtts' if use_tls else 'mqtt'}://{host}:{port or 1883})")
            run_mqtt(
                host, int(port or 1883), args.mqtt_topic,
                tracker, writer, raw_log, args.mqtt_client_id,
                tls=use_tls, tls_insecure=args.mqtt_tls_insecure, ca_certs=args.mqtt_ca,
                username=args.mqtt_username, password=args.mqtt_password,
            )
        else:
            print(f"transport: WebSocket ({args.addr})")
            asyncio.run(run_stream(args.addr, args.token, tracker, writer, raw_log))
    except KeyboardInterrupt:
        print("\nstopped.")
        if tracker.open is not None:
            print(f"note: an in-progress {tracker.open['phase']} session for "
                  f"{tracker.open['key']} was not closed (still running at exit).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

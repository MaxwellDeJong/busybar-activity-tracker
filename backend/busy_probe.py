#!/usr/bin/env python3
"""
busy_probe.py — Protocol discovery probe for the Busy Bar.

Run this on the machine that is physically connected to the bar. It is a
READ-ONLY discovery tool: it does not write an activity log and does not change
device settings. Its job is to start answering the open questions from our
design discussion:

  1. CONNECTION / IDENTITY  — which transport/interface is active, and basic
     device info, over the HTTP command channel.
  2. TIMER / PROFILE SHAPE  — what the (otherwise-opaque) timer and profile JSON
     actually contains, both as an HTTP snapshot and live on the stream.
  3. INPUT STREAMING        — whether physical controls (the three buttons, the
     5-position switch, the scroll encoder) reach a headless client, and in what
     form, via the /api/status/ws WebSocket.
  4. UPDATE INVENTORY       — which StateUpdate variants the firmware actually
     emits, so we know what the stream can be relied on to deliver.

Key facts this probe is built around (verified against busylib + busybar-protobuf):
  * There is ONE WebSocket, /api/status/ws. It is async-only in busylib.
    A JSON handshake {"enable": true} is sent, then binary protobuf frames arrive.
  * Each frame is a BSB_State.State with a repeated `updates` list. Every update
    is a oneof that is exactly one of: device_name, power, brightness,
    audio_volume, wifi, update_state, update_check, timezone, matter, frame,
    input, timer, ble, auto_update_state, timer_profiles.
  * So input events, timer state, and profiles all ride the SAME stream — no
    separate subscription is needed.
  * `frame` updates are front-display refreshes and are high-volume; this probe
    counts them but never prints pixel data (use --show-frames to see summaries).

Usage:
    pip install busylib
    python busy_probe.py --addr 10.0.4.20               # 60s live capture
    python busy_probe.py --addr 10.0.4.20 --seconds 0   # capture until Ctrl-C
    python busy_probe.py --self-test                    # offline, no hardware

During a live run, physically exercise the controls: flip the mode switch
through its positions, spin the scroll wheel both ways, and press each button.
Watch for INPUT lines to confirm they arrive.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import gzip
import json
import sys
import zlib
from collections import Counter
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# The oneof field names inside BSB_State.StateUpdate (proto field names).
# We categorize each update by which of these keys is present.
# ---------------------------------------------------------------------------
INPUT_KEY = "input"
TIMER_KEY = "timer"
PROFILES_KEY = "timer_profiles"
FRAME_KEY = "frame"


def _stamp() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%H:%M:%S.%f")[:-3]


# Proto3 omits scalar fields set to their zero value, and MessageToDict drops
# them. For the input enums, an ABSENT field means "the zero value", so we must
# supply it explicitly or we'd misread real events (OK/PRESS/BUSY are all 0).
_BUTTON_DEFAULT = "OK"       # Button 0
_ACTION_DEFAULT = "PRESS"    # ButtonAction 0
_SWITCH_DEFAULT = "BUSY"     # SwitchPosition 0


_NO_COMPRESSION = (None, "", 0, "NONE", "COMPRESSION_NONE")


def _decode_json_wrapper(payload):
    """
    Timer/Profile payloads ride in a BSB_Util.Json wrapper {compression, data}.
    `data` is a BYTES field, so MessageToDict base64-encodes it into a string;
    `compression` is an enum whose zero value ("none") is omitted when absent.

    Real unwrap: base64-decode data -> optionally decompress -> json.loads.
    Returns (parsed_or_none, note).
    """
    if not isinstance(payload, dict):
        return None, f"non-dict payload: {type(payload).__name__}"
    wrapper = payload.get("json", payload)
    if not isinstance(wrapper, dict):
        return None, "no json wrapper"
    data = wrapper.get("data")
    compression = wrapper.get("compression")  # absent => none

    if not isinstance(data, str) or data == "":
        return None, "no json.data field present"

    # data arrives base64-encoded (bytes field). Fall back to treating it as a
    # plain JSON string in case a future firmware/library sends it differently.
    try:
        raw = base64.b64decode(data)
    except Exception:  # noqa: BLE001
        try:
            return json.loads(data), "parsed from json.data (plain string)"
        except Exception as exc:  # noqa: BLE001
            return None, f"json.data neither base64 nor JSON: {exc}"

    buf = raw
    if compression in _NO_COMPRESSION:
        pass
    else:
        # Unknown/compressed: best-effort gzip then zlib.
        for fn in (gzip.decompress, zlib.decompress):
            try:
                buf = fn(raw)
                break
            except Exception:  # noqa: BLE001
                continue
        else:
            return None, f"compressed (compression={compression}); could not gunzip/inflate"

    try:
        return json.loads(buf.decode("utf-8")), f"parsed (compression={compression or 'none'})"
    except Exception as exc:  # noqa: BLE001
        return None, f"decoded {len(buf)} bytes but not valid JSON: {exc}"


def _pretty(obj) -> str:
    """Best-effort pretty JSON for pydantic models / dicts / anything."""
    for attr in ("model_dump_json",):
        if hasattr(obj, attr):
            try:
                return json.dumps(json.loads(getattr(obj, attr)()), indent=2)
            except Exception:
                pass
    if hasattr(obj, "model_dump"):
        try:
            return json.dumps(obj.model_dump(), indent=2, default=str)
        except Exception:
            pass
    try:
        return json.dumps(obj, indent=2, default=str)
    except Exception:
        return repr(obj)


# ===========================================================================
# Client construction (auth is optional; the constructor uses *args/**kwargs,
# so we try the token kwarg and fall back to setting the header directly).
# ===========================================================================
def make_client(async_: bool, addr: str, token: str | None):
    from busylib import AsyncBusyBar, BusyBar

    cls = AsyncBusyBar if async_ else BusyBar
    if token:
        try:
            return cls(addr, api_token=token)
        except TypeError:
            client = cls(addr)
            try:
                client.client.headers["X-API-Token"] = token
            except Exception:
                print("  ! could not attach token; continuing without it")
            return client
    return cls(addr)


# ===========================================================================
# PHASE A — HTTP command channel: identity, connection, timer/profile snapshot
# ===========================================================================
def phase_a_http(addr: str, token: str | None) -> None:
    print("=" * 70)
    print("PHASE A — HTTP command channel")
    print("=" * 70)

    bb = make_client(async_=False, addr=addr, token=token)

    # Each probe is isolated so one failure doesn't sink the rest.
    probes = [
        ("version", lambda: bb.version()),
        ("access mode", lambda: bb.access()),
        ("connection_type", lambda: bb.connection_type),
        ("transport (active interface)", lambda: bb.transport()),
        ("is_usb_connected", lambda: bb.is_usb_connected()),
        ("status_device", lambda: bb.status_device()),
        ("status_power", lambda: bb.status_power()),
        ("status_system", lambda: bb.status_system()),
        # These are the interesting ones: the timer/profile JSON shape.
        ("busy_profile[busy]", lambda: bb.busy_profile("busy")),
        ("busy_profile[custom]", lambda: bb.busy_profile("custom")),
        ("busy_snapshot", lambda: bb.busy_snapshot()),
    ]

    for label, fn in probes:
        try:
            result = fn()
            print(f"\n[{label}]")
            print(_pretty(result))
        except Exception as exc:  # noqa: BLE001 — discovery: report and move on
            print(f"\n[{label}] -> ERROR: {type(exc).__name__}: {exc}")

    try:
        bb.close()
    except Exception:
        pass


# ===========================================================================
# PHASE B — WebSocket stream: input, timer, profiles, and the update inventory
# ===========================================================================
class StreamSummary:
    def __init__(self) -> None:
        self.update_counts: Counter = Counter()
        self.text_messages = 0
        self.raw_frames = 0
        self.decode_errors = 0
        self.last_timer_json = None
        self.last_profiles_json = None
        self.input_events = 0
        self.switch_positions_seen: set[str] = set()
        self.buttons_seen: set[str] = set()
        self.encoder_total = 0

    def report(self) -> None:
        print("\n" + "=" * 70)
        print("STREAM SUMMARY — which StateUpdate variants actually arrived")
        print("=" * 70)
        if not self.update_counts:
            print("  (no decoded state updates observed)")
        for name, count in self.update_counts.most_common():
            print(f"  {name:<20} {count}")
        print(f"\n  raw text messages : {self.text_messages}")
        print(f"  raw binary frames : {self.raw_frames}")
        print(f"  decode errors     : {self.decode_errors}")

        print("\n  --- INPUT (the load-bearing question) ---")
        print(f"  input events      : {self.input_events}")
        print(f"  buttons seen      : {sorted(self.buttons_seen) or '(none)'}")
        print(f"  switch positions  : {sorted(self.switch_positions_seen) or '(none)'}")
        print(f"  encoder net delta : {self.encoder_total}")
        if self.input_events:
            print("  => Physical input DOES reach a headless client via the stream.")
        else:
            print("  => No input observed. If you exercised the controls and still")
            print("     see nothing, input may need a different enable flag or channel.")

        print("\n  --- TIMER / PROFILE JSON SHAPE ---")
        if self.last_timer_json is not None:
            print("  last timer json keys:",
                  list(self.last_timer_json.keys())
                  if isinstance(self.last_timer_json, dict) else type(self.last_timer_json))
        else:
            print("  no timer update seen on the stream")
        if self.last_profiles_json is not None:
            print("  profiles payload captured (see live log above)")
        else:
            print("  no timer_profiles update seen on the stream")


def handle_state_dict(state: dict, summary: StreamSummary, show_frames: bool) -> None:
    """Process one decoded BSB_State.State dict (from MessageToDict)."""
    for update in state.get("updates", []) or []:
        if not isinstance(update, dict) or not update:
            continue
        # Each update is a oneof: exactly one key identifies the variant.
        key = next(iter(update.keys()))
        summary.update_counts[key] += 1
        payload = update[key]

        if key == INPUT_KEY:
            _handle_input(payload, summary)
        elif key == TIMER_KEY:
            parsed, note = _decode_json_wrapper(payload)
            summary.last_timer_json = parsed if parsed is not None else payload
            if parsed is not None:
                print(f"[{_stamp()}] TIMER   ({note})\n{json.dumps(parsed, indent=2, default=str)}")
            else:
                print(f"[{_stamp()}] TIMER   ({note}) raw={json.dumps(payload, default=str)}")
        elif key == PROFILES_KEY:
            summary.last_profiles_json = payload
            # Profiles is a list; each profile has its own name + json wrapper.
            print(f"[{_stamp()}] PROFILES ({len(payload.get('profiles', [])) if isinstance(payload, dict) else '?'} profiles)")
            if isinstance(payload, dict):
                for prof in payload.get("profiles", []):
                    name = prof.get("name", "?") if isinstance(prof, dict) else "?"
                    parsed, note = _decode_json_wrapper(prof)
                    body = json.dumps(parsed, default=str) if parsed is not None else f"({note})"
                    print(f"           - {name}: {body}")
        elif key == FRAME_KEY:
            summary.raw_frames += 1
            if show_frames and isinstance(payload, dict):
                dims = f"{payload.get('width')}x{payload.get('height')}"
                enc = payload.get("encoding")
                print(f"[{_stamp()}] FRAME   {dims} encoding={enc} (data suppressed)")
        else:
            # Subsystem state (power, wifi, brightness, ble, timezone, ...)
            print(f"[{_stamp()}] STATE   {key}: {json.dumps(payload, default=str)}")


def _handle_input(payload: dict, summary: StreamSummary) -> None:
    summary.input_events += 1
    if not isinstance(payload, dict):
        print(f"[{_stamp()}] INPUT   {payload}")
        return
    if "button_event" in payload:
        b = payload["button_event"]
        button = b.get("button", _BUTTON_DEFAULT)   # absent == zero value (OK)
        action = b.get("action", _ACTION_DEFAULT)   # absent == zero value (PRESS)
        summary.buttons_seen.add(str(button))
        print(f"[{_stamp()}] INPUT   button={button:<6} action={action}")
    elif "switch_event" in payload:
        pos = payload["switch_event"].get("position", _SWITCH_DEFAULT)  # absent == BUSY
        summary.switch_positions_seen.add(str(pos))
        print(f"[{_stamp()}] INPUT   switch -> {pos}")
    elif "encoder_event" in payload:
        delta = payload["encoder_event"].get("delta", 0)
        try:
            summary.encoder_total += int(delta)
        except Exception:
            pass
        print(f"[{_stamp()}] INPUT   encoder delta={delta}")
    else:
        print(f"[{_stamp()}] INPUT   {payload}")


async def phase_b_stream(addr: str, token: str | None, seconds: int, show_frames: bool) -> None:
    print("\n" + "=" * 70)
    print("PHASE B — /api/status/ws live stream")
    print("(flip the switch, spin the wheel, press the buttons now)")
    print("=" * 70)

    bb = make_client(async_=True, addr=addr, token=token)
    summary = StreamSummary()

    async def consume() -> None:
        async for item in bb.stream_status_ws(enable=True, decode_protobuf=True):
            if isinstance(item, str):
                summary.text_messages += 1
                print(f"[{_stamp()}] TEXT    {item}")
            elif isinstance(item, (bytes, bytearray)):
                summary.raw_frames += 1  # only if decode_protobuf=False
            elif isinstance(item, dict):
                handle_state_dict(item, summary, show_frames)

    try:
        if seconds and seconds > 0:
            print(f"(capturing for {seconds}s)\n")
            try:
                await asyncio.wait_for(consume(), timeout=seconds)
            except asyncio.TimeoutError:
                print("\n(capture window elapsed)")
        else:
            print("(capturing until Ctrl-C)\n")
            await consume()
    except KeyboardInterrupt:
        print("\n(interrupted)")
    except Exception as exc:  # noqa: BLE001
        print(f"\nSTREAM ERROR: {type(exc).__name__}: {exc}")
    finally:
        try:
            await bb.close()
        except Exception:
            pass
        summary.report()


# ===========================================================================
# OFFLINE SELF-TEST — proves the decode/categorization path without hardware.
# Builds real protobuf messages, serializes, decodes exactly as the live path
# does, and runs them through the same handlers.
# ===========================================================================
def self_test() -> int:
    print("SELF-TEST — exercising the decode path offline (no device)\n")
    try:
        from google.protobuf.json_format import MessageToDict
        from busylib.state_stream_proto import input_pb2, state_pb2, timer_pb2
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED to import protobuf modules: {exc}")
        return 1

    state = state_pb2.State()

    # Synthesize the three input event kinds + a timer update.
    u_btn = state.updates.add()
    u_btn.input.button_event.button = input_pb2.START
    u_btn.input.button_event.action = input_pb2.PRESS

    u_sw = state.updates.add()
    u_sw.input.switch_event.position = input_pb2.CUSTOM

    u_enc = state.updates.add()
    u_enc.input.encoder_event.delta = -3

    u_timer = state.updates.add()
    # Timer carries a BSB_Util.Json wrapper {compression, data}; data holds JSON.
    try:
        u_timer.timer.json.data = b'{"phase":"work","remaining_s":900}'
    except Exception:
        pass

    # Round-trip through the wire exactly like stream_status_ws does.
    wire = state.SerializeToString()
    decoded = state_pb2.State()
    decoded.ParseFromString(wire)
    as_dict = MessageToDict(decoded, preserving_proto_field_name=True)

    summary = StreamSummary()
    handle_state_dict(as_dict, summary, show_frames=True)
    summary.report()

    ok = (
        summary.input_events == 3
        and "START" in summary.buttons_seen
        and "CUSTOM" in summary.switch_positions_seen
        and summary.encoder_total == -3
        and isinstance(summary.last_timer_json, dict)
        and summary.last_timer_json.get("phase") == "work"
    )
    print("\nSELF-TEST:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


# ===========================================================================
def main() -> int:
    p = argparse.ArgumentParser(description="Busy Bar protocol discovery probe.")
    p.add_argument("--addr", default="10.0.4.20", help="device address (default 10.0.4.20)")
    p.add_argument("--token", default=None, help="HTTP access key, if access mode is 'key'")
    p.add_argument("--seconds", type=int, default=60,
                   help="live capture duration; 0 = until Ctrl-C (default 60)")
    p.add_argument("--show-frames", action="store_true",
                   help="print (summarized) front-display frame updates")
    p.add_argument("--skip-http", action="store_true", help="skip Phase A")
    p.add_argument("--skip-stream", action="store_true", help="skip Phase B")
    p.add_argument("--self-test", action="store_true",
                   help="run the offline decode self-test and exit")
    args = p.parse_args()

    if args.self_test:
        return self_test()

    print(f"Busy Bar probe -> {args.addr}   ({_stamp()})\n")

    if not args.skip_http:
        try:
            phase_a_http(args.addr, args.token)
        except Exception as exc:  # noqa: BLE001
            print(f"PHASE A aborted: {type(exc).__name__}: {exc}")

    if not args.skip_stream:
        try:
            asyncio.run(phase_b_stream(args.addr, args.token, args.seconds, args.show_frames))
        except KeyboardInterrupt:
            print("\n(interrupted)")

    return 0


if __name__ == "__main__":
    sys.exit(main())

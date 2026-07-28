#!/usr/bin/env python3
"""
mqtt_linker.py — Completes the Busy Bar account link against our own broker.

Runs on the homelab alongside the broker and the recorder. It plays the role of
the vendor cloud just far enough to move the bar from ``ConnectedNotLinked`` to
``ConnectedLinked``, after which the firmware publishes timer snapshots on the
session scope on its own. No firmware changes; no vendor cloud. See
docs/mqtt-migration.md §6 (Path A) for the full rationale and source references.

Why this service exists
-----------------------
On firmware r909 (api_version 25.0.0) timer snapshots publish on the *session*
scope (``sessions/<session_id>/up/v1/busy/snapshot``), which the firmware only
permits while linked. A bare broker therefore receives only the device-scope
``presence`` beacon and no snapshots. The link handshake, however, does **no
validation**: the device accepts any JSON on ``devices/<serial>/down/v1/link/token``
carrying non-empty ``session_id``/``token``/``email``/``user_id``, stores it,
and flips itself to linked. We complete that handshake ourselves.

    presence / link.request  ──►  linker  ──►  link/token (non-retained)
      (device→broker)                              (broker→device)
                                     │
                             serial → uuid5 session_id
                             (stable, persisted linked-set)

Loop-safety, and why a *cooldown* rather than a permanent set (§6.7)
-------------------------------------------------------------------
1. The device re-publishes ``presence`` on *every* connect, including the
   reconnect it does right after becoming linked. If we re-sent a token on that,
   we'd force an endless link→reconnect→presence loop. So we rate-limit: we
   re-send to a given serial only if we haven't sent it one within
   ``cooldown_s`` (default 60 s). The post-link reconnect announces within a
   second or two — far inside the cooldown — so it's ignored, breaking the loop.

   We deliberately do NOT use a permanent "already-linked" set. A device that
   resets its saved state (a power-cycle/factory reset regenerates its
   ``client_id`` and returns it to ``NotLinked``) re-emits ``presence`` but sends
   no ``unlink``; a permanent set would ignore it forever, stranding the device
   NotLinked with no snapshots until someone manually wiped the state file. With
   a cooldown the linker self-heals: the device's next presence past the cooldown
   re-links it automatically.
2. The token must be published **non-retained**. A retained ``link/token`` would
   be redelivered on every reconnect → the same reconnect loop.

Usage
-----
    python mqtt_linker.py                       # uses MQTT_BROKER / LINKER_STATE env
    python mqtt_linker.py --mqtt 192.168.0.47:1883 --state ./linker_state.json
    python mqtt_linker.py --self-test           # offline logic check, no broker
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

# Topic roots/directions the firmware uses (docs §3.1). Every topic is
# <root>/<id>/<dir>/v1/<app-topic>; id is the serial (device scope) or the
# session_id (session scope).
PRESENCE_TOPIC = "devices/+/up/v1/presence"
LINK_REQUEST_TOPIC = "devices/+/up/v1/link/request"
UNLINK_TOPIC = "sessions/+/up/v1/unlink"

# Don't re-send a token to the same serial within this window. Long enough to
# swallow the device's immediate post-link reconnect (a second or two), short
# enough to re-link a genuinely reset device promptly. See the module docstring.
DEFAULT_COOLDOWN_S = 60

# A fixed namespace so session_ids are stable across linker restarts/reinstalls:
# uuid5(NAMESPACE, serial) is deterministic, so the snapshot topic never changes
# for a given bar even if the linked-set file is lost.
_SESSION_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "busybar-activity-tracker/linker")


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() not in ("", "0", "false", "no")


def session_id_for(serial: str) -> str:
    """Deterministic, topic-safe session id for a serial.

    uuid5 yields only hex + dashes, so it is free of the MQTT wildcards/separators
    (``/``, ``+``, ``#``) and whitespace that would break the snapshot topic."""
    return str(uuid.uuid5(_SESSION_NAMESPACE, serial))


def serial_from_topic(topic: str) -> str | None:
    """devices/<serial>/up/v1/... → <serial> (device-scope topics)."""
    parts = topic.split("/")
    if len(parts) >= 2 and parts[0] == "devices":
        return parts[1]
    return None


def session_from_topic(topic: str) -> str | None:
    """sessions/<session_id>/up/v1/... → <session_id> (session-scope topics)."""
    parts = topic.split("/")
    if len(parts) >= 2 and parts[0] == "sessions":
        return parts[1]
    return None


# ---------------------------------------------------------------------------
# Link ledger: when we last tokened each serial (persisted, restart-safe).
# ---------------------------------------------------------------------------
class LinkLedger:
    """Per-serial record of when we last sent a link token, persisted to JSON.

    This is a *cooldown*, not a permanent "already-linked" set (see the module
    docstring for why): ``should_link`` returns False only while a serial is
    within ``cooldown_s`` of its last token, which suppresses the device's
    immediate post-link reconnect but lets a genuinely reset device re-link on
    its next presence. Persisting the timestamps means a linker restart doesn't
    needlessly re-token a device linked moments ago. The reverse (session_id→
    serial) map lets us evict on a device-initiated ``unlink`` (session scope)."""

    def __init__(self, path: Path, cooldown_s: float = DEFAULT_COOLDOWN_S):
        self.path = path
        self.cooldown_s = cooldown_s
        self.last_sent: dict[str, float] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.last_sent = {k: float(v) for k, v in data.get("last_sent", {}).items()}
        except (FileNotFoundError, ValueError):
            self.last_sent = {}

    def _save(self) -> None:
        # The session map is derivable, but persist it so an operator can read the
        # file and see the serial→session_id (→ snapshot topic) mapping directly.
        payload = {
            "cooldown_s": self.cooldown_s,
            "last_sent": self.last_sent,
            "sessions": {session_id_for(s): s for s in sorted(self.last_sent)},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.path)  # atomic swap so a crash mid-write can't corrupt it

    def __len__(self) -> int:
        return len(self.last_sent)

    def should_link(self, serial: str, now: float) -> bool:
        last = self.last_sent.get(serial)
        return last is None or (now - last) >= self.cooldown_s

    def record(self, serial: str, now: float) -> None:
        self.last_sent[serial] = now
        self._save()

    def evict(self, serial: str) -> bool:
        if serial in self.last_sent:
            del self.last_sent[serial]
            self._save()
            return True
        return False

    def evict_session(self, session_id: str) -> str | None:
        """Evict the serial that owns session_id (the id we ourselves minted), so
        its next presence re-links immediately rather than waiting out a cooldown."""
        for serial in list(self.last_sent):
            if session_id_for(serial) == session_id:
                self.evict(serial)
                return serial
        return None


def build_token(serial: str, email: str, user_id: str, token: str) -> dict:
    """The synthetic link/token payload the device stores verbatim (§6.1).

    Only the four fields need to be non-empty for the device to consider its
    saved state valid on reconnect. ``session_id`` is what ends up in the snapshot
    topic; ``email``/``user_id`` are cosmetic (shown in the device UI)."""
    return {
        "session_id": session_id_for(serial),
        "token": token,
        "email": email,
        "user_id": user_id,
    }


# ---------------------------------------------------------------------------
# Linker service (paho-mqtt threaded loop; callbacks are synchronous).
# ---------------------------------------------------------------------------
def run_linker(
    broker: str,
    port: int,
    ledger: LinkLedger,
    email: str,
    user_id: str,
    token: str,
    send_otp: bool,
    client_id: str,
    username: str | None = None,
    password: str | None = None,
) -> None:
    import paho.mqtt.client as mqtt

    def publish_token(client, serial: str, *, reason: str, with_otp: bool) -> None:
        sid = session_id_for(serial)
        # Optionally satisfy the device's manual-link UI screen (link/request path
        # only) with a dummy OTP just before the token so it resolves cleanly.
        if with_otp and send_otp:
            otp = {"code": "0000", "expires_at": int(time.time()) + 300}
            client.publish(
                f"devices/{serial}/down/v1/link/otp",
                json.dumps(otp), qos=1, retain=False,
            )
        payload = build_token(serial, email, user_id, token)
        # retain=False is mandatory: a retained token redelivers on every reconnect
        # and traps the device in a link→reconnect loop (§6.7).
        client.publish(
            f"devices/{serial}/down/v1/link/token",
            json.dumps(payload), qos=1, retain=False,
        )
        ledger.record(serial, time.time())
        print(f"  tokened {serial} ({reason}) → session_id {sid}")

    def on_connect(client, userdata, flags, reason_code, properties=None):
        if reason_code.is_failure:
            print(f"  mqtt connect failed: {reason_code}")
            return
        print(f"connected to mqtt://{broker}:{port}; watching for bars to link")
        # Wildcard subscriptions are fine here — the linker is an ordinary client;
        # the firmware's no-wildcard limitation applies only to its own subs.
        client.subscribe([(PRESENCE_TOPIC, 1), (LINK_REQUEST_TOPIC, 1), (UNLINK_TOPIC, 1)])

    def on_message(client, userdata, msg):
        topic = msg.topic
        if topic.startswith("sessions/") and topic.endswith("/unlink"):
            sid = session_from_topic(topic)
            if sid is not None:
                serial = ledger.evict_session(sid)
                if serial:
                    print(f"  unlinked {serial} (device-initiated); will re-link on next presence")
            return

        serial = serial_from_topic(topic)
        if serial is None:
            return

        # link/request is only emitted while NotLinked, so answering is always safe
        # (and lets a user force a re-link from the device UI after a factory reset).
        if topic.endswith("/link/request"):
            publish_token(client, serial, reason="link/request", with_otp=True)
            return

        # presence: token unless we did so within the cooldown (which swallows the
        # device's own post-link reconnect but still re-links a device that reset).
        if topic.endswith("/presence"):
            if not ledger.should_link(serial, time.time()):
                return
            publish_token(client, serial, reason="presence", with_otp=False)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    client.on_connect = on_connect
    client.on_message = on_message
    if username:
        client.username_pw_set(username, password)
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    print(f"linker: {len(ledger)} serial(s) known, {ledger.cooldown_s:g}s cooldown "
          f"(from {ledger.path})")
    client.connect(broker, port, keepalive=60)
    client.loop_forever(retry_first_connection=True)


# ---------------------------------------------------------------------------
# Offline self-test — validates the pure logic without a broker.
# ---------------------------------------------------------------------------
def self_test() -> int:
    import tempfile

    ok = True

    def check(name, got, want):
        nonlocal ok
        if got == want:
            print(f"  PASS  {name}")
        else:
            ok = False
            print(f"  FAIL  {name}\n        got : {got}\n        want: {want}")

    serial = "2034305532325004002d0010"

    # session_id is deterministic and topic-safe.
    sid = session_id_for(serial)
    check("session_id is stable", session_id_for(serial), sid)
    check("session_id is topic-safe", any(c in sid for c in "/+# \t"), False)

    # Topic parsing.
    check("serial from device topic",
          serial_from_topic(f"devices/{serial}/up/v1/presence"), serial)
    check("serial from session topic is None",
          serial_from_topic(f"sessions/{sid}/up/v1/unlink"), None)
    check("session from session topic",
          session_from_topic(f"sessions/{sid}/up/v1/unlink"), sid)

    # Token payload has all four required non-empty fields.
    tok = build_token(serial, "homelab@local", "homelab", "homelab-local")
    check("token fields all non-empty",
          all(tok.get(k) for k in ("session_id", "token", "email", "user_id")), True)
    check("token session_id matches", tok["session_id"], sid)

    # LinkLedger: cooldown gating, persistence round-trip, evict, evict_session.
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "state.json"
        lg = LinkLedger(path, cooldown_s=60)
        check("unseen serial should link", lg.should_link(serial, now=1000.0), True)
        lg.record(serial, now=1000.0)
        # Within the cooldown → suppress the device's post-link reconnect.
        check("within cooldown does NOT re-link", lg.should_link(serial, now=1030.0), False)
        # Past the cooldown → re-link a device that reset its saved state.
        check("past cooldown re-links (self-heal)", lg.should_link(serial, now=1061.0), True)
        # Reload from disk → timestamps survive a linker restart.
        lg2 = LinkLedger(path, cooldown_s=60)
        check("persisted across reload", lg2.should_link(serial, now=1030.0), False)
        # Evict by session id (device-initiated unlink) → immediate re-link.
        evicted = lg2.evict_session(sid)
        check("evict_session returns owning serial", evicted, serial)
        check("after evict, re-links immediately", lg2.should_link(serial, now=1030.0), True)
        check("evict of absent serial returns False", lg2.evict(serial), False)

    print("\nself-test:", "ALL PASS" if ok else "FAILURES")
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--mqtt", metavar="HOST[:PORT]",
        default=os.environ.get("MQTT_BROKER", "mosquitto:1883"),
        help="broker to connect to (also MQTT_BROKER; default port 1883)",
    )
    p.add_argument(
        "--state", default=os.environ.get("LINKER_STATE", "/data/linker_state.json"),
        help="persisted linked-serial set (also LINKER_STATE)",
    )
    p.add_argument("--email", default=os.environ.get("LINK_EMAIL", "homelab@local"),
                   help="cosmetic 'linked account' email stored on the device")
    p.add_argument("--user-id", default=os.environ.get("LINK_USER_ID", "homelab"),
                   help="cosmetic user_id stored on the device")
    p.add_argument("--token", default=os.environ.get("LINK_TOKEN", "homelab-local"),
                   help="non-empty token (becomes the device's MQTT password)")
    p.add_argument("--cooldown", type=float,
                   default=float(os.environ.get("LINKER_COOLDOWN_S", DEFAULT_COOLDOWN_S)),
                   help=f"seconds before re-tokening the same serial on presence "
                        f"(also LINKER_COOLDOWN_S; default {DEFAULT_COOLDOWN_S})")
    p.add_argument("--no-otp", action="store_true", default=_env_flag("LINKER_NO_OTP"),
                   help="don't emit a dummy link/otp on the link/request path")
    p.add_argument("--client-id", default=os.environ.get("LINKER_CLIENT_ID", "busybar-linker"),
                   help="MQTT client id for the linker")
    p.add_argument("--mqtt-username", default=os.environ.get("MQTT_USERNAME"),
                   help="broker username (if the broker requires auth)")
    p.add_argument("--mqtt-password", default=os.environ.get("MQTT_PASSWORD"),
                   help="broker password")
    p.add_argument("--self-test", action="store_true", help="run offline logic checks and exit")
    args = p.parse_args()

    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    if args.self_test:
        return self_test()

    host, _, port = args.mqtt.partition(":")
    ledger = LinkLedger(Path(args.state), cooldown_s=args.cooldown)
    run_linker(
        host, int(port or 1883), ledger,
        email=args.email, user_id=args.user_id, token=args.token,
        send_otp=not args.no_otp, client_id=args.client_id,
        username=args.mqtt_username, password=args.mqtt_password,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

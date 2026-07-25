# Homelab Integration via MQTT

How to move activity recording off a USB-tethered laptop and onto an always-on
homelab box, using the device's built-in MQTT publishing instead of the
USB/WiFi WebSocket. This is "Tier 2" from the network discussion — more setup
than pointing the WS recorder at the bar's WiFi IP, but strictly more robust for
unattended, always-on operation.

---

## 1. Why MQTT (vs. the WebSocket)

The WebSocket (`/api/status/ws`) is **live-only**: the device streams events to
whoever is currently connected, with no replay. A disconnected client misses
everything during the gap, and the device keeps no session history to backfill.
The recorder's `truncated`-on-disconnect logic bounds the damage but cannot
recover the lost window.

MQTT inverts the coupling:

- The bar **publishes** each timer snapshot to a broker **independently of any
  client**, at **QoS 1** (at-least-once).
- The broker **retains / buffers** messages. A subscriber with a **persistent
  session** receives QoS-1 messages queued while it was briefly offline.
- The recorder becomes a stateless consumer it can restart freely.

Net effect: the "steady connection required" constraint largely goes away. The
device→broker link (WiFi) is what must stay up; the recorder→broker link is
local and cheap.

### The key network insight (unchanged)

Your homelab does **not** need WiFi. Put the **bar** on WiFi; the broker and
recorder run on the wired homelab; the router bridges the two. Everything is one
routable LAN.

```
   Busy Bar ──WiFi──► Router ──Ethernet──► Homelab box
   (publishes             (bridges)          ├── Mosquitto broker  (:1883)
    busy/snapshot)                           └── activity_recorder (subscribes)
                                                     └── activity_log.jsonl
```

---

## 2. Confirmed firmware facts (verified in source)

| Fact | Value | Source |
|------|-------|--------|
| Snapshot topic | `busy/snapshot` | `busy_timer.c:14-15` (`TIMER_MQTT_PREFIX "busy"`) |
| Profile topics | `busy/profiles/custom`, `busy/profiles/busy` | `busy_timer.c:16-18` |
| QoS | 1 (at-least-once) | `busy_timer.c:20-21` |
| Payload | **raw snapshot JSON**, identical to `/api/busy/snapshot` and the WS (no protobuf wrapper, no base64) | `busy_timer.c:492-500` publishes `busy_timer_snapshot_serialize()` output |
| Broker configurable | yes — custom `mqtt://` / `mqtts://` accepted | `mqtt_config.c:111-113` |
| Config API | `POST /api/account/backend` (`server_url`, `client_cert_type`, `ignore_server_cert`) | openapi `AccountBackend` |

The single most useful consequence: **the recorder's `SessionTracker` does not
change at all.** The MQTT payload is the same JSON the tracker already consumes;
only the *transport* is swapped. And it's actually simpler than the WS path — no
`_decode_json_wrapper` / protobuf decode; just `json.loads(payload)`.

> ⚠️ **Verify the exact topic on first connect.** The publish topic is literally
> `busy/snapshot` in firmware, but confirm nothing namespaces it per-device at
> your broker: `mosquitto_sub -h <broker> -t '#' -v` and watch what arrives while
> you start/stop a timer. Adjust the subscribe topic if needed.

---

## 3. Device-side setup

1. **Join WiFi.** On the bar, connect to your home WiFi (Settings → WiFi, or the
   `/api/wifi/connect` endpoint). Give it a **DHCP reservation** on the router
   keyed to its WiFi MAC `0c:fa:22:00:b0:69` so its IP is stable.
2. **Keep it powered.** Leave the bar on USB power so it doesn't enter low-power
   and drop its radio / web server.
3. **Point it at your broker.** With the bar reachable (over WiFi or still USB):

   ```bash
   curl -s -X POST http://<bar-ip>/api/account/backend \
     -H 'Content-Type: application/json' \
     -d '{"server_url":"mqtt://<homelab-ip>:1883","ignore_server_cert":true}'
   ```

   Then confirm:

   ```bash
   curl -s http://<bar-ip>/api/account/backend      # echoes server_url
   curl -s http://<bar-ip>/api/account/status       # MQTT link/connection status
   ```

### 3.1 TLS / auth caveat (the one real unknown)

The device's MQTT stack was built for the vendor's **mTLS** cloud. A plain
`mqtt://` LAN broker is *accepted by config validation*, but whether the client
insists on TLS/client-certs at connect time is the one thing not provable from a
read of the source. Two fallbacks if plain `mqtt://` won't connect:

- Run the broker with TLS (`mqtts://<homelab-ip>:8883`) using a self-signed cert
  and set `"ignore_server_cert": true`. Provide a client cert via the
  provisioning path (`scripts/mqtt_provision.py`) if the client requires one.
- Confirm the link with `/api/account/status` and the broker log before wiring up
  the recorder. **Prove the device connects and `busy/snapshot` messages land in
  `mosquitto_sub` before writing any recorder code** — that de-risks everything
  downstream.

---

## 4. Broker setup (Mosquitto)

Minimal `mosquitto.conf` for a trusted home LAN (plain, no auth — tighten later):

```conf
listener 1883 0.0.0.0
allow_anonymous true
persistence true
persistence_location /mosquitto/data/
```

For a persistent subscriber to receive messages buffered during downtime, the
**subscriber** (recorder) must use a fixed `client_id` and `clean_session=false`
and subscribe at QoS 1. The broker then queues QoS-1 messages for that client
while it is away. (Optionally harden later with `allow_anonymous false` + a
password file, and/or TLS per §3.1.)

---

## 5. Recorder code changes

The design goal: **add an MQTT transport, reuse everything else.** `SessionTracker`,
`SessionWriter`, `derive_state`, the monotonic guard, and the truncation logic all
stay exactly as they are.

### 5.1 Dependency

```bash
pip install paho-mqtt
```

### 5.2 New transport function

Add alongside `run_stream` in `activity_recorder.py`. paho's threaded loop calls
`on_message` for each snapshot; the tracker/writer are synchronous, so no asyncio
is needed on this path.

```python
def run_mqtt(broker, port, topic, tracker, writer, raw_log, client_id):
    import paho.mqtt.client as mqtt

    state = {"last_ts_ms": -1}  # persists across reconnects within this process

    def on_connect(client, userdata, flags, rc, properties=None):
        print(f"mqtt connected rc={rc}; subscribing {topic}")
        client.subscribe(topic, qos=1)

    def on_disconnect(client, userdata, rc, properties=None):
        # Bound the damage exactly like the WS path: close any open session at the
        # last snapshot we actually saw rather than across the outage.
        for record in tracker.close_open(state["last_ts_ms"]):
            writer.write(record)
        print(f"mqtt disconnected rc={rc}; paho will auto-reconnect")

    def on_message(client, userdata, msg):
        try:
            parsed = json.loads(msg.payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        snap = parsed.get("snapshot", parsed)
        ts_ms = int(parsed.get("snapshot_timestamp_ms", 0))
        if ts_ms <= state["last_ts_ms"]:
            return  # stale/duplicate (QoS1 can redeliver) — ignore
        state["last_ts_ms"] = ts_ms
        if raw_log is not None:
            with raw_log.open("a", encoding="utf-8") as f:
                f.write(json.dumps(parsed, separators=(",", ":")) + "\n")
        for record in tracker.on_snapshot(snap, ts_ms):
            writer.write(record)

    # Durable session so the broker buffers QoS-1 messages across recorder restarts.
    client = mqtt.Client(client_id=client_id, clean_session=False)
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    client.connect(broker, port, keepalive=60)
    client.loop_forever(retry_first_connection=True)
```

Notes:
- The **monotonic `last_ts_ms` guard is essential here** — QoS 1 is at-*least*-once,
  so the same snapshot can be redelivered; dropping non-newer timestamps dedupes it.
- `close_open()` on disconnect is the same disconnect-resilience already built; with
  a durable session most drops won't even lose messages, so `truncated` records
  become rare.

### 5.3 CLI wiring

In `main()`, add a broker option and branch the transport:

```python
p.add_argument("--mqtt", metavar="HOST[:PORT]",
               help="record from an MQTT broker instead of the WebSocket")
p.add_argument("--mqtt-topic", default="busy/snapshot")
p.add_argument("--mqtt-client-id", default="busybar-recorder")
```

```python
if args.mqtt:
    host, _, port = args.mqtt.partition(":")
    run_mqtt(host, int(port or 1883), args.mqtt_topic,
             tracker, writer, raw_log, args.mqtt_client_id)
else:
    asyncio.run(run_stream(args.addr, args.token, tracker, writer, raw_log))
```

Everything the tracker produces — `activity`/`phase`/`start`/`end`/`duration_s`/
`card_id`/`partial`/`truncated`/`overtime_s`, and `--flow-overtime` semantics — is
identical to the WS mode. The `--self-test` suite still covers the session logic
unchanged.

---

## 6. Containerize the homelab service (recommended)

Run the recorder (and optionally the broker) as containers rather than a bare
process on the host. Why this is the right call here:

- **Reproducible dependencies.** `paho-mqtt` (and `busylib` if you keep WS mode)
  are pinned in the image, not smeared across the host's Python. No "works on my
  laptop" drift when the homelab OS updates.
- **Restart policy = durability.** `restart: unless-stopped` gives you crash and
  reboot recovery for free — exactly what an always-on recorder needs — layered
  on top of paho's own reconnect.
- **Isolation & portability.** The service moves between homelab hosts as a unit;
  the JSONL log and config are explicit named volumes, so state is obvious and
  backup-able.
- **Clean upgrades.** Rebuild the image to update the recorder; roll back by
  re-tagging. No touching the host.

### 6.1 `Dockerfile` (recorder)

```dockerfile
FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir paho-mqtt
COPY activity_recorder.py busy_probe.py activity_card_id_map.json ./
# busy_probe.py is only needed for WS mode; harmless to include.
ENTRYPOINT ["python", "activity_recorder.py"]
```

### 6.2 `docker-compose.yml` (broker + recorder)

```yaml
services:
  mosquitto:
    image: eclipse-mosquitto:2
    restart: unless-stopped
    ports:
      - "1883:1883"
    volumes:
      - ./mosquitto.conf:/mosquitto/config/mosquitto.conf:ro
      - mosquitto-data:/mosquitto/data

  recorder:
    build: .
    restart: unless-stopped
    depends_on:
      - mosquitto
    command: >
      --mqtt mosquitto:1883
      --flow-overtime
      --out /data/activity_log.jsonl
    volumes:
      - ./data:/data          # activity_log.jsonl lands here on the host

volumes:
  mosquitto-data:
```

`--out /data/activity_log.jsonl` writes the log to the bind-mounted `./data`, so
it survives container rebuilds and is easy to back up. The `activity_card_id_map.json`
baked into the image can instead be mounted (`./activity_card_id_map.json:/app/activity_card_id_map.json:ro`)
if you want to update mappings without rebuilding.

Bring it up:

```bash
docker compose up -d --build
docker compose logs -f recorder      # watch sessions land
```

Point the bar at `mqtt://<homelab-ip>:1883` (§3). Note the bar reaches the broker
at the **host's** LAN IP and published port, not the compose service name.

---

## 7. Cutover checklist

1. Broker up; `mosquitto_sub -h <homelab> -t '#' -v` shows `busy/snapshot`
   arriving when you run a timer on the bar. **(Proves §3.1 before any code.)**
2. Recorder container up; `docker compose logs recorder` shows sessions logged.
3. Run the WS recorder and the MQTT recorder in parallel for a day and diff their
   JSONL — they should agree on completed sessions. Then retire the WS/USB path.
4. Back up `./data/activity_log.jsonl` (and put it under your normal homelab
   backup).

---

## 8. What stays the same

- `activity_card_id_map.json` — unchanged; the join key is still `card_id`.
- `SessionTracker` and its `--self-test` — unchanged; only the transport differs.
- `--flow-overtime`, `truncated`, `partial`, and all record fields — unchanged.
- The firmware — **no change required**; MQTT publishing already exists, it just
  needs a broker to point at.

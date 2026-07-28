# Homelab Integration via MQTT

How to move activity recording off a USB-tethered laptop and onto an always-on
homelab box, using the device's MQTT publishing instead of the USB/WiFi
WebSocket.

> **Status (2026-07-27): investigation complete, ready to implement.**
> Live testing on the homelab against the real device disproved the original
> premise of this document. A bare MQTT broker does **not** receive timer
> snapshots: on firmware **r909 (api_version 25.0.0)** snapshot publishing is
> **gated on the device being *linked* to an account**, and the link is a
> handshake the broker's operator must complete. The good news — the handshake
> is **completely unvalidated**, so we can complete it ourselves on the homelab
> with a tiny "linker" service and no firmware changes. That is the plan in
> **§6 (Path A)**. A firmware-patch alternative is **§7 (Path B)**.
>
> This document has been rewritten to reflect what we verified in source and on
> the wire. Sections that were wrong (flat `busy/snapshot` topic, "publishes
> independently of any client") are corrected below. Source line references are
> in the **Appendix**; they were read from `../busybar-firmware` on branch
> `activity-selection`, which matches the firmware currently on the bar.

---

## 1. Executive summary of findings

| # | Finding | Consequence |
|---|---------|-------------|
| 1 | Plain `mqtt://` (no TLS, anonymous) **connects fine**. The device published its presence beacon to our Mosquitto broker immediately. | The §3.1 TLS/mTLS unknown is **resolved**: no TLS, no client certs needed. |
| 2 | Across a full 1-minute timer run, the broker received **only** `devices/<serial>/up/v1/presence` and **zero** snapshots. | A bare broker is not enough. Something gates snapshots. |
| 3 | Snapshots are published on the **session scope**, which is only permitted when the device status is `ConnectedLinked`. Our device is `ConnectedNotLinked`, so every snapshot is dropped **on the device** before it hits the wire. | The migration hinges on getting the device into the *linked* state. |
| 4 | All MQTT topics are namespaced `root/<id>/dir/v1/topic`. The real snapshot topic is `sessions/<session_id>/up/v1/busy/snapshot`, **not** `busy/snapshot`. | The recorder must subscribe to the prefixed topic. |
| 5 | The account-link handshake performs **no validation**: the device accepts any JSON on `devices/<serial>/down/v1/link/token` containing `session_id`/`token`/`email`/`user_id`, stores it, and flips itself to `ConnectedLinked`. | We can complete the link ourselves against our own broker — no cloud, no firmware change. |
| 6 | The device's HTTP config API (`/api/account/backend`) is reachable **only over USB**, not over the WiFi IP. The working call is **`PUT`** (not `POST`) and must include `client_cert_type:"none"`. | Device pointing must be done from the USB-tethered laptop; the doc's old `POST` example was wrong. |

Net effect: the migration is viable and still needs no vendor cloud, but the
architecture is "**recorder + linker + broker**", not "recorder + broker".

---

## 2. The key network insight (unchanged, still correct)

The homelab does **not** need WiFi. Put the **bar** on WiFi; the broker,
recorder, and linker run on the wired homelab; the router bridges the two.
Everything is one routable LAN.

```
   Busy Bar ──WiFi──► Router ──Ethernet──► Homelab box
   (publishes             (bridges)          ├── Mosquitto broker   (:1883)
    presence,                                 ├── linker (completes the account link)
    then snapshots                            └── activity_recorder (subscribes)
    once linked)                                     └── activity_log.jsonl
```

### As tested

| Thing | Value |
|-------|-------|
| Homelab LAN IP (wired) | `192.168.0.47` |
| Bar WiFi IP (static/reserved) | `192.168.0.243` |
| Bar WiFi MAC | `0c:fa:22:00:b0:69` |
| Bar device serial | `2034305532325004002d0010` |
| Broker | `eclipse-mosquitto:2` (2.1.2), anonymous, persistence on |
| Firewall | `ufw` active; opened 1883 from the bar IP only |

The bar is pingable from the homelab (~30 ms, the WiFi hop). Its HTTP API is
**not** reachable over `192.168.0.243` — only over the USB virtual-LAN address
from the laptop.

---

## 3. How the device's MQTT actually works (verified in source)

### 3.1 Topic format

Every topic is built by `mqtt_make_topic_path()` as:

```
<root>/<id>/<direction>/v1/<app-topic>
```

- `root` = `devices` (device scope) or `sessions` (session scope)
- `id` = the device serial (device scope) or the `session_id` (session scope)
- `direction` = `up` (device→broker) or `down` (broker→device)
- `v1` = API version
- `app-topic` = the topic the application code passes (e.g. `busy/snapshot`)

Examples:
- Presence: `devices/2034305532325004002d0010/up/v1/presence`
- Snapshot (once linked): `sessions/<session_id>/up/v1/busy/snapshot`
- Link token (broker→device): `devices/<serial>/down/v1/link/token`

### 3.2 Status states and the publish gate

The MQTT service has four states: `Error`, `NotConnected`,
`ConnectedNotLinked`, `ConnectedLinked`. Publishing/subscribing is scope-gated:

| Scope | Allowed when… |
|-------|---------------|
| **Device** (`devices/<serial>/…`) | `ConnectedNotLinked` **or** `ConnectedLinked` |
| **Session** (`sessions/<session_id>/…`) | `ConnectedLinked` **only** |

Timer snapshots (`busy_timer.c`) are published through the public
`mqtt_publish()`, which is hard-wired to **session scope**. Therefore, while the
device is `ConnectedNotLinked`, every snapshot publish fails the scope check and
is dropped on-device with `Unable to publish` — nothing is sent. This is exactly
what we observed.

What *does* publish while `NotLinked` (device scope): the `presence` beacon and
the `link/request` message.

### 3.3 Connection parameters (observed + source)

The device connects as: MQTT **v5**, **clean session = true**, keepalive ~600 s,
`client_id` = a device-generated `busybar-<random>` (persisted), username =
`"BusyBar device <serial>"`, password = the stored link `token` (empty until
linked). Presence is also registered as the MQTT **last-will** on the device
topic. Our broker runs `allow_anonymous true`, so username/password are accepted
regardless of value.

### 3.4 The account-link handshake — and why it's completable by us

On every connect the device (device scope, allowed while `NotLinked`) subscribes
to:
- `devices/<serial>/down/v1/link/otp` — a PIN/OTP for the *normal* app flow
  (drives a UI screen; **not required** for our purposes).
- `devices/<serial>/down/v1/link/token` — the link completion.

The **token callback does no validation whatsoever**. If it receives JSON on
`link/token` containing non-empty `session_id`, `token`, `email`, and `user_id`,
it stores them, persists them, marks itself linked, and closes the connection to
reconnect. On the reconnect, `mqtt_saved_state_is_valid()` (requires `client_id`
+ those four fields all non-empty; `client_id` is already device-generated) is
true, so the device comes up `ConnectedLinked` and begins publishing snapshots to
`sessions/<session_id>/up/v1/busy/snapshot`, where `session_id` is **whatever we
put in the token**.

The normal flow (`link/request` → cloud issues `link/otp` PIN → user confirms in
app → cloud issues `link/token`) exists only so the vendor cloud can associate a
real user. Since we own the broker, we skip straight to publishing a synthetic
`link/token`.

Two facts that make this safe and loop-free to automate:
- The device re-publishes `presence` on **every** connect, including after it
  becomes linked. So the linker must **not** re-send a token on every presence,
  or it will force an endless reconnect loop. Track already-linked serials.
- The token must be published **non-retained**. A retained `link/token` would be
  redelivered on every device reconnect → reconnect loop.

---

## 4. Broker setup (Mosquitto) — unchanged, already deployed

`broker/mosquitto.conf` is correct as-is for this plan (plain, anonymous,
persistent, unlimited QoS-1 queue for the durable recorder). No TLS block is
needed (finding #1). It is already running on the homelab and was loopback-
verified. For reference, the effective config:

```conf
listener 1883 0.0.0.0
allow_anonymous true
persistence true
persistence_location /mosquitto/data/
max_queued_messages 0
persistent_client_expiration 7d
```

Security note: with `allow_anonymous true` the broker (and the linker in §6)
will accept/serve **any** device that connects. Keep the scope tight with the
`ufw` rule that only allows the bar's IP inbound to 1883 (see §8). Harden with
a password file + TLS later if the broker is ever exposed beyond the LAN.

---

## 5. Recorder — one change needed

The `SessionTracker`, `SessionWriter`, `derive_state`, monotonic `last_ts_ms`
dedupe guard, `--flow-overtime`, and all record fields stay **exactly as they
are**. The MQTT payload is the same snapshot JSON the tracker already consumes.

The **only** change: the subscribe topic must move from the flat `busy/snapshot`
to the real session-scoped topic. Use a single-level wildcard on the session id
so re-links don't break it:

```
sessions/+/up/v1/busy/snapshot
```

Concretely: change the `MQTT_TOPIC` default (env + `.env` + compose) and the
`--mqtt-topic` default in `activity_recorder.py` from `busy/snapshot` to
`sessions/+/up/v1/busy/snapshot`. `run_mqtt` already subscribes at QoS 1 with a
durable session, dedupes redeliveries, and closes open sessions on disconnect —
none of that changes. (`+` matches exactly one topic level, so it will not catch
`busy/profiles/*`, which the recorder ignores anyway.)

---

## 6. Path A (recommended) — complete the link on the homelab

Add a small **linker** service to the homelab stack. It plays the role of the
vendor cloud just far enough to move the device into `ConnectedLinked`, after
which the firmware publishes snapshots on its own. No firmware changes.

### 6.1 Linker service — behavioral spec

A long-running MQTT client (e.g. a ~100-line Python script using `paho-mqtt`,
same dependency the recorder already pins). It:

1. **Connects** to the broker (anonymous is fine; in-network at `mosquitto:1883`).
2. **Subscribes** (QoS 1) to:
   - `devices/+/up/v1/presence`
   - `devices/+/up/v1/link/request`
   - *(optional, for robustness)* `sessions/+/up/v1/unlink` — the device
     publishes here when a user unlinks it from the device UI; use it to evict a
     serial from the linked-set so it can be re-linked automatically.

   (The **linker** is an ordinary MQTT client, so wildcard subscriptions are
   fine — the "no wildcard" limitation applies only to the firmware's own
   subscriptions.)
3. **Maintains a persisted per-serial cooldown** in a small JSON file on the
   `/data` volume — the wall-clock time each serial was last tokened (survives
   linker restarts, so a just-linked bar isn't needlessly re-tokened on restart).
   This is deliberately a *cooldown*, not a permanent "already-linked" set: a bar
   that resets its saved link state on reboot re-emits `presence` with no
   `unlink`, and a permanent set would strand it `NotLinked` forever. The cooldown
   lets the linker self-heal — see §6.7. *(Implemented: this was originally a
   permanent set; changed to a cooldown after live testing showed a power-cycle
   can reset the device's link state.)*
4. **On `link/request` from a serial** → always publish a token (a `link/request`
   is only emitted while `NotLinked`, so this is always safe). Record the time.
   **On `presence` from a serial last tokened longer ago than the cooldown (or
   never)** → publish a token, record the time.
   **On `presence` within the cooldown** → ignore (this swallows the device's own
   post-link reconnect, preventing the reconnect loop).
5. **Publishes `link/token`** to `devices/<serial>/down/v1/link/token`, **QoS 1,
   retain = false**, payload:

   ```json
   {
     "session_id": "<stable-id-derived-from-serial>",
     "token": "homelab-local",
     "email": "homelab@local",
     "user_id": "homelab"
   }
   ```

   - `session_id` — must be **stable per serial** and **topic-safe** (no `/`,
     `+`, `#`, no whitespace). Derive it deterministically, e.g.
     `uuid5(NAMESPACE_URL, serial)`, so the snapshot topic never changes across
     re-links. The linker should log the serial→session_id mapping.
   - `token` — any non-empty string. Becomes the device's MQTT password on
     reconnect; irrelevant to an anonymous broker but must be non-empty.
   - `email`, `user_id` — any non-empty strings. Stored and shown in the device
     UI as the "linked account"; cosmetic.
   - *(Optional)* On the `link/request` path only, publish a dummy
     `devices/<serial>/down/v1/link/otp` `{"code":"0000","expires_at":<epoch+300>}`
     just before the token so the device's manual-link UI screen resolves
     cleanly. Not needed on the presence (zero-touch) path.

After the device consumes the token it closes, reconnects `ConnectedLinked`, and
starts publishing `sessions/<session_id>/up/v1/busy/snapshot`.

### 6.2 Recorder change

As in §5: subscribe to `sessions/+/up/v1/busy/snapshot`.

### 6.3 Compose + env

Add a `linker` service alongside `mosquitto`, `recorder`, `dashboard`:

```yaml
  linker:
    build: ./backend            # reuse the backend image (has paho-mqtt), new entrypoint
    restart: unless-stopped
    depends_on:
      - mosquitto
    environment:
      MQTT_BROKER: ${MQTT_BROKER:-mosquitto:1883}
      LINKER_STATE: /data/linker_state.json
      LINK_EMAIL: homelab@local
      LINK_USER_ID: homelab
    volumes:
      - ./data:/data
    command: ["python", "mqtt_linker.py"]   # new script in backend/
```

`.env` / compose recorder change:

```ini
# was: MQTT_TOPIC=busy/snapshot
MQTT_TOPIC=sessions/+/up/v1/busy/snapshot
```

Keep the recorder's `MQTT_CLIENT_ID` stable (durable subscriber) as today.

### 6.4 Device-side config (one-time, from the USB laptop)

The HTTP API is USB-only. From the tethered laptop, point the bar at the homelab
broker. **Use `PUT`, and include `client_cert_type:"none"`** (this is the call
that actually worked; the old `POST` example did not):

```bash
curl -s -X PUT http://<bar-usb-ip>/api/account/backend \
  -H 'Content-Type: application/json' \
  -d '{"server_url":"mqtt://192.168.0.47:1883","client_cert_type":"none","ignore_server_cert":true}'

curl -s http://<bar-usb-ip>/api/account/backend   # GET echoes server_url back
curl -s http://<bar-usb-ip>/api/account/status    # shows link/connection status
```

`<bar-usb-ip>` is the USB virtual-LAN address (repo default historically
`10.0.4.20`), **not** the WiFi IP. `server_url` uses the homelab **LAN IP**
(`192.168.0.47`), the host's published port — not the `mosquitto` compose service
name.

### 6.5 Bootstrapping the already-connected device

The device is currently sitting `ConnectedNotLinked` and already emitted its
presence before the linker existed; it will not re-announce until it reconnects.
To trigger a reconnect (and thus a fresh `presence` the linker will catch),
re-issue the `PUT /api/account/backend` from the laptop — setting config calls
`mqtt_connection_close(reconnect)` — or power-cycle the bar. After that the
linker links it automatically.

### 6.6 Verification (do these in order)

1. Stack up: `docker compose up -d --build` (broker, linker, recorder, dashboard).
2. Trigger a device reconnect (§6.5). In the linker logs you should see the
   presence, the token publish, and the serial→session_id mapping.
3. `/api/account/status` on the laptop will report `{"status":"connected"}` — but
   **note this does NOT confirm linking**. That endpoint returns `"connected"`
   for *both* `ConnectedNotLinked` and `ConnectedLinked` (`api_account.c` only
   distinguishes error / disconnected / connected). The real proof of the linked
   state is seeing a **session-scope** publish on the wire — either
   `sessions/<session_id>/up/v1/state` (sent on connect once linked) or the
   snapshots in step 4. Don't treat "connected" as done.
4. Watch snapshots land:
   `docker compose exec mosquitto mosquitto_sub -t 'sessions/+/up/v1/busy/snapshot' -v`
   then start/stop a timer on the bar.
5. `docker compose logs -f recorder` — completed sessions should append to
   `./data/activity_log.jsonl`.

### 6.7 Edge cases & operational notes (implementers must handle)

- **Re-link loop avoidance** is mandatory: rate-limit tokens with the per-serial
  cooldown (persisted) and publish the token **non-retained**. Skipping either
  causes an endless link→reconnect→presence→link loop. The cooldown must be
  longer than the device's post-link reconnect latency (~1–2 s); the 60 s default
  has wide margin.
- **Device-side unlink / factory reset / reboot** clears the device's saved state
  → it returns `NotLinked`, regenerates its `client_id`, and re-emits `presence`
  with no `unlink`. **This was observed live**: a power-cycle reset the link, and
  under the original permanent-set design the linker ignored the presence and the
  bar was stranded `NotLinked` (linked-status endpoint still said "connected", so
  it was silent — snapshots just stopped). The cooldown design recovers
  automatically: once the cooldown elapses, the next `presence` re-links the bar.
  Belt-and-suspenders: (a) the linker always answers `link/request` (force a
  re-link from the device UI), and (b) it subscribes to `sessions/+/up/v1/unlink`
  to evict immediately on a device-initiated unlink. A manual state-file wipe
  (`docker compose exec -T recorder rm -f /data/linker_state.json`; the file is
  root-owned) still works as a last resort.
- **Multiple devices**: everything is keyed by serial and uses wildcards, so N
  bars work without change.
- **Broker credential drift**: if a device ever fails to subscribe with
  `NotAuthorized` while linked, the firmware resets its own saved state and
  unlinks. With `allow_anonymous true` this cannot happen, but keep it in mind if
  auth is added later — a re-link via the linker recovers it.
- **Security**: the linker will link any device that connects. Rely on the §8
  `ufw` rule scoping inbound 1883 to the bar's IP. If the broker is ever exposed,
  add broker auth and restrict the linker accordingly.

---

## 7. Path B (alternative / possible follow-up) — patch the firmware

If we prefer a bare broker with **no linker component** and a stock-broker
architecture, patch the firmware so timer snapshots publish on the **device
scope** (which is allowed while `NotLinked`) instead of the session scope. The
effective topic becomes `devices/<serial>/up/v1/busy/snapshot`, published as soon
as the device connects — no account link required.

- **Change area**: the snapshot (and, if desired, profile) publish path in
  `applications/services/busy_timer/busy_timer.c` currently calls the public
  `mqtt_publish()`, which routes to `MqttScopeSession`. Provide/route it through
  a **device-scope** publish instead (e.g. a new
  `mqtt_publish_device()` public API wrapping `mqtt_publish_internal(...,
  MqttScopeDevice, ...)`, or an equivalent). Do **not** globally relax the scope
  gate — keep the change scoped to the snapshot publish so the rest of the
  account/session protocol is untouched.
- **Recorder change**: subscribe to `devices/+/up/v1/busy/snapshot` instead of
  the `sessions/...` topic.
- **Trade-offs**: cleanest end-state (bare broker, no fake-cloud, device never
  shows a fake "linked account"), but requires the firmware build toolchain and
  flashing, must be re-applied after any vendor firmware update, and diverges
  from stock firmware.

Recommendation: ship **Path A** first (non-destructive, reversible, no flashing,
and we have proven the device accepts unvalidated tokens). Evaluate Path B later
only if we want to retire the linker for a cleaner permanent architecture.

---

## 8. Homelab / network checklist (environment-specific, done by hand)

1. **Bar → WiFi with a DHCP reservation** keyed to MAC `0c:fa:22:00:b0:69`.
   Done — static `192.168.0.243`. Leave the bar on USB **power** so its radio /
   web server don't sleep.
2. **Open inbound TCP 1883 from the bar's IP only** on the homelab. `ufw` is
   active; the rule is:
   `sudo ufw allow from 192.168.0.243 to any port 1883 proto tcp comment 'busy bar MQTT'`
   (Done.) This is the real posture change — the **bar dials in** to the broker.
3. **Point the device at the broker** (one-time, from the USB laptop) per §6.4 —
   `PUT`, with `client_cert_type:"none"`.
4. **Bring up the stack & keep it running**: `cp .env.example .env` (adjust `TZ`),
   set `MQTT_TOPIC=sessions/+/up/v1/busy/snapshot`, then
   `docker compose up -d --build`. `restart: unless-stopped` on all services
   gives reboot/crash recovery.
5. **Verify** per §6.6. Back up `./data/activity_log.jsonl` under your normal
   homelab backup.

---

## 9. Cutover checklist

1. Broker up (done) and loopback-verified (done).
2. Firewall opened to the bar's IP (done).
3. Linker + recorder up. Device pointed at the broker (§6.4) and reconnected
   (§6.5); `/api/account/status` shows **linked**.
4. `mosquitto_sub -t 'sessions/+/up/v1/busy/snapshot' -v` shows snapshots while a
   timer runs. **(This is the gate that proves the whole path.)**
5. `docker compose logs recorder` shows sessions logged to
   `./data/activity_log.jsonl`.
6. Optionally parallel-run the old USB/WS recorder for a day and diff the two
   JSONLs before retiring it. Add `./data/activity_log.jsonl` to backups.

---

## 10. What stays the same

- `activity_card_id_map.json` — unchanged; the join key is still `card_id`.
- `SessionTracker` and its `--self-test` — unchanged; only the transport and the
  subscribe topic differ.
- `--flow-overtime`, `truncated`, `partial`, `overtime_s`, and all record fields
  — unchanged.
- The snapshot **payload** — unchanged; it is the same JSON the WS path consumes,
  so `run_mqtt`'s parsing and the monotonic dedupe guard are untouched.

---

## Appendix — source references (firmware, branch `activity-selection`)

Read from `../busybar-firmware`. Line numbers are approximate but current.

| What | Where |
|------|-------|
| Snapshot topic `busy/snapshot`, QoS 1 (`MqttQosAtLeastOnce`) | `applications/services/busy_timer/busy_timer.c:14-21` |
| Snapshot published via `mqtt_publish()` (public → session scope) | `busy_timer.c:511-525` |
| Topic roots `devices` / `sessions`; app topics `presence`, `link/otp`, `link/token`, `link/request`, `unlink`, `gone` | `applications/services/mqtt/mqtt_i.h:23-37` |
| Topic builder `root/id/dir/v1/topic` | `applications/services/mqtt/mqtt_subscription.c:36-57` |
| Scope gate (session ⇒ `ConnectedLinked` only; device ⇒ either) | `mqtt_subscription.c:19-34` |
| Publish drops when scope invalid (`Unable to publish`) | `mqtt_subscription.c:124-127` |
| `mqtt_publish()` public API → session scope | `applications/services/mqtt/mqtt_api.c:335-347` |
| `link/request` sent only while `NotLinked` (device scope, QoS2) | `mqtt_api.c:257-280` |
| Link-token callback — **no validation**; stores + relinks | `applications/services/mqtt/mqtt_account.c:30-66` |
| Link subscriptions set up on connect (otp, token; `gone`) | `mqtt_account.c:78-102` |
| Connect: valid saved-state ⇒ `ConnectedLinked` else `NotLinked`; presence re-sent every connect | `applications/services/mqtt/mqtt_connection.c:223-246` |
| Presence is device-scope + registered as last-will; conn params (v5, clean, keepalive, username, password=token, client_id) | `mqtt_connection.c:181-209, 383-405` |
| Saved-state validity (client_id+session_id+user_id+email+token non-empty) | `applications/services/mqtt/settings/mqtt_saved_state_interface_v1.c:3-7` |
| `client_id` device-generated `busybar-<random>` on reset | `applications/services/mqtt/mqtt.c` (`mqtt_reset_saved_state`) |
</content>

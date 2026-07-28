# Busy Bar Activity Tracker — Host Tooling

Passive, host-side tooling for personal activity time-tracking on the
[Busy Bar](https://busy.app), paired with on-device activity-selection firmware
(kept in a separate fork — see below).

Selection happens entirely on the device: turn the wheel to a custom activity,
press to start. This tooling never participates in selection — it is a **passive
recorder** that consumes the device's timer snapshots (over MQTT) and writes a
per-activity session log, plus a dashboard that visualizes that log.

## How it works

The bar publishes a timer *snapshot* (activity, phase, timestamps) over MQTT
every time the timer changes. An always-on box runs the broker those snapshots
land on, a recorder that turns them into a session log, and a dashboard over that
log. The **bar dials in** to the broker, so the recorder needs no route *to* the
bar — put the bar on WiFi and the broker on the wired homelab; one routable LAN
bridges them.

```
   Busy Bar ──WiFi──► Router ──Ethernet──► Homelab box
   (publishes             (bridges)          ├── mosquitto  broker   (:1883)
    snapshots                                 ├── linker     (completes the link)
    once linked)                              ├── recorder   → activity_log.jsonl
                                              └── dashboard  (:8501)
```

**Why the linker exists.** On current firmware (r909) the bar only publishes
snapshots once it is *linked to an account* — before that, a snapshot publish is
dropped on-device. The normal link flow goes through the vendor cloud. The link
handshake, however, is unvalidated: the device accepts any link token published
to its own topic and then flips itself to *linked*. The **linker** service plays
that role locally — it watches for a bar announcing itself and publishes a
synthetic token — so no vendor cloud (and no firmware change) is needed. The full
investigation and rationale are in [`docs/mqtt-migration.md`](docs/mqtt-migration.md).

## Layout

The repo splits into a capture backend and a visualization frontend that share
only the session log and the card map:

```
broker/      mosquitto.conf — the MQTT broker the bar publishes to
backend/     recorder + linker (one image, two entrypoints; needs paho-mqtt)
  activity_recorder.py   snapshot → session state machine, --flow-overtime, --self-test
  mqtt_linker.py         completes the device's account link so it starts publishing
  busy_probe.py          read-only protocol client for the WS fallback (needs busylib)
frontend/    dashboard — reads the log, renders the interactive views
  dashboard.py           Streamlit app (Day / Week / Month / Heatmap)
  dashboard_data.py      load / filter / shape pipeline (also used by the CLI)
  dashboard_viz.py       Plotly figures
  dashboard_theme.py     palette + Plotly theming
  activity_summary.py    single-day CLI summary
config/      activity_card_id_map.json — the one coordination point with firmware
data/        activity_log.jsonl + linker_state.json (gitignored, bind-mounted)
docs/        design + migration notes
```

Backend and frontend share no code — only the files under `config/` and `data/`,
which are bind-mounted into the containers.

## Getting the stack online & linked (Docker)

This is the full path from a fresh clone to a linked bar logging sessions. It
assumes the bar and the homelab are on one routable LAN (see the diagram above),
and that you have the bar tethered to a laptop over **USB** for the one-time
pointing step — the device's HTTP config API is reachable only over USB, not over
its WiFi IP.

### 1. Configure

```bash
cp .env.example .env
```

Edit `.env`: at minimum set `TZ` (logs are stored in UTC; this only affects how
the dashboard renders times). The MQTT defaults already target this stack. See
[Configuration](#configuration) for the full list.

Optionally edit `config/activity_card_id_map.json` to map your firmware's card
UUIDs to stable activity keys (see [The card map](#the-card-map)). Unmapped cards
still record — they just log under the raw UUID until you add them.

### 2. Open the broker port to the bar (homelab firewall)

If the homelab runs a firewall, allow inbound MQTT **from the bar's IP only**:

```bash
sudo ufw allow from <bar-wifi-ip> to any port 1883 proto tcp comment 'busy bar MQTT'
```

The broker is plain + anonymous (fine for a trusted LAN); this rule is what keeps
it scoped. See `broker/mosquitto.conf` for optional TLS/password hardening.

### 3. Bring up the stack

```bash
docker compose up -d --build
```

- **mosquitto** — the broker the bar publishes to (host port `MQTT_PORT`, default 1883).
- **linker** — completes the account link; persists per-bar state to `data/linker_state.json`.
- **recorder** — subscribes and appends to `data/activity_log.jsonl`.
- **dashboard** — serves the UI at <http://localhost:8501>.

### 4. Point the bar at your broker (one-time, over USB)

From the USB-tethered laptop, point the bar at the homelab's **LAN IP** (not the
`mosquitto` service name, and not the bar's own IP). Use **`PUT`** and include
`client_cert_type:"none"`:

```bash
curl -s -X PUT http://<bar-usb-ip>/api/account/backend \
  -H 'Content-Type: application/json' \
  -d '{"server_url":"mqtt://<homelab-lan-ip>:1883","client_cert_type":"none","ignore_server_cert":true}'
```

`<bar-usb-ip>` is the USB virtual-LAN address (repo default `10.0.4.20`). Setting
the backend makes the bar reconnect, which announces its presence — the linker
catches it and links it automatically. (A power-cycle of the bar does the same.)

### 5. Verify it linked and is recording

```bash
docker compose logs linker        # look for: tokened <serial> → session_id <uuid>
docker compose logs recorder      # look for: connected … subscribing 'sessions/+/up/v1/busy/snapshot'
```

Then start a timer on the bar for ~30s and stop it. Watch snapshots on the wire:

```bash
docker compose exec mosquitto mosquitto_sub -t 'sessions/+/up/v1/busy/snapshot' -v
```

and confirm a completed session lands in the log:

```bash
docker compose logs -f recorder   # prints each logged session
tail -f data/activity_log.jsonl
```

> **Do not trust `/api/account/status` to confirm linking.** That endpoint returns
> `{"status":"connected"}` for *both* the linked and not-linked states — it only
> tells you the bar reached the broker, not that it linked. The real proof is
> seeing **session-scope** traffic (`sessions/<id>/up/v1/…`) on the wire, since the
> firmware only permits those publishes once linked.

### Recovering a stuck bar

If a bar resets its link state (a power-cycle or factory reset can), the linker
normally re-links it on its next presence once its cooldown elapses
(`LINKER_COOLDOWN_S`, default 60s). If it ever gets stuck not-linked, force a
clean slate by clearing the linker's state and restarting it (the file is
root-owned, so remove it from inside a container that mounts `/data`):

```bash
docker compose exec -T recorder rm -f /data/linker_state.json
docker compose restart linker
```

Then re-trigger a bar reconnect (step 4, or power-cycle).

## Configuration

All knobs live in `.env` (copied from `.env.example`, which documents each one):

| Var | Purpose |
|-----|---------|
| `TZ` | Timezone the dashboard renders session times in (logs stay UTC). |
| `MQTT_TOPIC` | Snapshot topic the recorder subscribes to. Default `sessions/+/up/v1/busy/snapshot`. |
| `MQTT_PORT` | Host port the broker is published on — the port the bar connects to. |
| `MQTT_CLIENT_ID` | Recorder's durable client id; keep stable so the broker buffers across restarts. |
| `LINKER_COOLDOWN_S` | Seconds before the linker re-tokens the same bar (self-heals a reset bar). |
| `LINK_EMAIL` / `LINK_USER_ID` | Cosmetic "linked account" the bar stores and shows in its UI. |
| `FLOW_OVERTIME` | Count work done past the timer until the break is manually started (1/0). |
| `DASHBOARD_PORT` | Host port the dashboard is published on. |
| `MQTT_BROKER` / `BUSY_ADDR` | Broker address; unset `MQTT_BROKER` to fall back to the WS path at `BUSY_ADDR`. |

### The card map

`config/activity_card_id_map.json` maps each firmware **card UUID → stable
activity key** — the one place this tooling and the firmware must agree. It is
bind-mounted read-only into the recorder and dashboard; edit it and
`docker compose restart recorder dashboard` to pick up changes (no rebuild). An
unmapped card records under its raw UUID, so to discover a new card's UUID, run
its timer once and read the `card_id` from the recorder log or `activity_log.jsonl`,
then add a line:

```json
{ "b1a70000-0000-4000-8000-000000000004": "development" }
```

## Running locally (no Docker)

The WebSocket path (recorder connects *outbound* to the bar, no broker/linker)
still works for a laptop physically near the bar. Paths default to `data/` and
`config/` in the repo, or the `ACTIVITY_LOG` / `CARD_MAP` env vars if set.

```bash
pip install busylib                                                   # backend
python3 backend/activity_recorder.py --self-test                      # offline logic check
python3 backend/activity_recorder.py --addr 10.0.4.20 --flow-overtime # live recording (WS)
python3 backend/mqtt_linker.py --self-test                            # linker logic check

pip install -r frontend/requirements.txt                             # frontend
streamlit run frontend/dashboard.py                                   # dashboard
python3 frontend/activity_summary.py 7/21/26                          # one-day CLI summary
```

## Session log schema

Each line of `data/activity_log.jsonl`:

- `activity` — stable activity key (from the map)
- `phase` — `work` / `rest` / `focus`
- `start`, `end` — ISO-8601 UTC
- `duration_s`
- `interval_index` — for INTERVAL activities
- `card_id` — the device UUID that streamed
- `partial` — session was open at connect (start time approximate)
- `truncated` — session force-closed at last-seen snapshot on a stream drop
- `overtime_s` — work beyond the timer, in `--flow-overtime` mode

## Related repositories & artifacts

- **Firmware fork** (on-device activity selection):
  `github.com/MaxwellDeJong/busybar-firmware`, branch `activity-selection`.
  Tracked separately; not part of this repo.
- **Recovery bundles** (`recovery/`, gitignored): re-download and SHA-256 verify
  the stock 1.0.2 images from
  `https://update.busy.app/busybar-firmware/directory.json` — see
  `docs/firmware-activity-selection-plan.md` §9.2.
- **MQTT migration** (`docs/mqtt-migration.md`): the full investigation behind the
  broker + linker + recorder architecture, including the firmware source
  references that justify it.

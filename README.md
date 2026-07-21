# Busy Bar Activity Tracker — Host Tooling

Passive, host-side tooling for personal activity time-tracking on the
[Busy Bar](https://busy.app), paired with on-device activity-selection firmware
(kept in a separate fork — see below).

Selection happens entirely on the device: turn the wheel to a custom activity,
press to start. This tooling never participates in selection — it is a **passive
recorder** that reads the device's timer stream and writes a per-activity
session log.

## Components

| File | What it is |
|------|------------|
| `activity_recorder.py` | The recorder. Streams `/api/status/ws`, maps `card_id` → activity key, writes one JSONL line per completed session. Supports `--flow-overtime` and an offline `--self-test`. |
| `busy_probe.py` | Read-only protocol discovery probe (identity, timer/profile shape, live input/timer streaming). |
| `activity_card_id_map.json` | Static `card_id → activity key` map — the one coordination point between firmware and host. |
| `firmware-activity-selection-plan.md` | Full design, implementation, and on-device verification record for the whole system. |
| `mqtt-migration.md` | Plan for moving recording to an always-on homelab via the device's built-in MQTT publishing, including containerization. |

## Quick start

```bash
pip install busylib
python3 activity_recorder.py --self-test                     # offline logic check, no hardware
python3 activity_recorder.py --addr 10.0.4.20 --flow-overtime # live recording over USB/WiFi
```

Start the recorder **before** starting a timer so no session is mid-flight at
connect. Completed sessions are appended to `activity_log.jsonl` (gitignored).

## Session log schema

Each line of `activity_log.jsonl`:

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
  `https://update.busy.app/busybar-firmware/directory.json` — see the plan §9.2.

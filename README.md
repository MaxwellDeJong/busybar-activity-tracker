# Busy Bar Activity Tracker — Host Tooling

Passive, host-side tooling for personal activity time-tracking on the
[Busy Bar](https://busy.app), paired with on-device activity-selection firmware
(kept in a separate fork — see below).

Selection happens entirely on the device: turn the wheel to a custom activity,
press to start. This tooling never participates in selection — it is a **passive
recorder** that reads the device's timer stream and writes a per-activity
session log, plus a dashboard that visualizes that log.

## Layout

The repo splits into a capture backend and a visualization frontend that share
only the session log and the card map:

```
backend/     recorder — streams the device WS, writes the session log
  activity_recorder.py   card_id -> activity mapping, --flow-overtime, --self-test
  busy_probe.py          read-only protocol client (needs busylib)
frontend/    dashboard — reads the log, renders the interactive views
  dashboard.py           Streamlit app (Day / Week / Month / Heatmap)
  dashboard_data.py      load / filter / shape pipeline (also used by the CLI)
  dashboard_viz.py       Plotly figures
  dashboard_theme.py     palette + Plotly theming
  activity_summary.py    single-day CLI summary
config/      activity_card_id_map.json — the one coordination point with firmware
data/        activity_log.jsonl — the durable session log (gitignored)
docs/        design + migration notes
```

Backend and frontend share no code — only the files under `config/` and `data/`,
which are bind-mounted into both containers.

## Running with Docker (recommended)

```bash
cp .env.example .env          # set BUSY_ADDR to your bar's LAN address
docker compose up -d --build
```

- **recorder** streams the device and appends to `data/activity_log.jsonl`.
- **dashboard** serves the UI at <http://localhost:8501>.

Both mount `./data` (the log, read-write) and `./config` (the card map,
read-only), so the recorder's new sessions show up in the dashboard on Refresh,
and editing the card map + restarting updates both halves without a rebuild.

The recorder container connects *outbound* to the bar, so standard bridge
networking works as long as `BUSY_ADDR` is routable from the Docker host. Start
the recorder **before** starting a timer so no session is mid-flight at connect.

## Running locally (no Docker)

Paths default to `data/` and `config/` in the repo, or the `ACTIVITY_LOG` /
`CARD_MAP` env vars if set.

```bash
pip install busylib                                                   # backend
python3 backend/activity_recorder.py --self-test                      # offline logic check
python3 backend/activity_recorder.py --addr 10.0.4.20 --flow-overtime # live recording

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
- **MQTT migration** (`docs/mqtt-migration.md`): plan for moving recording to an
  always-on homelab via the device's built-in MQTT publishing.

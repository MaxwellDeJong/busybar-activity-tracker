# Recorder: streams the Busy Bar's /api/status/ws and appends session records to
# the shared log volume. Read-only with respect to the device.
FROM python:3.12-slim

WORKDIR /app

# tzdata so a named TZ resolves; the recorder logs UTC regardless, but a correct
# TZ keeps its console/log captions aligned with the dashboard.
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY activity_recorder.py busy_probe.py ./

# Build-time sanity gate: the session state machine must pass its offline checks.
RUN python activity_recorder.py --self-test

# Paths default to the shared mounts; BUSY_ADDR / FLOW_OVERTIME come from compose.
ENV TZ=America/New_York \
    ACTIVITY_LOG=/data/activity_log.jsonl \
    CARD_MAP=/config/activity_card_id_map.json

CMD ["python", "activity_recorder.py"]

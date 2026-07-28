# Backend image, shared by two services:
#   - recorder: subscribes to the broker (or the WS fallback) and appends session
#     records to the shared log volume. Read-only with respect to the device.
#   - linker:   completes the device's account link so it starts publishing.
# The compose `command:` selects which one a given container runs.
FROM python:3.12-slim

WORKDIR /app

# tzdata so a named TZ resolves; the recorder logs UTC regardless, but a correct
# TZ keeps its console/log captions aligned with the dashboard.
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY activity_recorder.py busy_probe.py mqtt_linker.py ./

# Build-time sanity gate: the session state machine and the linker logic must
# pass their offline checks. The same image runs the recorder and the linker.
RUN python activity_recorder.py --self-test \
    && python mqtt_linker.py --self-test

# Paths default to the shared mounts; BUSY_ADDR / FLOW_OVERTIME come from compose.
ENV TZ=America/New_York \
    ACTIVITY_LOG=/data/activity_log.jsonl \
    CARD_MAP=/config/activity_card_id_map.json

CMD ["python", "activity_recorder.py"]

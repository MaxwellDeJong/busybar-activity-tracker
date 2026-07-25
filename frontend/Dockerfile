# Dashboard: reads the shared session log and serves the interactive Streamlit UI.
FROM python:3.12-slim

WORKDIR /app

# tzdata so a named TZ (e.g. America/New_York) resolves; slim images ship none,
# which would silently pin the container to UTC and shift every rendered time.
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# TZ drives .astimezone() in the dashboard; overridable via compose/.env.
ENV TZ=America/New_York \
    ACTIVITY_LOG=/data/activity_log.jsonl \
    CARD_MAP=/config/activity_card_id_map.json

EXPOSE 8501

# 0.0.0.0 so the port is reachable from outside the container; headless so it
# never tries to open a browser inside the image.
CMD ["streamlit", "run", "dashboard.py", \
     "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true"]

# Dashboard: reads the shared session log and serves the interactive Streamlit UI.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV ACTIVITY_LOG=/data/activity_log.jsonl \
    CARD_MAP=/config/activity_card_id_map.json

EXPOSE 8501

# 0.0.0.0 so the port is reachable from outside the container; headless so it
# never tries to open a browser inside the image.
CMD ["streamlit", "run", "dashboard.py", \
     "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true"]

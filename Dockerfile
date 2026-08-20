FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Dependencies first so code edits don't invalidate the layer.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py ./
COPY mccapbot ./mccapbot

# Alert/payment state lives here. Mount a Railway volume at /data to make it
# survive deploys — without one, every restart starts from an empty alert list.
ENV DATA_DIR=/data
RUN mkdir -p /data

CMD ["python", "-u", "main.py"]

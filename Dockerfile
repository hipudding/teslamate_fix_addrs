FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CHECKPOINT_FILE=/data/checkpoint.json

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY teslamate_fix_addrs.py .

# Git commit of this build, printed at startup.
ARG APP_BUILD=local
ENV APP_BUILD=$APP_BUILD

# Progress (checkpoint.json) lives here; mount a volume to keep it across
# container re-creation, otherwise mode 1 calls the map API for every
# address again.
VOLUME /data
WORKDIR /data

ENTRYPOINT ["python3", "/app/teslamate_fix_addrs.py"]

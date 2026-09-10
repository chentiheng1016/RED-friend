FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    RED_RUNTIME_DIR=/var/lib/red \
    RED_CLOUD_MODE=1 \
    RED_EXCHANGE_MODE=telegram_only \
    AGENT_DAEMON_MODE=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-core.txt requirements-rag.txt requirements-web.txt requirements-docs.txt ./
RUN python -m pip install --upgrade pip \
    && pip install \
        -r requirements-core.txt \
        -r requirements-rag.txt \
        -r requirements-web.txt \
        -r requirements-docs.txt

COPY . .

RUN mkdir -p /var/lib/red/state/google /var/lib/red/data /var/lib/red/logs /var/lib/red/runs /var/lib/red/workflows \
    && groupadd --system --gid 10001 red \
    && useradd --system --uid 10001 --gid 10001 --create-home --home-dir /home/red red \
    && chown -R red:red /var/lib/red /app

USER red

CMD ["python", "-m", "agent_core.cloud_run_entrypoint"]

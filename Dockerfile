# Pinned by digest so a rebuild is reproducible; Dependabot (docker ecosystem)
# proposes new digests. The tag is kept for readability.
FROM python:3.15-slim@sha256:94d3426f1def7db4a106a232ac121bfd975393e5dfd40a619b955fae45fe7913

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/home/app \
    MPLCONFIGDIR=/tmp/matplotlib \
    XDG_CACHE_HOME=/app/data/.cache

# Unprivileged runtime user. The entrypoint starts as root only to chown the
# data volume, then drops to this user (see docker-entrypoint.sh).
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin app

WORKDIR /app

COPY requirements.txt .
# curl_cffi is a hard dependency of modern yfinance and must remain installed.
RUN pip install --no-cache-dir -r requirements.txt

COPY news_bot.py .
COPY newsbot/ newsbot/
COPY evaluation_universe.csv .
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

# "Running" is not "working": the process can stay up with a dead poller thread
# or a wedged scheduler. The bot writes heartbeat files next to its state file
# and this fails when they go stale. Docker only marks the container
# unhealthy; restart behaviour is unchanged.
HEALTHCHECK --interval=60s --timeout=30s --start-period=180s --retries=3 \
  CMD ["python", "news_bot.py", "--healthcheck"]

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "news_bot.py"]

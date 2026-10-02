FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
COPY news_bot.py .
COPY newsbot/ newsbot/
COPY evaluation_universe.csv .

# curl_cffi is a hard dependency of modern yfinance and must remain installed.
RUN pip install --no-cache-dir -r requirements.txt

# "Running" is not "working": the process can stay up with a dead poller thread
# or a wedged scheduler. The bot writes heartbeat files next to its state file
# and this fails when they go stale. Docker only marks the container
# unhealthy; restart behaviour is unchanged.
HEALTHCHECK --interval=60s --timeout=30s --start-period=180s --retries=3 \
  CMD ["python", "news_bot.py", "--healthcheck"]

CMD ["python", "news_bot.py"]

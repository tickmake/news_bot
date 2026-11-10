# Read-only web viewer for the bot's SQLite DB (https://datasette.io).
# The official datasetteproject/datasette image is amd64-only, so build a
# tiny one on the same Python base the bot uses; it runs on the Pi 5.
FROM python:3.11-slim@sha256:bab1b7ef4b450c81002278d035eff85ebe394ae94df904f7a3ba14f7e16e487b

RUN pip install --no-cache-dir datasette==0.65.1 \
    # Datasette names a database after its file, and the bot's file is
    # dot-prefixed; a symlink gives it the readable name `trade_history`.
    && mkdir /db && ln -s /app/data/.news_bot_trade_history.db /db/trade_history.db

USER nobody

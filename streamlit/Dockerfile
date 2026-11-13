# Fjord Signals, Streamlit edition: the dashboard pages plus a what-if tool for
# the screen's filters, read straight from the bot's SQLite DB (read-only).
FROM python:3.15-slim@sha256:94d3426f1def7db4a106a232ac121bfd975393e5dfd40a619b955fae45fe7913

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY .streamlit ./.streamlit
COPY app.py archive_views.py ./

USER nobody
ENV HOME=/tmp
CMD ["streamlit", "run", "app.py"]

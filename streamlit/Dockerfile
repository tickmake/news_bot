# Fjord Signals, Streamlit edition: the dashboard pages plus a what-if tool for
# the screen's filters, read straight from the bot's SQLite DB (read-only).
FROM python:3.15-slim@sha256:94d3426f1def7db4a106a232ac121bfd975393e5dfd40a619b955fae45fe7913

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY .streamlit ./.streamlit
COPY app.py archive_views.py auth.py serve.py ./

USER nobody
ENV HOME=/tmp
# The health route is the one path the login (auth.py) lets through. urllib,
# because the slim image has no curl; any non-2xx answer raises and fails it.
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=5)"]
# serve.py wraps app.py in the login; running app.py directly would skip it.
CMD ["streamlit", "run", "serve.py"]

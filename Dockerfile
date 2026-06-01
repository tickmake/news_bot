FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
COPY news_bot.py .

# curl_cffi is a hard dependency of modern yfinance and must remain installed.
RUN pip install --no-cache-dir -r requirements.txt

CMD ["python", "news_bot.py"]

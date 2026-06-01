FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
COPY news_bot.py .

# yfinance is pinned to a requests-only release (no curl_cffi); see requirements.txt.
RUN pip install --no-cache-dir -r requirements.txt

CMD ["python", "news_bot.py"]

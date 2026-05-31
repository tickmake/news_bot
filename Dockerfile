FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
COPY news_bot.py .

# Install dependencies, then forcefully remove the problematic curl_cffi library
RUN pip install --no-cache-dir -r requirements.txt && \
    pip uninstall -y curl_cffi

CMD ["python", "news_bot.py"]

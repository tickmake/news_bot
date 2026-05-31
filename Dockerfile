FROM python:3.11-slim

# Set the working directory inside the container
WORKDIR /app

# Copy your local Git files directly into the container image
COPY requirements.txt .
COPY news_bot.py .

# Install the Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Run the script when the container starts
CMD ["python", "news_bot.py"]

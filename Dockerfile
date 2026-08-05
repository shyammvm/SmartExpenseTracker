FROM python:3.11-slim

# Prevent Python from writing pyc files and buffer outputs
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Copy and install dependencies first to leverage Docker caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy remaining code
COPY . .

# Run uvicorn on 0.0.0.0 and use $PORT set by Cloud Run (defaults to 8080)
CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}

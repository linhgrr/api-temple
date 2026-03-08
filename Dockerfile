FROM python:3.11-slim

# Install build dependencies for native extensions (lz4, cffi, etc.)
RUN apt-get update && apt-get install -y --no-install-recommends gcc g++ && rm -rf /var/lib/apt/lists/*

# Install Requirements
WORKDIR /app
COPY requirements.txt .
RUN pip install --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browser and system dependencies
RUN playwright install chromium --with-deps

# Copy project files
COPY . .

# Set PYTHONPATH so uvicorn can find the app module inside src/
ENV PYTHONPATH=/app/src

# Default Port (Render overrides via $PORT)
EXPOSE 6969

# Run Uvicorn server — use shell form so $PORT is expanded at runtime
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-6969}

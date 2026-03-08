FROM python:3.11-slim

# Install build dependencies for native extensions (lz4, cffi, etc.)
RUN apt-get update && apt-get install -y --no-install-recommends gcc g++ && rm -rf /var/lib/apt/lists/*

# Install Requirements
WORKDIR /app
COPY requirements.txt .
RUN pip install --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium + Xvfb + X11 libs for headful browser mode (needed to bypass Google bot detection)
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium procps xvfb \
    libx11-6 libxcomposite1 libxdamage1 libxext6 libxfixes3 \
    libxrandr2 libxrender1 libxtst6 libxss1 \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libgbm1 libpango-1.0-0 libcairo2 \
    libasound2 libatspi2.0-0 fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Copy project files
COPY . .

# Set PYTHONPATH so uvicorn can find the app module inside src/
ENV PYTHONPATH=/app/src

# Default Port (Render overrides via $PORT)
EXPOSE 6969

# Run Uvicorn server — use shell form so $PORT is expanded at runtime
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-6969}

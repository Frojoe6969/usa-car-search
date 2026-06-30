FROM python:3.12-slim

# System deps for Playwright/Chromium
RUN apt-get update && apt-get install -y \
    curl wget gnupg ca-certificates \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libgbm1 libasound2 \
    libpango-1.0-0 libpangocairo-1.0-0 libgtk-3-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright Chromium browser
RUN playwright install chromium

# Copy source
COPY usa-car-search.py _at_worker.py ebay-oauth-setup.py fb-auth-setup.py ./

# Data directory for persistent files (seen list, tokens, session)
VOLUME ["/data"]

# Default: run the scraper (use --notify to send Telegram alerts)
ENTRYPOINT ["python3", "usa-car-search.py"]
CMD []

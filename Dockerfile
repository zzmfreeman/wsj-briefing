FROM python:3.12-slim

# System deps for Playwright
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget gnupg2 libnss3 libnspr4 libdbus-1-3 libatk1.0-0 \
    libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 \
    libatspi2.0-0 libxcomposite1 libxdamage1 libxfixes3 \
    libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 \
    libx11-xcb1 libxcb-dri3-0 libxshmfence1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers
RUN playwright install chromium

# App code
COPY config.py .
COPY wsj_rss_briefing.py .
COPY wsj_cn_home_briefing.py .
COPY generate_web.py .
COPY run_combined_briefing.py .

# Volumes
VOLUME /data/cookies
VOLUME /data/archive
VOLUME /data/seen
VOLUME /data/web

# Cookie file location (override via env)
ENV COOKIE_FILE=/data/cookies/cn_wsj_cookies.txt
ENV ARCHIVE_DIR=/data/archive
ENV WEB_DIR=/data/web

# Healthcheck: verify config readable + cookie valid
HEALTHCHECK --interval=5m --timeout=10s --retries=3 \
    CMD python3 -c "from config import get_model_config, check_cookie_health; \
        c=get_model_config(); h=check_cookie_health(); \
        exit(0 if c and h[0] else 1)"

ENTRYPOINT ["python3", "run_combined_briefing.py"]

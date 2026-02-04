# US Visa Appointment Checker - Docker Image
# Optimized for Ubuntu 24.04 LTS with Chrome headless support
# Build: docker build -t visa-checker .
# Run: docker compose up -d

FROM ubuntu:24.04

# Prevent interactive prompts during apt install
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV TZ=America/Toronto

# Labels for image identification
LABEL maintainer="US Visa Appointment Checker"
LABEL version="2.0"
LABEL description="Automated US Visa appointment checker with email notifications"

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-venv \
    wget \
    gnupg \
    curl \
    ca-certificates \
    fonts-liberation \
    libasound2t64 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libatspi2.0-0 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgbm1 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxkbcommon0 \
    libxrandr2 \
    xdg-utils \
    && rm -rf /var/lib/apt/lists/*

# Install Google Chrome (stable)
RUN wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg && \
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list && \
    apt-get update && \
    apt-get install -y google-chrome-stable && \
    rm -rf /var/lib/apt/lists/*

# Create non-root user for security
RUN useradd -m -u 1000 -s /bin/bash visabot

# Create app directory
WORKDIR /app

# Copy requirements first (for Docker layer caching)
COPY requirements.txt .

# Install Python dependencies
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

# Copy application code
COPY visa_appointment_checker.py .
COPY config.ini.template .

# Create directories for logs and artifacts
RUN mkdir -p logs artifacts && \
    chown -R visabot:visabot /app

# Switch to non-root user
USER visabot

# Health check: verify Chrome works and log file is being written
HEALTHCHECK --interval=5m --timeout=30s --start-period=60s --retries=3 \
    CMD test -f /app/logs/visa_checker.log && \
        find /app/logs/visa_checker.log -mmin -10 | grep -q . || exit 1

# Default command - run checker with 5-minute frequency
# Override with: docker run visa-checker python3 visa_appointment_checker.py --frequency 3
CMD ["python3", "visa_appointment_checker.py", "--frequency", "5"]

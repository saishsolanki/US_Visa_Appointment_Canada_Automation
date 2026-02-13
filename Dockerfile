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

# Create app directory first
WORKDIR /app

# Copy requirements first (for Docker layer caching)
COPY requirements.txt .

# Install Python dependencies
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

# Copy application code
COPY visa_appointment_checker.py .
COPY config.ini.template .

# Create directories
RUN mkdir -p logs artifacts

# Note: We run as root to avoid permission issues with mounted volumes
# Security is maintained through Docker's resource limits and network isolation

# Health check: verify the checker process is still running as PID 1
HEALTHCHECK --interval=5m --timeout=30s --start-period=60s --retries=3 \
    CMD python3 -c "import pathlib, sys; cmdline = pathlib.Path('/proc/1/cmdline').read_text(errors='ignore'); sys.exit(0 if 'visa_appointment_checker.py' in cmdline else 1)"

# Default command - run checker with 5-minute frequency
CMD ["python3", "visa_appointment_checker.py", "--frequency", "5"]

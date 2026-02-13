# Docker Deployment Guide

This guide explains how to run the US Visa Appointment Checker in Docker for reliable 24/7 background operation.

## 🚀 Quick Start (3 Commands)

```bash
# 1. Clone the repository
git clone https://github.com/saishsolanki/US_Visa_Appointment_Canada_Automation.git
cd US_Visa_Appointment_Canada_Automation

# 2. Create and edit your configuration
cp config.ini.template config.ini
nano config.ini  # or use any text editor

# 3. Start the container
docker compose up -d
```

That's it! The checker is now running in the background.

## 📦 Use a stable, versioned image

Release images are published as version tags (for example `v1.2.0`) to GHCR:

```bash
docker pull ghcr.io/saishsolanki/us_visa_appointment_canada_automation:v1.2.0
```

Pinning a versioned tag avoids unexpected behavior from unpinned latest builds.

## 📋 Prerequisites

- **Docker Engine** 20.10+ (with Docker Compose v2)
- **Ubuntu 24.04 LTS** (recommended) or any Linux with Docker support
- **2GB+ free disk space**
- **Gmail account** with App Password for notifications

### Install Docker on Ubuntu 24.04

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install Docker
sudo apt install -y docker.io docker-compose-v2

# Add yourself to docker group (avoid sudo)
sudo usermod -aG docker $USER

# Log out and back in, then verify
docker --version
docker compose version
```

## ⚙️ Configuration

### Required Settings in config.ini

```ini
[DEFAULT]
# Your AIS credentials (from usvisa-info.com)
email = your_ais_email@example.com
password = your_ais_password

# Your current appointment date
current_appointment_date = 2025-12-01

# Embassy location
location = Ottawa - U.S. Embassy

# Date range you're looking for
start_date = 2025-09-25
end_date = 2025-12-31

# Gmail notifications (use App Password!)
smtp_user = your_gmail@gmail.com
smtp_pass = your_16_char_app_password
notify_email = your_gmail@gmail.com
```

### Gmail App Password Setup

1. Enable 2-Step Verification at https://myaccount.google.com/security
2. Go to https://myaccount.google.com/apppasswords
3. Generate a new App Password (select "Mail")
4. Copy the 16-character password to `smtp_pass`

## 🐳 Docker Commands

### Start the checker (background)
```bash
docker compose up -d
```

### View live logs
```bash
docker compose logs -f
```

### View last 100 log lines
```bash
docker compose logs --tail 100
```

### Stop the checker
```bash
docker compose down
```

### Restart after config changes
```bash
docker compose down
docker compose up -d
```

### Rebuild after code updates
```bash
git pull
docker compose build --no-cache
docker compose up -d
```

### Check container status
```bash
docker compose ps
```

## 📊 Monitoring

### Progress Reports

The checker automatically sends email progress reports every 6 hours including:
- Success/failure statistics
- Key events detected
- Attached log file

To change the interval:
```bash
# Edit docker-compose.yml command to add --report-interval
docker compose down
# Add to command: python3 visa_appointment_checker.py --frequency 5 --report-interval 3
docker compose up -d
```

### View Logs on Host

Logs are persisted to your host machine:
```bash
# Real-time log watching
tail -f logs/visa_checker.log

# Search for appointments found
grep -i "available\|found" logs/visa_checker.log

# Check for errors
grep -i "error\|fail" logs/visa_checker.log
```

### Check Resource Usage
```bash
docker stats visa-checker
```

## 🔧 Customization

### Change Check Frequency

Edit `docker-compose.yml` and modify the command:
```yaml
command: ["python3", "visa_appointment_checker.py", "--frequency", "3"]
```

### Change Timezone

Edit `docker-compose.yml`:
```yaml
environment:
  - TZ=America/New_York  # or your timezone
```

### Run in Non-Headless Mode (Debugging)

```bash
# This won't work in Docker (no display)
# Instead, debug on your local machine:
python visa_appointment_checker.py --no-headless --frequency 5
```

## 🚨 Troubleshooting

### Container keeps restarting

Check the logs for errors:
```bash
docker compose logs --tail 50
```

Common issues:
- Invalid config.ini settings
- SMTP authentication failed
- Chrome crash (usually fixed by rebuilding)

### SMTP/Email errors

1. Verify your Gmail App Password
2. Check if port 587 is accessible: `nc -zv smtp.gmail.com 587`
3. Make sure 2FA is enabled on your Gmail account

### Out of disk space

```bash
# Clean up Docker resources
docker system prune -a

# Check artifacts folder size
du -sh artifacts/
# Delete old artifacts if needed
rm -rf artifacts/*.html artifacts/*.png
```

### Chrome crashes

Rebuild the container:
```bash
docker compose build --no-cache
docker compose up -d
```

## 🔒 Security Notes

1. **Never commit config.ini** - It contains your credentials
2. The container runs as non-root user for security
3. Config file is mounted read-only
4. Consider using Docker secrets for production deployments

## 📁 File Structure

After running Docker, your directory will look like:
```
US_Visa_Appointment_Canada_Automation/
├── config.ini           # Your configuration (git-ignored)
├── config.ini.template  # Template file
├── docker-compose.yml   # Container orchestration
├── Dockerfile           # Container definition
├── logs/                # Persistent logs (git-ignored)
│   └── visa_checker.log
├── artifacts/           # Screenshots on errors (git-ignored)
└── visa_appointment_checker.py
```

## 🆘 Getting Help

- Check the logs first: `docker compose logs --tail 100`
- Review [GMAIL_SETUP_GUIDE.md](GMAIL_SETUP_GUIDE.md) for email issues
- Open an issue on GitHub with:
  - Error messages from logs
  - Your config.ini (with credentials removed)
  - Docker version: `docker --version`

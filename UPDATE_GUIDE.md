# Update Guide

## 🚀 Quick Update (Recommended)

On your server, run these commands in the installation directory:

```bash
cd ~/nas_storage/US_Visa_Appointment_Canada_Automation

# Make update script executable (first time only)
chmod +x update.sh

# Run update
./update.sh
```

The update script will:
- ✅ Backup your config.ini
- ✅ Pull latest code from GitHub
- ✅ Rebuild Docker container or update dependencies
- ✅ Restart the service
- ✅ Preserve your configuration

---

## 🐳 Manual Update (Docker)

If you prefer to update manually:

```bash
cd ~/nas_storage/US_Visa_Appointment_Canada_Automation

# Backup your config
cp config.ini config.ini.backup

# Stop the running container
docker compose down

# Pull latest code
git pull origin main

# Rebuild the image (with no cache to ensure fresh build)
docker compose build --no-cache

# Start the updated container
docker compose up -d

# View logs to confirm it's working
docker compose logs -f
```

---

## 🛠️ Fix Current UID Error

You encountered this error because UID 1000 is already taken. I've fixed it in the latest code. To apply the fix now:

```bash
cd ~/nas_storage/US_Visa_Appointment_Canada_Automation

# Pull the fix
git pull origin main

# Rebuild with the corrected Dockerfile
docker compose build --no-cache

# Start the container
docker compose up -d
```

The Dockerfile now uses UID 1001 instead of 1000 to avoid conflicts.

---

## ⚙️ Manual Update (Systemd Service)

For native systemd installations:

```bash
cd ~/nas_storage/US_Visa_Appointment_Canada_Automation

# Backup config
cp config.ini config.ini.backup

# Stop service
sudo systemctl stop visa-checker

# Pull latest code
git pull origin main

# Update Python packages
source visa_env/bin/activate
pip install --upgrade -r requirements.txt
deactivate

# Restart service
sudo systemctl start visa-checker

# Check status
sudo systemctl status visa-checker
```

---

## 🔍 Verify Update Success

### For Docker:
```bash
# Check container is running
docker compose ps

# View recent logs
docker compose logs --tail 50

# Watch live logs
docker compose logs -f
```

### For Systemd:
```bash
# Check service status
sudo systemctl status visa-checker

# View logs
sudo journalctl -u visa-checker -n 50 -f
```

---

## 📋 What Gets Updated?

When you update, these improvements will be applied:

✅ **Enhanced Calendar Detection**
- Better handling of busy state detection
- Multiple fallback methods for opening calendar
- Smarter detection of visible vs. hidden busy messages

✅ **Improved Location Selection**
- Facility ID mapping for Canadian embassies
- Better fuzzy matching for location names
- More reliable dropdown selection

✅ **Progress Reports** (New Feature!)
- Automatic email reports every 6 hours
- Includes statistics and log file
- Configurable with `--report-interval` flag

✅ **Docker Improvements**
- Fixed UID conflict issue
- Better resource limits
- Improved health checks

✅ **Bug Fixes**
- Session persistence improvements
- Better error recovery
- Reduced false positives for busy calendar

---

## ⚠️ Important Notes

### Your Configuration is Safe
- `config.ini` is NOT overwritten during updates
- Always backed up automatically
- Only `config.ini.template` gets updated (for reference)

### Check for Config Changes
After updating, compare your config with the template:
```bash
diff config.ini config.ini.template
```

If new options were added, you can manually add them to your `config.ini`.

### Rollback if Needed
If something goes wrong:
```bash
# View available backups
ls -la config.ini.backup.*

# Restore a backup
cp config.ini.backup.YYYYMMDD_HHMMSS config.ini

# For Docker, restart
docker compose restart

# For systemd, restart
sudo systemctl restart visa-checker
```

---

## 🆘 Troubleshooting Updates

### Git Pull Fails (Local Changes)
```bash
# See what changed
git status

# Stash your changes
git stash

# Pull updates
git pull origin main

# Apply your changes back (if needed)
git stash pop
```

### Docker Build Fails
```bash
# Clean up old images
docker system prune -a

# Rebuild from scratch
docker compose build --no-cache --pull
```

### Permission Denied on update.sh
```bash
chmod +x update.sh
```

### Container Won't Start After Update
```bash
# Check logs for errors
docker compose logs

# Common fix: recreate container
docker compose down
docker compose up -d --force-recreate
```

---

## 📅 Update Frequency

**Recommended:** Check for updates weekly or when:
- You encounter bugs
- New features are announced
- Embassy website changes cause failures

**Check for updates:**
```bash
cd ~/nas_storage/US_Visa_Appointment_Canada_Automation
git fetch origin
git log HEAD..origin/main --oneline
```

If you see commits listed, run `./update.sh` to apply them.

---

## 🔔 Stay Updated

Watch the GitHub repository for new releases:
1. Go to https://github.com/saishsolanki/US_Visa_Appointment_Canada_Automation
2. Click "Watch" → "Custom" → "Releases"
3. You'll get notified of new versions

---

## 💡 Pro Tips

**Automatic Updates (Advanced):**
Create a cron job to update weekly:
```bash
# Edit crontab
crontab -e

# Add line (updates every Sunday at 3 AM)
0 3 * * 0 cd ~/nas_storage/US_Visa_Appointment_Canada_Automation && ./update.sh >> ~/visa_update.log 2>&1
```

**Pre-update Test:**
Test in a separate directory first:
```bash
# Clone to test location
git clone https://github.com/saishsolanki/US_Visa_Appointment_Canada_Automation.git test-update
cd test-update
cp ../US_Visa_Appointment_Canada_Automation/config.ini .
docker compose up -d
# Test for a few hours, then apply to production
```

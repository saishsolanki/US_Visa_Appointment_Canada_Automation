# Update Guide

## Quick Update

Run the unified update script:

```bash
cd ~/nas_storage/US_Visa_Appointment_Canada_Automation
chmod +x update.sh
./update.sh
```

This automatically:
- ✅ Backs up your config.ini
- ✅ Pulls latest code
- ✅ Rebuilds Docker or updates dependencies
- ✅ Restarts the service

---

## Manual Updates by Deployment Method

### Docker
```bash
cd ~/nas_storage/US_Visa_Appointment_Canada_Automation
git pull origin main
docker compose build --no-cache
docker compose up -d
docker compose logs -f
```

### Systemd (Native Linux)
```bash
cd ~/nas_storage/US_Visa_Appointment_Canada_Automation
git pull origin main
source visa_env/bin/activate
pip install --upgrade -r requirements.txt
sudo systemctl restart visa-checker
sudo systemctl status visa-checker
```

### Windows
```batch
cd US_Visa_Appointment_Canada_Automation
git pull origin main
python -m pip install --upgrade -r requirements.txt
REM Restart your scheduled task or manual execution
```

---

## Verify Success

```bash
# Docker
docker compose logs --tail 20

# Systemd
sudo journalctl -u visa-checker -n 20 -f

# Windows
type logs\visa_checker.log | tail -20
```

---

## Troubleshooting

**Permission errors after update?** See [QUICK_FIX.md](QUICK_FIX.md)  
**Email/notification issues?** See [GMAIL_SETUP_GUIDE.md](GMAIL_SETUP_GUIDE.md)  
**Docker-specific issues?** See [DOCKER_GUIDE.md](DOCKER_GUIDE.md)  
**General troubleshooting?** See [FAQ.md](FAQ.md)

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

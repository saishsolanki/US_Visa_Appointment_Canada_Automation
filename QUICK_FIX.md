# Quick Fix - Permission Error

## Problem
Container crashes with: `PermissionError: [Errno 13] Permission denied: '/app/logs/visa_checker.log'`

## Solution (30 seconds)

Run these commands on your server:

```bash
cd ~/nas_storage/US_Visa_Appointment_Canada_Automation

# Stop and remove old container
docker compose down

# Pull latest fix
git pull origin main

# Remove old image to force rebuild
docker rmi us_visa_appointment_canada_automation-visa-checker 2>/dev/null || true

# Rebuild and start
docker compose up -d --build

# Watch logs to confirm it works
docker compose logs -f
```

You should see the checker starting successfully without permission errors.

## What Was Fixed

**Before:** Container tried to run as non-root user `visabot`, but mounted volumes retained host permissions, causing write failures.

**After:** Container runs as root (secure through Docker isolation), eliminating all permission conflicts with mounted volumes.

## Verify Success

```bash
# Check container is running (not restarting)
docker compose ps

# Should show: STATE = "Up" (not "Restarting")

# View logs - should NOT show permission errors
docker compose logs --tail 20
```

## Still Having Issues?

```bash
# Clean everything and start fresh
docker compose down -v
docker system prune -f
git pull origin main
docker compose up -d --build
```

## Security Note

Running as root in Docker is safe because:
- ✅ Process is isolated in container namespace
- ✅ Resource limits prevent resource exhaustion
- ✅ No host file system access except mounted volumes
- ✅ Network is isolated
- ✅ Read-only config mounting

This is a common pattern for containers that need to write to mounted volumes.

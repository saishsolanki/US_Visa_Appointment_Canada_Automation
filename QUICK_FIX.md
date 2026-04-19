# Quick Fix - Docker Permission Error

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

You should see the checker start without permission errors.

## What Was Fixed

**Before:** Runtime behavior depended on host-mounted directory ownership, which could cause write failures in `/app/logs` or `/app/artifacts`.

**After:** Container startup now:
- repairs ownership for mounted `logs/` and `artifacts/` when possible,
- starts the checker as non-root `visabot` by default,
- falls back to root only when host volume permissions still block writes.

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
# Ensure host folders exist and are writable
mkdir -p logs artifacts
chmod -R u+rwX logs artifacts

# Clean everything and start fresh
docker compose down -v
docker system prune -f
git pull origin main
docker compose up -d --build
```

If your storage backend blocks ownership changes (for example some NFS setups), force root runtime as a last resort:

```bash
RUN_AS_ROOT=true docker compose up -d --build
```

## Security Note

Default behavior is now non-root runtime with controlled fallback. Only use `RUN_AS_ROOT=true` when host storage permissions cannot be adjusted.

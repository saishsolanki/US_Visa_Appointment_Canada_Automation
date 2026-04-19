#!/usr/bin/env bash
set -euo pipefail

APP_USER="${APP_USER:-visabot}"
APP_GROUP="${APP_GROUP:-visabot}"
RUN_AS_ROOT_RAW="${RUN_AS_ROOT:-false}"
RUN_AS_ROOT="${RUN_AS_ROOT_RAW,,}"

mkdir -p /app/logs /app/artifacts

if [[ "${RUN_AS_ROOT}" == "true" ]]; then
    echo "[entrypoint] RUN_AS_ROOT=true -> launching as root"
    exec "$@"
fi

if [[ "$(id -u)" -ne 0 ]]; then
    exec "$@"
fi

# Best-effort ownership repair for bind-mounted host folders.
chown -R "${APP_USER}:${APP_GROUP}" /app/logs /app/artifacts 2>/dev/null || true

if gosu "${APP_USER}:${APP_GROUP}" test -w /app/logs && gosu "${APP_USER}:${APP_GROUP}" test -w /app/artifacts; then
    exec gosu "${APP_USER}:${APP_GROUP}" "$@"
fi

echo "[entrypoint] Volume permissions still blocked for ${APP_USER}; falling back to root"
exec "$@"

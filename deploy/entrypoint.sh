#!/bin/sh
# Bring the database schema up to date, then run a job-scout command (default: run).
# notify-failure skips the migration: it must still work when a migration is what failed.
set -eu
mkdir -p "$HOME"
if [ "${1:-}" != "notify-failure" ]; then
    alembic -c /app/alembic.ini upgrade head
fi
exec job-scout "$@"

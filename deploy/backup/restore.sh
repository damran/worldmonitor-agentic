#!/usr/bin/env bash
# Gate B-4b (ADR 0050) — thin ops entrypoint for a cross-store restore (DESTRUCTIVE).
# ALL logic lives in worldmonitor.backup; this wrapper carries none. It builds the three stores
# from process settings (env / .env) and rebuilds them from the backup directory passed as "$@":
#
#   deploy/backup/restore.sh /var/backups/worldmonitor/20260626T000000Z
#
# Restore wipes + reloads every store; it is a deliberate operator action, NEVER agent-auto-run, and
# is all-or-nothing + halt-loud (a missing / incomplete / corrupt backup aborts before any store is
# touched). The target stores must already exist + be migrated (alembic head) — see the runbook.
set -euo pipefail
exec uv run python -m worldmonitor.backup restore "$@"

#!/usr/bin/env bash
#
# netbox_backup.sh
#
# Daily backup of NetBox's Postgres database. Dumps, compresses, applies
# local retention cleanup, and optionally uploads to Azure Blob Storage.
# Can install/manage its own daily cron job, same pattern as
# azure_to_netbox_sync.py.
#
# Usage:
#   ./netbox_backup.sh                              # run a backup now
#   ./netbox_backup.sh --install-cron [schedule]     # install daily cron (default: "0 2 * * *")
#   ./netbox_backup.sh --uninstall-cron              # remove the cron job
#
# Configuration (env vars, or edit the defaults below):
#   NETBOX_DIR              Path to the netbox-docker directory (default: /opt/netbox-docker)
#   BACKUP_DIR               Where local backup files are kept (default: /root/netbox-backups)
#   RETENTION_DAYS           How many days of local backups to keep (default: 14)
#   AZURE_STORAGE_UPLOAD      Set to "true" to also upload to Azure Blob Storage
#   AZURE_STORAGE_ACCOUNT     Required if AZURE_STORAGE_UPLOAD=true
#   AZURE_STORAGE_CONTAINER   Blob container name (default: netbox-backups)

set -euo pipefail

NETBOX_DIR="${NETBOX_DIR:-/opt/netbox-docker}"
BACKUP_DIR="${BACKUP_DIR:-/root/netbox-backups}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
AZURE_STORAGE_UPLOAD="${AZURE_STORAGE_UPLOAD:-false}"
AZURE_STORAGE_ACCOUNT="${AZURE_STORAGE_ACCOUNT:-}"
AZURE_STORAGE_CONTAINER="${AZURE_STORAGE_CONTAINER:-netbox-backups}"

SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
CRON_MARKER="# managed-by:netbox_backup.sh"
LOG_FILE="${BACKUP_DIR}/netbox_backup.log"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') [INFO] $*"
}
err() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') [ERROR] $*" >&2
}

# ---------------------------------------------------------------------------
# Cron management (install/uninstall) - same pattern as azure_to_netbox_sync.py
# ---------------------------------------------------------------------------

install_cron() {
    local schedule="${1:-0 2 * * *}"
    mkdir -p "$BACKUP_DIR"
    local new_line="${schedule} ${SCRIPT_PATH} >> ${LOG_FILE} 2>&1 ${CRON_MARKER}"

    local current
    current="$(crontab -l 2>/dev/null || true)"
    local kept
    kept="$(echo "$current" | grep -v "$CRON_MARKER" || true)"

    { [ -n "$kept" ] && echo "$kept"; echo "$new_line"; } | crontab -
    log "Cron job installed: ${new_line}"
    log "NOTE: this schedule runs in the server's LOCAL timezone (check 'timedatectl'), not necessarily UTC."
    log "Backups will be written to: ${BACKUP_DIR}"
}

uninstall_cron() {
    local current
    current="$(crontab -l 2>/dev/null || true)"
    if ! echo "$current" | grep -q "$CRON_MARKER"; then
        log "No cron entry for this script was found - nothing to remove."
        return
    fi
    echo "$current" | grep -v "$CRON_MARKER" | crontab -
    log "Cron job removed."
}

# ---------------------------------------------------------------------------
# Backup logic
# ---------------------------------------------------------------------------

run_backup() {
    mkdir -p "$BACKUP_DIR"
    local date_str
    date_str="$(date +%F_%H%M%S)"
    local filename="netbox-${date_str}.sql.gz"
    local filepath="${BACKUP_DIR}/${filename}"

    if [ ! -d "$NETBOX_DIR" ]; then
        err "NETBOX_DIR does not exist: ${NETBOX_DIR}"
        exit 1
    fi

    log "Starting backup..."
    cd "$NETBOX_DIR"

    if ! docker compose exec -T postgres pg_dump -U netbox netbox | gzip > "$filepath"; then
        err "pg_dump failed"
        rm -f "$filepath"
        exit 1
    fi

    if [ ! -s "$filepath" ]; then
        err "Backup file is empty - dump likely failed silently"
        rm -f "$filepath"
        exit 1
    fi

    local size
    size="$(du -h "$filepath" | cut -f1)"
    log "Backup written: ${filepath} (${size})"

    # Optional: upload to Azure Blob Storage
    if [ "$AZURE_STORAGE_UPLOAD" = "true" ]; then
        if [ -z "$AZURE_STORAGE_ACCOUNT" ]; then
            err "AZURE_STORAGE_UPLOAD=true but AZURE_STORAGE_ACCOUNT is not set"
            exit 1
        fi
        log "Uploading to Azure Blob Storage (account: ${AZURE_STORAGE_ACCOUNT}, container: ${AZURE_STORAGE_CONTAINER})..."
        if az storage blob upload \
            --account-name "$AZURE_STORAGE_ACCOUNT" \
            --container-name "$AZURE_STORAGE_CONTAINER" \
            --name "$filename" \
            --file "$filepath" \
            --auth-mode login \
            --overwrite; then
            log "Upload complete."
        else
            err "Upload failed - local backup file is still retained at ${filepath}"
            # Don't exit 1 here: the local backup succeeded, which is the
            # more important half. A failed upload shouldn't be reported
            # as a total backup failure, but IS worth surfacing loudly.
        fi
    fi

    # Local retention cleanup
    find "$BACKUP_DIR" -name "netbox-*.sql.gz" -mtime "+${RETENTION_DAYS}" -delete
    log "Retention cleanup applied (keeping ${RETENTION_DAYS} days locally)."
    log "Backup complete."
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

case "${1:-}" in
    --install-cron)
        install_cron "${2:-}"
        ;;
    --uninstall-cron)
        uninstall_cron
        ;;
    "")
        run_backup
        ;;
    *)
        err "Unknown argument: $1"
        echo "Usage: $0 [--install-cron [\"schedule\"] | --uninstall-cron]"
        exit 1
        ;;
esac

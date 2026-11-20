#!/bin/sh
# Put a backup made by backup.sh back. Stop the app first, so nothing writes meanwhile:
#
#   docker compose stop app app-b hub dashboard
#   docker compose exec backup sh /scripts/restore.sh /backups/papertrader-2026-11-19_0500.dump
#   docker compose start app app-b hub dashboard
#
# The whole database is replaced in one transaction: if anything goes wrong, nothing
# changes. The model files from the same backup are put back too.
set -u

DATA_DIR="${DATA_DIR:-/data}"
dump="${1:-}"
if [ -z "$dump" ] || [ ! -f "$dump" ]; then
    echo "usage: restore.sh /backups/papertrader-<date>_<time>.dump"
    echo "backups available:"
    ls -1 /backups/papertrader-*.dump 2>/dev/null || echo "  none"
    exit 1
fi

if [ "${2:-}" != "--yes" ]; then
    printf 'This REPLACES the whole database with %s.\nType YES to continue: ' "$dump"
    read -r answer
    if [ "$answer" != "YES" ]; then
        echo "Cancelled. Nothing was changed."
        exit 1
    fi
fi

if ! pg_restore --clean --if-exists --no-owner --single-transaction --dbname="$PGDATABASE" "$dump"; then
    echo "RESTORE FAILED: the database was left as it was." >&2
    exit 1
fi
echo "database restored from $dump"

stamp=$(basename "$dump" .dump)
stamp=${stamp#papertrader-}
models="$(dirname "$dump")/models-$stamp.tgz"
if [ -f "$models" ]; then
    mkdir -p "$DATA_DIR" && tar -xzf "$models" -C "$DATA_DIR" && echo "model files restored from $models"
fi
echo "Done. Start the apps again: docker compose start app app-b hub dashboard"

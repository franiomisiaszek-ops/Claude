#!/bin/sh
# Daily backups, run by the "backup" service in docker-compose.yml.
# It uses the postgres image, so pg_dump always matches the database version.
#
#   backup.sh        run forever: one backup a day, after BACKUP_HOUR_UTC
#   backup.sh now    one backup now, then stop
#
# Each backup is two files in the backups folder:
#   papertrader-<date>_<time>.dump   the whole database (pg_dump custom format)
#   models-<date>_<time>.tgz         the trained pattern models (not the datasets: they
#                                    can be rebuilt from the database)
# Connection settings come from PGHOST, PGUSER, PGPASSWORD and PGDATABASE.
set -u

BACKUP_DIR="${BACKUP_DIR:-/backups}"
DATA_DIR="${DATA_DIR:-/data}"
KEEP="${BACKUP_KEEP:-7}"
HOUR="${BACKUP_HOUR_UTC:-5}"   # 05:00 UTC = midnight or 1 am New York, after every job

prune() {
    ls -1 "$BACKUP_DIR"/papertrader-*.dump 2>/dev/null | sort -r | tail -n +"$((KEEP + 1))" |
        while read -r old; do
            stamp=$(basename "$old" .dump)
            stamp=${stamp#papertrader-}
            rm -f "$old" "$BACKUP_DIR/models-$stamp.tgz"
            echo "removed old backup $stamp"
        done
}

backup() {
    stamp=$(date -u +%Y-%m-%d_%H%M)
    mkdir -p "$BACKUP_DIR" || return 1
    part="$BACKUP_DIR/.papertrader-$stamp.part"
    if ! pg_dump --format=custom --file="$part"; then
        rm -f "$part"
        echo "BACKUP FAILED: pg_dump could not dump the database" >&2
        return 1
    fi
    mv "$part" "$BACKUP_DIR/papertrader-$stamp.dump"
    if [ -d "$DATA_DIR/model" ] && ls -d "$DATA_DIR"/model/pm-* >/dev/null 2>&1; then
        (cd "$DATA_DIR" && tar -czf "$BACKUP_DIR/.models-$stamp.part" model/pm-*) &&
            mv "$BACKUP_DIR/.models-$stamp.part" "$BACKUP_DIR/models-$stamp.tgz" ||
            echo "warning: the model files could not be copied" >&2
    fi
    echo "backup written: papertrader-$stamp.dump ($(du -h "$BACKUP_DIR/papertrader-$stamp.dump" | cut -f1))"
    prune
}

if [ "${1:-}" = "now" ]; then
    backup
    exit $?
fi

echo "backup service: one backup a day after ${HOUR}:00 UTC into $BACKUP_DIR, keeping the newest $KEEP"
while true; do
    today=$(date -u +%Y-%m-%d)
    if [ "$(date -u +%H)" -ge "$HOUR" ] && ! ls "$BACKUP_DIR"/papertrader-"$today"_*.dump >/dev/null 2>&1; then
        backup || echo "will try again in 10 minutes" >&2
    fi
    sleep 600
done

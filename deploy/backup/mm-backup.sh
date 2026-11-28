#!/bin/sh
# Backups of the database to S3-compatible storage (ADR 0010, docs/runbooks/cloud-install.md).
#
#   mm-backup schedule                every night at MM_BACKUP_TIME (UTC): backup (default command)
#   mm-backup backup                  one backup now: pg_dump -> S3, then copies older than
#                                     MM_BACKUP_KEEP_DAYS are deleted (only after a successful upload)
#   mm-backup list                    backups in the bucket
#   mm-backup restore NAME|latest --yes
#                                     REPLACES the database $PGDATABASE on $PGHOST with the backup
#   mm-backup restore-check [NAME|latest]
#                                     restores into the throwaway database on $MM_RESTORE_CHECK_HOST
#                                     and compares it with the live one
#
# Every backup and restore check writes its result to the table backup_runs of the database; the worker
# tells the service and the superadmin in Telegram about a failure and about no fresh backup (ADR 0011).
#
# Connection: PGHOST, PGUSER, PGPASSWORD, PGDATABASE. Storage: MM_BACKUP_S3_ENDPOINT,
# MM_BACKUP_S3_REGION, MM_BACKUP_S3_BUCKET, MM_BACKUP_S3_ACCESS_KEY, MM_BACKUP_S3_SECRET_KEY,
# MM_BACKUP_S3_PREFIX. The bucket must exist; keys need list, read, write and delete.
set -eu

: "${PGHOST:?}" "${PGUSER:?}" "${PGPASSWORD:?}" "${PGDATABASE:?}"
: "${MM_BACKUP_S3_ENDPOINT:=}" "${MM_BACKUP_S3_REGION:=us-east-1}" "${MM_BACKUP_S3_BUCKET:=}"
: "${MM_BACKUP_S3_ACCESS_KEY:=}" "${MM_BACKUP_S3_SECRET_KEY:=}" "${MM_BACKUP_S3_PREFIX:=machine-monitor}"
: "${MM_BACKUP_TIME:=23:30}" "${MM_BACKUP_KEEP_DAYS:=30}" "${MM_RESTORE_CHECK_HOST:=restore-check-db}"

export RCLONE_CONFIG_S3_TYPE=s3 RCLONE_CONFIG_S3_PROVIDER=Other
export RCLONE_CONFIG_S3_ENDPOINT="$MM_BACKUP_S3_ENDPOINT" RCLONE_CONFIG_S3_REGION="$MM_BACKUP_S3_REGION"
export RCLONE_CONFIG_S3_ACCESS_KEY_ID="$MM_BACKUP_S3_ACCESS_KEY" RCLONE_CONFIG_S3_SECRET_ACCESS_KEY="$MM_BACKUP_S3_SECRET_KEY"
export RCLONE_CONFIG_S3_FORCE_PATH_STYLE=true RCLONE_CONFIG_S3_NO_CHECK_BUCKET=true
REMOTE="s3:$MM_BACKUP_S3_BUCKET/$MM_BACKUP_S3_PREFIX"
WORK=/work

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) backup: $*"; }
fail() {
    log "ERROR: $*" >&2
    echo "$*" > "$WORK/last-error" 2>/dev/null || true  # the reason goes into backup_runs
    exit 1
}

record() {  # KIND OK MESSAGE: the result of a run, for the notifications of the worker
    # psql expands :'variables' only in what it reads, not in -c
    echo "INSERT INTO backup_runs (kind, ok, message) VALUES (:'kind', :'ok', :'message')" |
        psql -q -X -v ON_ERROR_STOP=1 -v kind="$1" -v ok="$2" -v message="$3" > /dev/null 2>&1 ||
        log "WARNING: the result could not be written to the database (no notification about it)" >&2
}

recorded() {  # KIND COMMAND [ARGS]: runs the command in a fresh shell (set -e works there), records the result
    kind=$1
    shift
    rm -f "$WORK/last-error"
    status=0
    MM_BACKUP_INNER=1 "$0" "$@" || status=$?
    if [ "$status" -eq 0 ]; then
        record "$kind" true ""
    else
        record "$kind" false "$(cat "$WORK/last-error" 2> /dev/null || echo "exit status $status")"
    fi
    return "$status"
}

require_storage() {
    missing=""
    for name in MM_BACKUP_S3_ENDPOINT MM_BACKUP_S3_BUCKET MM_BACKUP_S3_ACCESS_KEY MM_BACKUP_S3_SECRET_KEY; do
        eval "value=\${$name}"
        [ -n "$value" ] || missing="$missing $name"
    done
    [ -z "$missing" ] || fail "backups are not configured, set in .env:$missing (docs/runbooks/cloud-install.md)"
}

backup() {
    require_storage
    name="$PGDATABASE-$(date -u +%Y%m%dT%H%M%SZ).dump"
    file="$WORK/$name"
    trap 'rm -f "$file"' EXIT
    log "dumping $PGDATABASE from $PGHOST"
    # TimescaleDB: pg_dump may warn about circular foreign keys of hypertables; that is expected
    pg_dump --format=custom --compress=6 --file="$file" "$PGDATABASE" || fail "pg_dump failed"
    pg_restore --list "$file" > /dev/null || fail "the dump is not readable"
    size=$(wc -c < "$file" | tr -d ' ')
    rclone copyto "$file" "$REMOTE/$name" || fail "upload to $REMOTE failed"
    log "uploaded $REMOTE/$name ($size bytes)"
    rm -f "$file"
    trap - EXIT
    # after a successful upload only: the newest copy always stays
    rclone delete --min-age "${MM_BACKUP_KEEP_DAYS}d" --include "*.dump" "$REMOTE/" ||
        fail "uploaded, but old copies could not be removed"
    log "copies older than $MM_BACKUP_KEEP_DAYS days removed"
}

seconds_until_next_run() {
    now=$(date -u +%s)
    target=$(date -u -d "$(date -u +%Y-%m-%d) $MM_BACKUP_TIME:00" +%s) || fail "MM_BACKUP_TIME must be HH:MM (UTC)"
    [ "$target" -gt "$now" ] || target=$((target + 86400))
    echo $((target - now))
}

schedule() {
    seconds_until_next_run > /dev/null
    # not configured: every night records a failure, so the service is told instead of a silent restart loop
    (require_storage) || log "WARNING: backups are not configured; every night will be recorded as failed" >&2
    log "nightly backups at $MM_BACKUP_TIME UTC to $REMOTE, kept $MM_BACKUP_KEEP_DAYS days"
    while true; do
        sleep "$(seconds_until_next_run)"
        if recorded backup backup; then log "nightly backup done"; else log "ERROR: nightly backup FAILED" >&2; fi
    done
}

list() {
    require_storage
    rclone lsl --include "*.dump" "$REMOTE/"
}

fetch() {  # NAME|latest -> path of the downloaded dump
    name=$1
    if [ "$name" = latest ]; then
        name=$(rclone lsf --files-only --include "*.dump" "$REMOTE/" | sort | tail -n 1)
        [ -n "$name" ] || fail "no backups in $REMOTE"
    fi
    rclone copyto "$REMOTE/$name" "$WORK/$name" || fail "cannot download $REMOTE/$name"
    echo "$WORK/$name"
}

restore_into() {  # HOST FILE: the database $PGDATABASE on HOST is created anew from the dump
    host=$1 file=$2
    waited=0
    until pg_isready -q -h "$host" -d postgres; do
        [ "$waited" -lt 120 ] || fail "the database server $host does not answer"
        sleep 1
        waited=$((waited + 1))
    done
    log "restoring $(basename "$file") into $PGDATABASE on $host"
    psql -q -h "$host" -d postgres -v ON_ERROR_STOP=1 \
        -c "DROP DATABASE IF EXISTS \"$PGDATABASE\" WITH (FORCE)" -c "CREATE DATABASE \"$PGDATABASE\""
    psql -q -h "$host" -d "$PGDATABASE" -v ON_ERROR_STOP=1 \
        -c "CREATE EXTENSION IF NOT EXISTS timescaledb" -c "SELECT timescaledb_pre_restore()" > /dev/null
    pg_restore --exit-on-error -h "$host" -d "$PGDATABASE" "$file"
    psql -q -h "$host" -d "$PGDATABASE" -v ON_ERROR_STOP=1 -c "SELECT timescaledb_post_restore()" > /dev/null
    log "restored: alembic revision $(psql -At -h "$host" -d "$PGDATABASE" -c 'SELECT version_num FROM alembic_version')"
}

restore() {
    [ "${2:-}" = "--yes" ] || fail "this REPLACES the database $PGDATABASE on $PGHOST; run: restore ${1:-latest} --yes"
    require_storage
    file=$(fetch "${1:-latest}")
    restore_into "$PGHOST" "$file"
    rm -f "$file"
}

# Small reference tables are compared whole; raw and derived data by the number of rows
# (the live database has gone on receiving data since the backup).
REFERENCE_TABLES="organizations sites areas collectors machines users memberships device_tokens shift_templates
downtime_reasons scrap_reasons programs state_rules downtime_annotations scrap_records repair_requests
maintenance_plans ballbar_tests alembic_version"
COUNTED_TABLES="telemetry machine_events state_intervals part_events cycles oee_hourly oee_shift"

restore_check() {
    require_storage
    file=$(fetch "${1:-latest}")
    restore_into "$MM_RESTORE_CHECK_HOST" "$file"
    rm -f "$file"
    differs=0
    printf '%-22s %-34s %s\n' table live backup
    for table in $REFERENCE_TABLES; do
        row="t::text"
        # a collector reports its heartbeat, version and buffer epoch all the time: not part of the comparison
        [ "$table" = collectors ] && row="(t.id, t.site_id, t.name)::text"
        query="SELECT md5(COALESCE(string_agg($row, '|' ORDER BY $row), '')) FROM $table t"
        live=$(psql -At -h "$PGHOST" -c "$query")
        restored=$(psql -At -h "$MM_RESTORE_CHECK_HOST" -c "$query")
        mark="same"
        [ "$live" = "$restored" ] || { mark="DIFFERS (changed after the backup?)"; differs=$((differs + 1)); }
        printf '%-22s %-34s %s\n' "$table" "$live" "$mark"
    done
    for table in $COUNTED_TABLES; do
        live=$(psql -At -h "$PGHOST" -c "SELECT count(*) FROM $table")
        restored=$(psql -At -h "$MM_RESTORE_CHECK_HOST" -c "SELECT count(*) FROM $table")
        printf '%-22s %-34s %s\n' "$table" "$live rows" "$restored rows"
    done
    log "restore check passed: the backup restores on a clean database; reference tables that differ: $differs"
}

command=${1:-schedule}
[ "$#" -gt 0 ] && shift
case "$command" in
    schedule) schedule ;;
    # run by hand (make backup-now, make restore-check) or by the schedule: the result is recorded
    backup) if [ -n "${MM_BACKUP_INNER:-}" ]; then backup; else recorded backup backup; fi ;;
    list) list ;;
    restore) restore "$@" ;;
    restore-check)
        if [ -n "${MM_BACKUP_INNER:-}" ]; then restore_check "$@"; else recorded restore_check restore-check "$@"; fi ;;
    *) fail "unknown command '$command' (schedule, backup, list, restore, restore-check)" ;;
esac

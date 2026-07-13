#!/bin/sh
set -eu

secret_file="${POSTGRES_PASSWORD_FILE:-/run/secrets/postgres_password}"
if [ ! -r "${secret_file}" ]; then
    echo "PostgreSQL password secret is not readable: ${secret_file}" >&2
    exit 1
fi

export PGPASSWORD="$(cat "${secret_file}")"
if [ -z "${PGPASSWORD}" ]; then
    echo "PostgreSQL password secret is empty" >&2
    exit 1
fi

retention_days="${AUDIT_RETENTION_DAYS:-365}"
interval_seconds="${AUDIT_RETENTION_INTERVAL_SECONDS:-86400}"

case "${retention_days}:${interval_seconds}" in
    *[!0-9:]*|:*|*:0)
        echo "Audit retention values must be positive integers" >&2
        exit 1
        ;;
esac

while true; do
    psql \
        --host "${POSTGRES_HOST:-postgres}" \
        --port "${POSTGRES_PORT:-5432}" \
        --username "${POSTGRES_USER:-rag}" \
        --dbname "${POSTGRES_DB:-rag}" \
        --set ON_ERROR_STOP=1 \
        --set retention_days="${retention_days}" <<'SQL'
SET lock_timeout = '5s';
SET statement_timeout = '10min';
WITH deleted AS (
    DELETE FROM audit_events
    WHERE created_at < EXTRACT(
        EPOCH FROM CURRENT_TIMESTAMP - (:'retention_days' || ' days')::interval
    )
    RETURNING 1
)
SELECT count(*) AS deleted_audit_events FROM deleted;
SQL
    sleep "${interval_seconds}"
done

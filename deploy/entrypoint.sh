#!/bin/sh
set -eu

read_secret() {
    variable="$1"
    file="$2"
    if [ -z "${file}" ] || [ ! -r "${file}" ]; then
        echo "Required secret for ${variable} is not readable: ${file}" >&2
        exit 1
    fi
    value="$(cat "${file}")"
    if [ -z "${value}" ]; then
        echo "Required secret for ${variable} is empty: ${file}" >&2
        exit 1
    fi
    export "${variable}=${value}"
}

read_secret POSTGRES_PASSWORD "${POSTGRES_PASSWORD_FILE:-/run/secrets/postgres_password}"
read_secret REDIS_PASSWORD "${REDIS_PASSWORD_FILE:-/run/secrets/redis_password}"
read_secret RAG_SECRET_KEY "${RAG_SECRET_KEY_FILE:-/run/secrets/app_secret_key}"

POSTGRES_HOST="${POSTGRES_HOST:-postgres}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"
POSTGRES_DB="${POSTGRES_DB:-rag}"
POSTGRES_USER="${POSTGRES_USER:-rag}"
REDIS_HOST="${REDIS_HOST:-redis}"
REDIS_PORT="${REDIS_PORT:-6379}"

encoded_postgres_password="$(python -c 'import os, urllib.parse; print(urllib.parse.quote(os.environ["POSTGRES_PASSWORD"], safe=""))')"
encoded_redis_password="$(python -c 'import os, urllib.parse; print(urllib.parse.quote(os.environ["REDIS_PASSWORD"], safe=""))')"
plain_database_url="postgresql://${POSTGRES_USER}:${encoded_postgres_password}@${POSTGRES_HOST}:${POSTGRES_PORT}/${POSTGRES_DB}"
export RAG_DATABASE_URL="${RAG_DATABASE_URL:-postgresql+psycopg://${POSTGRES_USER}:${encoded_postgres_password}@${POSTGRES_HOST}:${POSTGRES_PORT}/${POSTGRES_DB}}"
export DATABASE_URL="${DATABASE_URL:-${plain_database_url}}"
export RAG_REDIS_URL="${RAG_REDIS_URL:-redis://:${encoded_redis_password}@${REDIS_HOST}:${REDIS_PORT}/0}"
export REDIS_URL="${REDIS_URL:-${RAG_REDIS_URL}}"
export RESPONSE_CACHE_URL="${RESPONSE_CACHE_URL:-${RAG_REDIS_URL}}"

case "${1:-api}" in
    api)
        exec uvicorn asgi_app:app \
            --host "${RAG_HOST:-0.0.0.0}" \
            --port "${RAG_PORT:-8000}" \
            --workers "${WEB_CONCURRENCY:-2}" \
            --proxy-headers \
            --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-*}" \
            --no-server-header
        ;;
    migrate)
        alembic upgrade head
        exec python -c 'from db_store import run_migrations; run_migrations()'
        ;;
    worker)
        exec arq ingestion_worker.WorkerSettings
        ;;
    *)
        exec "$@"
        ;;
esac

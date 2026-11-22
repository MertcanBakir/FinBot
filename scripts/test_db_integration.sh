#!/usr/bin/env bash

set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KEEP_RUNNING=false
STARTED_BY_RUNNER=false

SQL_TESTS=(
    "tests/sql/analysis_backend_test.sql"
    "tests/sql/personal_analysis_auth_test.sql"
    "tests/sql/portfolio_transaction_rpc_integration.sql"
    "supabase/tests/portfolio_cash_account_model_test.sql"
    "tests/sql/portfolio_cash_reversal_snapshot_integration.sql"
    "tests/sql/portfolio_reset_rpc_integration.sql"
    "tests/sql/user_watchlist_rpc_test.sql"
    "tests/sql/portfolio_activity_rpc_test.sql"
    "tests/sql/mobile_system_health_rpc_test.sql"
)

usage() {
    cat <<'EOF'
Usage: ./scripts/test_db_integration.sh [--keep-running]

Rebuild the local Supabase database, run every SQL integration test, and run
the concurrent portfolio transaction test.

Options:
  --keep-running  Leave Supabase running when this script started it.
  -h, --help      Show this help message.
EOF
}

log() {
    printf '\n==> %s\n' "$1"
}

fail() {
    printf 'ERROR: %s\n' "$1" >&2
    exit 1
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || fail "Required command not found: $1"
}

cleanup() {
    local exit_code=$?

    if [[ "$STARTED_BY_RUNNER" == true && "$KEEP_RUNNING" == false ]]; then
        log "Stopping the local Supabase instance started by this runner"
        supabase stop --workdir "$PROJECT_ROOT" --no-backup || true
    fi

    exit "$exit_code"
}

while (($# > 0)); do
    case "$1" in
        --keep-running)
            KEEP_RUNNING=true
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            usage >&2
            fail "Unknown option: $1"
            ;;
    esac
    shift
done

trap cleanup EXIT

cd "$PROJECT_ROOT"

require_command docker
require_command supabase
require_command psql

if [[ -x "$PROJECT_ROOT/venv/bin/python" ]]; then
    PYTHON="$PROJECT_ROOT/venv/bin/python"
elif [[ -x "$PROJECT_ROOT/.venv/bin/python" ]]; then
    PYTHON="$PROJECT_ROOT/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON="$(command -v python3)"
else
    fail "Python was not found. Create venv/ or install Python 3.11."
fi

"$PYTHON" -c "import psycopg" >/dev/null 2>&1 || \
    fail "psycopg is missing. Install the development requirements first."

docker info >/dev/null 2>&1 || \
    fail "Docker is not running or its API is not accessible."

for test_file in "${SQL_TESTS[@]}"; do
    [[ -f "$PROJECT_ROOT/$test_file" ]] || fail "SQL test not found: $test_file"
done

if supabase status --workdir "$PROJECT_ROOT" -o json >/dev/null 2>&1; then
    log "Reusing the existing local Supabase instance"
else
    log "Starting a database-only local Supabase instance"
    STARTED_BY_RUNNER=true
    supabase start \
        --workdir "$PROJECT_ROOT" \
        --exclude edge-runtime,gotrue,imgproxy,kong,logflare,mailpit,postgres-meta,postgrest,realtime,storage-api,studio,supavisor,vector
fi

log "Rebuilding the local database from migrations and seed data"
supabase db reset --local --workdir "$PROJECT_ROOT" --yes

STATUS_JSON="$(supabase status --workdir "$PROJECT_ROOT" -o json)"
DB_URL="$(printf '%s' "$STATUS_JSON" | "$PYTHON" -c '
import json
import sys

status = json.load(sys.stdin)
value = status.get("DB_URL") or status.get("db_url")
if not value:
    raise SystemExit("Supabase status did not include DB_URL")
print(value)
')"

"$PYTHON" - "$DB_URL" <<'PY'
import sys
from urllib.parse import urlparse

dsn = sys.argv[1]
parsed = urlparse(dsn)
allowed_hosts = {"127.0.0.1", "localhost", "::1"}

if parsed.scheme not in {"postgres", "postgresql"}:
    raise SystemExit("Refusing database URL with a non-PostgreSQL scheme")
if parsed.hostname not in allowed_hosts:
    raise SystemExit(
        f"Refusing non-local database host: {parsed.hostname or '<missing>'}"
    )
PY

log "Running SQL integration tests"
for test_file in "${SQL_TESTS[@]}"; do
    printf '  -> %s\n' "$test_file"
    psql "$DB_URL" \
        -X \
        --set=ON_ERROR_STOP=1 \
        --file="$PROJECT_ROOT/$test_file" \
        >/dev/null
done

log "Running the concurrent portfolio transaction test"
"$PYTHON" tests/integration/portfolio_transaction_rpc_concurrency.py "$DB_URL"

log "Database integration suite passed (${#SQL_TESTS[@]} SQL files + 1 concurrency test)"


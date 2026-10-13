#!/usr/bin/env bash
# Real SQL engine E2E, exclusively disposable CI lab. NOT a HML test.
set -euo pipefail

if [[ "$GITHUB_ACTIONS" != "true" || "$GITHUB_REPOSITORY" != "ericson-j-santos/reqsys-data-platform" || "$RUNNER_OS" != "Linux" ]]; then
  echo "LAB_SCOPE_BLOCKED" >&2; exit 3
fi
if [[ ! "$SOURCE_SHA" =~ ^[0-9a-f]{40}$ || ! "$GITHUB_RUN_ID" =~ ^[0-9]+$ || ! "$GITHUB_RUN_ATTEMPT" =~ ^[0-9]+$ ]]; then
  echo "LAB_IDENTITY_BLOCKED" >&2; exit 3
fi
if [[ "$(git rev-parse HEAD)" != "$SOURCE_SHA" ]]; then
  echo "LAB_SOURCE_SHA_MISMATCH" >&2; exit 3
fi
IMAGE="mcr.microsoft.com/mssql/server@sha256:2b5b581621126574f3d1f75e78d3eebe8d05aedb59ad0cfdf9aa42cb0634d726"
LAB_CONTAINER="sql2025-lab-$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT"
LAB_DB="ChatGPT_SQL_Lab"
mkdir -p artifacts/sqlserver-lab
trap 'docker rm -f "$LAB_CONTAINER" >/dev/null 2>&1 || true' EXIT

# Random temporary lab credential, never printed or persisted.
read -r MSSQL_SA_PASSWORD < <(printf "Aa9!%s\n" "$(openssl rand -hex 24)")
export MSSQL_SA_PASSWORD
echo "::add-mask::$MSSQL_SA_PASSWORD"
docker pull "$IMAGE" >/dev/null
docker run --detach --network none --memory 4g --cpus 2 \
  --name "$LAB_CONTAINER" --hostname sql2025-lab \
  --env ACCEPT_EULA=Y --env MSSQL_PID=Developer --env MSSQL_SA_PASSWORD \
  "$IMAGE" >/dev/null

sql() {
  local query="$1"
  local database="$2"
  docker exec "$LAB_CONTAINER" /bin/bash -c \
    'printf -v SQLCMDPASSWORD %s "$MSSQL_SA_PASSWORD"; export SQLCMDPASSWORD; exec /opt/mssql-tools18/bin/sqlcmd -S localhost -U sa -C -b -V 16 -l 5 -h -1 -W -d "$2" -Q "SET NOCOUNT ON; $1"' \
    sqlcmd "$query" "$database"
}
scalar() {
  sql "$1" "$2" | tr -d '\r' | sed '/^[[:space:]]*$/d;s/^[[:space:]]*//;s/[[:space:]]*$//'
}
assert_same() {
  if [[ "$1" != "$2" ]]; then
    echo "LAB_ASSERTION_FAILED:$3" >&2; exit 1
  fi
}

READY=0
for _ in $(seq 1 45); do
  # Docker may report a created container even after sqlservr has crashed.
  if [[ "$(docker inspect --format '{{.State.Running}}' "$LAB_CONTAINER" 2>/dev/null || true)" != "true" ]]; then
    echo "LAB_CONTAINER_EXITED" >&2
    break
  fi
  if ! docker exec "$LAB_CONTAINER" test -x /opt/mssql-tools18/bin/sqlcmd >/dev/null 2>&1; then
    echo "LAB_SQLCMD_NOT_PRESENT" >&2
    break
  fi
  if sql "SELECT 1;" master >/dev/null 2>&1; then READY=1; break; fi
  sleep 4
done
if [[ "$READY" != "1" ]]; then
  echo "LAB_ENGINE_NOT_READY" >&2
  echo "LAB_CONTAINER_STATE=$(docker inspect --format '{{.State.Status}}' "$LAB_CONTAINER" 2>/dev/null || echo unknown)" >&2
  echo "LAB_CONTAINER_EXIT_CODE=$(docker inspect --format '{{.State.ExitCode}}' "$LAB_CONTAINER" 2>/dev/null || echo unknown)" >&2
  echo "LAB_CONTAINER_OOM_KILLED=$(docker inspect --format '{{.State.OOMKilled}}' "$LAB_CONTAINER" 2>/dev/null || echo unknown)" >&2
  # Only diagnostic labels and known engine startup messages; never dump env.
  docker logs --tail 100 "$LAB_CONTAINER" 2>&1 \
    | grep -E 'SQL Server is now ready|encountered a fatal error|Reason:|Message:|Error:|error:|Exception|ASSERT:|failed|Failure' \
    | head -n 24 >&2 || true
  exit 2
fi
ENGINE_VERSION="$(scalar "SELECT CONVERT(varchar(64), SERVERPROPERTY('ProductVersion'));" master)"
case "$ENGINE_VERSION" in 17.*) ;; *) echo "LAB_UNEXPECTED_ENGINE_VERSION" >&2; exit 1 ;; esac
EDITION="$(scalar "SELECT CONVERT(nvarchar(128), SERVERPROPERTY('Edition'));" master)"
case "$EDITION" in *Developer*) ;; *) echo "LAB_UNEXPECTED_EDITION" >&2; exit 1 ;; esac

sql "CREATE DATABASE [$LAB_DB];" master >/dev/null
sql "ALTER DATABASE [$LAB_DB] SET COMPATIBILITY_LEVEL = 170;" master >/dev/null
sql "ALTER DATABASE [$LAB_DB] SET QUERY_STORE = ON;" master >/dev/null
sql "ALTER DATABASE [$LAB_DB] SET QUERY_STORE (OPERATION_MODE = READ_WRITE);" master >/dev/null
assert_same "$(scalar "SELECT compatibility_level FROM sys.databases WHERE name=N'$LAB_DB';" master)" "170" "compatibility_170"
assert_same "$(scalar "SELECT actual_state_desc FROM sys.database_query_store_options;" "$LAB_DB")" "READ_WRITE" "query_store"

assert_same "$(scalar "SELECT COUNT(*) FROM OPENJSON(N'[{\"id\":1},{\"id\":2}]') WITH (id int '$.id') WHERE id > 0;" "$LAB_DB")" "2" "openjson_positive"
if sql "SELECT COUNT(*) FROM OPENJSON(N'{invalid');" "$LAB_DB" >/dev/null 2>&1; then
  echo "LAB_NEGATIVE_JSON_WAS_ACCEPTED" >&2; exit 1
fi

sql "CREATE TABLE dbo.LabEvidence (id int NOT NULL PRIMARY KEY, payload nvarchar(max) NOT NULL);" "$LAB_DB" >/dev/null
for _ in 1 2; do
  sql "INSERT INTO dbo.LabEvidence(id,payload)
       SELECT 1, N'{\"tenant\":\"A\"}'
       WHERE NOT EXISTS(SELECT 1 FROM dbo.LabEvidence WHERE id=1);" "$LAB_DB" >/dev/null
done
assert_same "$(scalar "SELECT COUNT(*) FROM dbo.LabEvidence;" "$LAB_DB")" "1" "idempotent_readback"
sql "BEGIN TRANSACTION; INSERT INTO dbo.LabEvidence(id,payload) VALUES(2,N'{\"rollback\":true}'); ROLLBACK TRANSACTION;" "$LAB_DB" >/dev/null
assert_same "$(scalar "SELECT COUNT(*) FROM dbo.LabEvidence;" "$LAB_DB")" "1" "rollback_readback"

assert_same "$(scalar "EXEC sys.sp_set_session_context @key=N'ci_tenant', @value=N'lab'; SELECT COALESCE(CONVERT(nvarchar(20), SESSION_CONTEXT(N'ci_tenant')), N'NULL');" "$LAB_DB")" "lab" "session_context_positive"
assert_same "$(scalar "SELECT COALESCE(CONVERT(nvarchar(20), SESSION_CONTEXT(N'ci_tenant')), N'NULL');" "$LAB_DB")" "NULL" "session_context_isolation"

export LAB_ENGINE_VERSION="$ENGINE_VERSION"
python3 - <<'PY'
import json
import os
from pathlib import Path

payload = {
  "schema_version": "1.0",
  "status": "validated",
  "environment": "CI_EPHEMERAL_SYNTHETIC",
  "hml_verified": False,
  "collector_odbc_e2e_verified": False,
  "sql_server_version": os.environ["LAB_ENGINE_VERSION"],
  "compatibility_level": 170,
  "query_store_state": "READ_WRITE",
  "image": "mcr.microsoft.com/mssql/server@sha256:2b5b581621126574f3d1f75e78d3eebe8d05aedb59ad0cfdf9aa42cb0634d726",
  "source_sha": os.environ["SOURCE_SHA"],
  "github_run_id": os.environ["GITHUB_RUN_ID"],
  "github_run_attempt": os.environ["GITHUB_RUN_ATTEMPT"],
  "correlation_id": "sqlserver-lab-" + os.environ["GITHUB_RUN_ID"] + "-" + os.environ["GITHUB_RUN_ATTEMPT"],
  "positive": ["engine_major_17", "compat_170", "query_store_read_write", "openjson", "idempotent_insert", "session_context"],
  "negative": ["malformed_openjson_rejected", "rollback_not_persisted", "session_context_isolation"],
  "readback": "independent_sqlcmd_connections",
  "destructive_target": "ephemeral_container_only"
}
Path("artifacts/sqlserver-lab/evidence.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print("SQLSERVER_LAB_E2E_VALIDATED run_id=" + payload["github_run_id"])
PY

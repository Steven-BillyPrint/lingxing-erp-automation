#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "The controlled deployment gate must run as root." >&2
  exit 2
fi

repository=/srv/lingxing-erp-automation/repo
runtime=/srv/lingxing-erp-automation/runtime
coordination_db="${runtime}/data/coordination.sqlite3"
lock_file=/run/lock/lingxing-erp-production-deploy.lock

exec 9>"${lock_file}"
if ! flock -n 9; then
  echo "Another production deployment is already running." >&2
  exit 3
fi

if [[ ! -d "${repository}/.git" ]]; then
  echo "Repository checkout is missing: ${repository}" >&2
  exit 2
fi

active_tasks="$(
  python3 - "${coordination_db}" <<'PY'
import sqlite3
import sys
import time
from pathlib import Path

path = Path(sys.argv[1])
if not path.is_file():
    print(0)
    raise SystemExit(0)
with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5) as connection:
    row = connection.execute(
        """
        SELECT COUNT(DISTINCT task_id)
        FROM coordination_leases
        WHERE task_id <> '' AND expires_at > ?
        """,
        (time.time(),),
    ).fetchone()
print(int(row[0] if row else 0))
PY
)"
if [[ "${active_tasks}" != "0" ]]; then
  echo "Deployment refused: ${active_tasks} active background task(s) still hold leases." >&2
  exit 4
fi

sudo -u admin -H bash "${repository}/deploy/server/deploy_current.sh"

expected_version="$(
  tr -d '\r\n' <"${repository}/CLIENT_VERSION"
)"
health_payload="$(
  curl --fail --silent --show-error \
    --connect-timeout 5 \
    --max-time 15 \
    http://127.0.0.1:18765/health
)"
python3 - "${expected_version}" "${health_payload}" <<'PY'
import json
import sys

expected = sys.argv[1]
payload = json.loads(sys.argv[2])
if payload.get("status") != "healthy":
    raise SystemExit("Coordinator health endpoint did not report healthy.")
actual = str(payload.get("required_client_version") or "")
if actual != expected:
    raise SystemExit(
        f"Coordinator requires {actual!r}; deployed repository declares {expected!r}."
    )
PY

deployed_commit="$(
  sudo -u admin -H git -C "${repository}" rev-parse HEAD
)"
echo "DEPLOYED_COMMIT=${deployed_commit}"
echo "DEPLOYED_VERSION=${expected_version}"
echo "DEPLOYMENT_HEALTH=healthy"


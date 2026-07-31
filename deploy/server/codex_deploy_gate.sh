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
health_payload=""
for attempt in $(seq 1 45); do
  if health_payload="$(
    curl --fail --silent \
      --connect-timeout 2 \
      --max-time 5 \
      http://127.0.0.1:18765/health
  )"; then
    break
  fi
  health_payload=""
  sleep 1
done
if [[ -z "${health_payload}" ]]; then
  echo "Coordinator health endpoint did not become ready within 45 seconds." >&2
  exit 5
fi
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
previous = str(payload.get("rollout_previous_client_version") or "")
if previous:
    try:
        previous_parts = tuple(map(int, previous.split(".")))
        expected_parts = tuple(map(int, expected.split(".")))
    except ValueError as exc:
        raise SystemExit("Coordinator reports an invalid rollout version.") from exc
    if len(previous_parts) != 4 or previous_parts >= expected_parts:
        raise SystemExit(
            "Coordinator rollout version is not older than the required version."
        )
remaining = payload.get("client_rollout_grace_remaining_seconds")
if not isinstance(remaining, int) or not 0 <= remaining <= 86_400:
    raise SystemExit("Coordinator reports an invalid rollout grace period.")
PY

deployed_commit="$(
  sudo -u admin -H git -C "${repository}" rev-parse HEAD
)"
echo "DEPLOYED_COMMIT=${deployed_commit}"
echo "DEPLOYED_VERSION=${expected_version}"
echo "DEPLOYMENT_HEALTH=healthy"


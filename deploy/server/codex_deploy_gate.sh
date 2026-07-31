#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "The controlled deployment gate must run as root." >&2
  exit 2
fi

if [[ "$#" -ne 2 ]] \
  || [[ ! "$1" =~ ^[0-9a-f]{40}$ ]] \
  || [[ ! "$2" =~ ^[0-9]{4}\.[0-9]{2}\.[0-9]{2}\.[0-9]+$ ]]; then
  echo "The controlled deployment gate requires an exact main commit and version." >&2
  exit 2
fi
expected_commit="$1"
expected_version="$2"

repository=/srv/lingxing-erp-automation/repo
runtime=/srv/lingxing-erp-automation/runtime
coordination_db="${runtime}/data/coordination.sqlite3"
lock_file=/run/lock/lingxing-erp-production-deploy.lock
deployed_commit_file=/etc/lingxing-erp/deployed-main-commit

exec 9>"${lock_file}"
if ! flock -n 9; then
  echo "Another production deployment is already running." >&2
  exit 3
fi

if [[ ! -d "${repository}/.git" ]]; then
  echo "Repository checkout is missing: ${repository}" >&2
  exit 2
fi

already_deployed=0
if [[ -f "${deployed_commit_file}" ]] \
  && [[ "$(tr -d '\r\n' <"${deployed_commit_file}")" == "${expected_commit}" ]] \
  && [[ "$(sudo -u admin -H git -C "${repository}" rev-parse HEAD)" == "${expected_commit}" ]] \
  && [[ "$(tr -d '\r\n' <"${repository}/CLIENT_VERSION")" == "${expected_version}" ]]; then
  current_health="$(
    curl --fail --silent \
      --connect-timeout 2 \
      --max-time 5 \
      http://127.0.0.1:18765/health
  )" || current_health=""
  if [[ -n "${current_health}" ]] && python3 - "${expected_version}" "${current_health}" <<'PY'
import json
import sys

expected, raw_payload = sys.argv[1:]
payload = json.loads(raw_payload)
if payload.get("status") != "healthy":
    raise SystemExit(1)
if str(payload.get("required_client_version") or "") != expected:
    raise SystemExit(1)
PY
  then
    already_deployed=1
  fi
fi

active_tasks=0
if [[ "${already_deployed}" != "1" ]]; then
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
fi
if [[ "${active_tasks}" != "0" ]]; then
  echo "Deployment refused: ${active_tasks} active background task(s) still hold leases." >&2
  exit 4
fi

sudo -u admin -H bash \
  "${repository}/deploy/server/deploy_current.sh" \
  "${expected_commit}" \
  "${expected_version}"

repository_version="$(
  tr -d '\r\n' <"${repository}/CLIENT_VERSION"
)"
if [[ "${repository_version}" != "${expected_version}" ]]; then
  echo "Deployed repository version does not match the authorized version." >&2
  exit 5
fi
expected_previous_version=""
if [[ -f /etc/lingxing-erp/previous-client-version ]]; then
  expected_previous_version="$(
    tr -d '\r\n' </etc/lingxing-erp/previous-client-version
  )"
fi
expected_rollout_deadline=""
if [[ -f /etc/lingxing-erp/client-rollout-deadline ]]; then
  expected_rollout_deadline="$(
    tr -d '\r\n' </etc/lingxing-erp/client-rollout-deadline
  )"
fi
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
python3 \
  - \
  "${expected_version}" \
  "${expected_previous_version}" \
  "${expected_rollout_deadline}" \
  "${health_payload}" <<'PY'
import json
import sys

expected, expected_previous, expected_deadline, raw_payload = sys.argv[1:]
payload = json.loads(raw_payload)
if payload.get("status") != "healthy":
    raise SystemExit("Coordinator health endpoint did not report healthy.")
actual = str(payload.get("required_client_version") or "")
if actual != expected:
    raise SystemExit(
        f"Coordinator requires {actual!r}; deployed repository declares {expected!r}."
    )
previous = str(payload.get("rollout_previous_client_version") or "")
if previous != expected_previous:
    raise SystemExit("Coordinator rollout version does not match persisted state.")
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
deadline = payload.get("client_rollout_grace_deadline_epoch")
if not isinstance(deadline, int) or deadline != int(expected_deadline or 0):
    raise SystemExit("Coordinator rollout deadline does not match persisted state.")
if previous and remaining <= 0:
    raise SystemExit("Coordinator rollout grace expired before deployment completed.")
if not previous and (remaining != 0 or deadline != 0):
    raise SystemExit("Coordinator unexpectedly opened a rollout grace.")
PY

deployed_commit="$(
  sudo -u admin -H git -C "${repository}" rev-parse HEAD
)"
if [[ "${deployed_commit}" != "${expected_commit}" ]]; then
  echo "Deployed repository commit does not match the authorized main commit." >&2
  exit 5
fi
echo "DEPLOYED_COMMIT=${deployed_commit}"
echo "DEPLOYED_VERSION=${expected_version}"
echo "DEPLOYMENT_HEALTH=healthy"


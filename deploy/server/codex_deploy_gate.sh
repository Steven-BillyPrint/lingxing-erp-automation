#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "The controlled deployment gate must run as root." >&2
  exit 2
fi

mode=deploy
expected_commit=""
expected_version=""
if [[ "$#" -eq 1 && "$1" == "--report-deployed" ]]; then
  mode=report
elif [[ "$#" -eq 3 && "$1" == "--activate-rollout" ]] \
  && [[ "$2" =~ ^[0-9a-f]{40}$ ]] \
  && [[ "$3" =~ ^[0-9]{4}\.[0-9]{2}\.[0-9]{2}\.[0-9]+$ ]]; then
  mode=activate
  expected_commit="$2"
  expected_version="$3"
elif [[ "$#" -eq 2 ]] \
  && [[ "$1" =~ ^[0-9a-f]{40}$ ]] \
  && [[ "$2" =~ ^[0-9]{4}\.[0-9]{2}\.[0-9]{2}\.[0-9]+$ ]]; then
  expected_commit="$1"
  expected_version="$2"
else
  echo "The controlled deployment gate requires an exact main commit and version." >&2
  exit 2
fi

repository=/srv/lingxing-erp-automation/repo
runtime=/srv/lingxing-erp-automation/runtime
coordination_db="${runtime}/data/coordination.sqlite3"
lock_file=/run/lock/lingxing-erp-production-deploy.lock
deployed_commit_file=/etc/lingxing-erp/deployed-main-commit
previous_client_version_file=/etc/lingxing-erp/previous-client-version
rollout_deadline_file=/etc/lingxing-erp/client-rollout-deadline
coordination_env=/etc/lingxing-erp/coordination.env

exec 9>"${lock_file}"
if ! flock -n 9; then
  echo "Another production deployment is already running." >&2
  exit 3
fi

install_rollout_marker() {
  local value="$1"
  python3 - "${rollout_deadline_file}" "${value}" <<'PY'
import os
import sys
from pathlib import Path

destination = Path(sys.argv[1])
value = sys.argv[2]
temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
try:
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o644,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(f"{value}\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, destination)
    directory_fd = os.open(destination.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
finally:
    temporary.unlink(missing_ok=True)
PY
}

deployment_drain_active() {
  python3 - "${coordination_db}" <<'PY'
import sqlite3
import sys
import time
from pathlib import Path

path = Path(sys.argv[1])
if not path.is_file():
    raise SystemExit("Coordination database is missing while checking deployment drain.")
with sqlite3.connect(path, timeout=15) as connection:
    row = connection.execute(
        """
        SELECT value
        FROM coordination_meta
        WHERE key = 'deployment_drain_until'
        """
    ).fetchone()
active = row is not None and int(row[0] or 0) > int(time.time())
print("true" if active else "false")
PY
}

if [[ "${mode}" == "report" ]]; then
  if [[ ! -f "${deployed_commit_file}" ]]; then
    echo "No verified production deployment receipt exists." >&2
    exit 5
  fi
  deployed_commit="$(tr -d '\r\n' <"${deployed_commit_file}")"
  if [[ ! "${deployed_commit}" =~ ^[0-9a-f]{40}$ ]]; then
    echo "The production deployment receipt is invalid." >&2
    exit 5
  fi
  report_health="$(
    curl --fail --silent \
      --connect-timeout 2 \
      --max-time 5 \
      http://127.0.0.1:18765/health
  )" || {
    echo "The deployed coordinator is not healthy." >&2
    exit 5
  }
  report_state="$(
    python3 - "${report_health}" <<'PY'
import json
import re
import sys

payload = json.loads(sys.argv[1])
if payload.get("status") != "healthy":
    raise SystemExit("The deployed coordinator did not report healthy.")
version = str(payload.get("required_client_version") or "")
if not re.fullmatch(r"\d{4}\.\d{2}\.\d{2}\.\d+", version):
    raise SystemExit("The deployed coordinator version is invalid.")
pending = payload.get("client_rollout_pending_activation", False)
if not isinstance(pending, bool):
    raise SystemExit("The deployed coordinator rollout state is invalid.")
print(version, "true" if pending else "false")
PY
  )"
  read -r deployed_version rollout_pending <<<"${report_state}"
  rollout_drain_active="$(deployment_drain_active)"
  echo "DEPLOYED_COMMIT=${deployed_commit}"
  echo "DEPLOYED_VERSION=${deployed_version}"
  echo "ROLLOUT_PENDING=${rollout_pending}"
  echo "ROLLOUT_DRAIN_ACTIVE=${rollout_drain_active}"
  echo "DEPLOYMENT_HEALTH=healthy"
  exit 0
fi

if [[ "${mode}" == "activate" ]]; then
  if [[ ! -f "${deployed_commit_file}" ]] \
    || [[ "$(tr -d '\r\n' <"${deployed_commit_file}")" != "${expected_commit}" ]]; then
    echo "Rollout activation does not match the deployed main commit." >&2
    exit 5
  fi
  if [[ ! -f "${coordination_db}" ]]; then
    echo "Coordination database is missing during rollout activation." >&2
    exit 5
  fi
  coordinator_active=0
  if systemctl is-active --quiet lingxing-erp-coordinator.service; then
    coordinator_active=1
  fi
  activation_tasks="$(
    python3 - "${coordination_db}" "${coordinator_active}" <<'PY'
import sqlite3
import sys
import time

path, active_text = sys.argv[1:]
active = active_text == "1"
with sqlite3.connect(path, timeout=15) as connection:
    connection.execute("BEGIN IMMEDIATE")
    now = time.time()
    if active:
        connection.execute(
            """
            DELETE FROM coordination_leases
            WHERE task_id = '' AND expires_at <= ?
            """,
            (now,),
        )
    else:
        # In-process background workers cannot survive a stopped coordinator.
        connection.execute("DELETE FROM coordination_leases")
    row = connection.execute(
        """
        SELECT COUNT(DISTINCT request_id)
        FROM coordination_leases
        WHERE (task_id <> '' AND ?)
           OR expires_at > ?
        """,
        (active, now),
    ).fetchone()
    active_tasks = int(row[0] if row else 0)
    # Even when existing work delays activation, stop admission of new work so
    # the next retry can finish after those tasks reach a terminal state. This
    # is intentionally persistent and is cleared only after verified activation
    # or deployment rollback; a wall-clock timeout could silently reopen writes
    # while the rollout transaction is still incomplete.
    connection.execute(
        """
        INSERT INTO coordination_meta(key, value)
        VALUES ('deployment_drain_until', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (4_102_444_800,),
    )
print(active_tasks)
PY
  )"
  if [[ "${activation_tasks}" != "0" ]]; then
    echo "Rollout activation deferred: ${activation_tasks} active task(s) are draining." >&2
    exit 4
  fi
  previous_version=""
  if [[ -f "${previous_client_version_file}" ]]; then
    previous_version="$(
      tr -d '\r\n' <"${previous_client_version_file}"
    )"
  fi
  if [[ -n "${previous_version}" ]] \
    && [[ ! "${previous_version}" =~ ^[0-9]{4}\.[0-9]{2}\.[0-9]{2}\.[0-9]+$ ]]; then
    echo "Persisted previous client version is invalid." >&2
    exit 5
  fi

  recover_pending_rollout() {
    if [[ -z "${previous_version}" ]]; then
      return 1
    fi
    install_rollout_marker pending
    if ! systemctl restart lingxing-erp-coordinator.service; then
      return 1
    fi
    local recovery_health=""
    for attempt in $(seq 1 45); do
      if recovery_health="$(
        curl --fail --silent \
          --connect-timeout 2 \
          --max-time 5 \
          http://127.0.0.1:18765/health
      )"; then
        if python3 \
          - \
          "${expected_version}" \
          "${previous_version}" \
          "${recovery_health}" <<'PY'
import json
import sys

required, previous, raw_payload = sys.argv[1:]
payload = json.loads(raw_payload)
if payload.get("status") != "healthy":
    raise SystemExit(1)
if str(payload.get("required_client_version") or "") != required:
    raise SystemExit(1)
if str(payload.get("rollout_previous_client_version") or "") != previous:
    raise SystemExit(1)
if payload.get("client_rollout_pending_activation") is not True:
    raise SystemExit(1)
if payload.get("client_rollout_grace_deadline_epoch") != 0:
    raise SystemExit(1)
PY
        then
          return 0
        fi
      fi
      recovery_health=""
      sleep 1
    done
    return 1
  }

  expected_deadline=0
  if [[ -n "${previous_version}" ]]; then
    rollout_marker=""
    if [[ -f "${rollout_deadline_file}" ]]; then
      rollout_marker="$(tr -d '\r\n' <"${rollout_deadline_file}")"
    fi
    if [[ "${rollout_marker}" == "pending" ]]; then
      grace_seconds="$(
        awk -F= \
          '$1 == "ERP_CLIENT_ROLLOUT_GRACE_SECONDS" { print $2 }' \
          "${coordination_env}"
      )"
      if [[ ! "${grace_seconds}" =~ ^[0-9]+$ ]] \
        || (( grace_seconds < 1 || grace_seconds > 86400 )); then
        echo "Persisted client rollout grace period is invalid." >&2
        exit 5
      fi
      expected_deadline="$(( $(date +%s) + grace_seconds ))"
      install_rollout_marker "${expected_deadline}"
    elif [[ "${rollout_marker}" =~ ^[0-9]+$ ]]; then
      expected_deadline="${rollout_marker}"
    else
      echo "Persisted client rollout activation marker is invalid." >&2
      exit 5
    fi
    # The deployment drain has admitted no background tasks, so restarting
    # here cannot interrupt work. The restart loads the now-absolute deadline.
    if ! systemctl restart lingxing-erp-coordinator.service; then
      if ! recover_pending_rollout; then
        echo "Rollout activation failed and pending-mode recovery also failed." >&2
        exit 6
      fi
      echo "Rollout activation failed; the coordinator recovered in pending mode." >&2
      exit 5
    fi
  elif ! systemctl is-active --quiet lingxing-erp-coordinator.service; then
    systemctl restart lingxing-erp-coordinator.service
  fi

  activation_health=""
  for attempt in $(seq 1 45); do
    if activation_health="$(
      curl --fail --silent \
        --connect-timeout 2 \
        --max-time 5 \
        http://127.0.0.1:18765/health
    )"; then
      break
    fi
    activation_health=""
    sleep 1
  done
  if [[ -z "${activation_health}" ]]; then
    if ! recover_pending_rollout; then
      echo "Rollout health failed and pending-mode recovery also failed." >&2
      exit 6
    fi
    echo "Coordinator recovered in pending mode after rollout health failed." >&2
    exit 5
  fi
  if ! python3 \
    - \
    "${expected_version}" \
    "${previous_version}" \
    "${expected_deadline}" \
    "${activation_health}" <<'PY'
import json
import sys

required, previous, expected_deadline, raw_payload = sys.argv[1:]
payload = json.loads(raw_payload)
if payload.get("status") != "healthy":
    raise SystemExit("Coordinator did not report healthy after rollout activation.")
if str(payload.get("required_client_version") or "") != required:
    raise SystemExit("Activated rollout requires the wrong client version.")
if str(payload.get("rollout_previous_client_version") or "") != previous:
    raise SystemExit("Activated rollout has the wrong previous client version.")
if payload.get("client_rollout_pending_activation") is not False:
    raise SystemExit("Coordinator rollout is still pending activation.")
deadline = payload.get("client_rollout_grace_deadline_epoch")
if not isinstance(deadline, int) or deadline != int(expected_deadline):
    raise SystemExit("Activated rollout deadline does not match persisted state.")
remaining = payload.get("client_rollout_grace_remaining_seconds")
if not isinstance(remaining, int) or remaining < 0:
    raise SystemExit("Activated rollout grace period is invalid.")
if not previous and (deadline != 0 or remaining != 0):
    raise SystemExit("Coordinator opened a rollout without a previous version.")
PY
  then
    if ! recover_pending_rollout; then
      echo "Rollout verification failed and pending-mode recovery also failed." >&2
      exit 6
    fi
    echo "Coordinator recovered in pending mode after rollout verification failed." >&2
    exit 5
  fi

  python3 - "${coordination_db}" <<'PY'
import sqlite3
import sys

with sqlite3.connect(sys.argv[1], timeout=15) as connection:
    connection.execute(
        """
        INSERT INTO coordination_meta(key, value)
        VALUES ('deployment_drain_until', 0)
        ON CONFLICT(key) DO UPDATE SET value = 0
        """
    )
PY
  if [[ "$(deployment_drain_active)" != "false" ]]; then
    echo "Verified rollout activation did not clear the deployment drain." >&2
    exit 6
  fi
  echo "DEPLOYED_COMMIT=${expected_commit}"
  echo "DEPLOYED_VERSION=${expected_version}"
  echo "ROLLOUT_PENDING=false"
  echo "ROLLOUT_DRAIN_ACTIVE=false"
  echo "ROLLOUT_ACTIVATED=true"
  echo "DEPLOYMENT_HEALTH=healthy"
  exit 0
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
  coordinator_active=0
  if systemctl is-active --quiet lingxing-erp-coordinator.service; then
    coordinator_active=1
  fi
  active_tasks="$(
    python3 - "${coordination_db}" "${coordinator_active}" <<'PY'
import sqlite3
import sys
import time
from pathlib import Path

path = Path(sys.argv[1])
coordinator_active = sys.argv[2] == "1"
if not path.is_file():
    print(0)
    raise SystemExit(0)
with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5) as connection:
    row = connection.execute(
        """
        SELECT COUNT(DISTINCT task_id)
        FROM coordination_leases
        WHERE task_id <> ''
          AND (? OR expires_at > ?)
        """,
        (coordinator_active, time.time()),
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
pending = payload.get("client_rollout_pending_activation")
if not isinstance(pending, bool):
    raise SystemExit("Coordinator reports an invalid rollout activation state.")
deadline = payload.get("client_rollout_grace_deadline_epoch")
expected_pending = expected_deadline == "pending"
expected_deadline_value = 0 if expected_pending else int(expected_deadline or 0)
if not isinstance(deadline, int) or deadline != expected_deadline_value:
    raise SystemExit("Coordinator rollout deadline does not match persisted state.")
if pending != expected_pending:
    raise SystemExit("Coordinator rollout activation state does not match persisted state.")
if previous and not pending and remaining <= 0:
    raise SystemExit("Coordinator rollout grace expired before deployment completed.")
if pending and (not previous or remaining != 0 or deadline != 0):
    raise SystemExit("Coordinator pending rollout state is inconsistent.")
if not previous and (remaining != 0 or deadline != 0 or pending):
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
if [[ "${expected_rollout_deadline}" == "pending" ]]; then
  if [[ "$(deployment_drain_active)" != "true" ]]; then
    echo "Pending rollout unexpectedly accepts new write operations." >&2
    exit 5
  fi
  echo "ROLLOUT_PENDING=true"
  echo "ROLLOUT_DRAIN_ACTIVE=true"
else
  if [[ "$(deployment_drain_active)" != "false" ]]; then
    echo "Completed rollout unexpectedly remains drained." >&2
    exit 5
  fi
  echo "ROLLOUT_PENDING=false"
  echo "ROLLOUT_DRAIN_ACTIVE=false"
fi
echo "DEPLOYMENT_HEALTH=healthy"


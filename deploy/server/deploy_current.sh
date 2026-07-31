#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ne 2 ]] \
  || [[ ! "$1" =~ ^[0-9a-f]{40}$ ]] \
  || [[ ! "$2" =~ ^[0-9]{4}\.[0-9]{2}\.[0-9]{2}\.[0-9]+$ ]]; then
  echo "Deployment requires an exact authorized main commit and client version." >&2
  exit 2
fi
expected_commit="$1"
expected_client_version="$2"

repository=/srv/lingxing-erp-automation/repo
runtime=/srv/lingxing-erp-automation/runtime
coordination_db="${runtime}/data/coordination.sqlite3"
current_image=lingxing-erp-coordinator:1.0
rollback_image=lingxing-erp-coordinator:rollback
service_stop_marker=/etc/lingxing-erp/deployment-in-progress
deployment_transaction_root=/etc/lingxing-erp/deploy-rollback
deployed_commit_file=/etc/lingxing-erp/deployed-main-commit

install_transaction_value() {
  local destination="$1"
  local value="$2"
  local temporary
  temporary="$(mktemp)"
  printf '%s\n' "${value}" >"${temporary}"
  sudo install -o root -g root -m 0600 \
    "${temporary}" \
    "${destination}"
  rm -f -- "${temporary}"
}

install_deployment_marker_state() {
  local value="$1"
  sudo python3 - "${service_stop_marker}" "${value}" <<'PY'
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
        0o600,
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

clear_deployment_drain_unconditionally() {
  if ! sudo test -f "${coordination_db}"; then
    return
  fi
  sudo python3 - "${coordination_db}" <<'PY'
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
}

backup_transaction_file() {
  local label="$1"
  local source="$2"
  local state_path="${deployment_transaction_root}/${label}.state"
  local backup_path="${deployment_transaction_root}/${label}.backup"
  if sudo test -e "${source}"; then
    sudo cp -a -- "${source}" "${backup_path}"
    install_transaction_value "${state_path}" present
  else
    install_transaction_value "${state_path}" absent
  fi
}

restore_transaction_file() {
  local label="$1"
  local destination="$2"
  local state_path="${deployment_transaction_root}/${label}.state"
  local backup_path="${deployment_transaction_root}/${label}.backup"
  local state
  if ! sudo test -f "${state_path}"; then
    echo "Deployment rollback metadata is missing for ${label}." >&2
    return 1
  fi
  state="$(sudo cat -- "${state_path}" | tr -d '\r\n')"
  case "${state}" in
    present)
      if ! sudo test -e "${backup_path}"; then
        echo "Deployment rollback copy is missing for ${label}." >&2
        return 1
      fi
      sudo cp -a -- "${backup_path}" "${destination}"
      ;;
    absent)
      sudo rm -f -- "${destination}"
      ;;
    *)
      echo "Deployment rollback metadata is invalid for ${label}." >&2
      return 1
      ;;
  esac
}

restore_optional_rollout_file() {
  local label="$1"
  local destination="$2"
  local state_path="${deployment_transaction_root}/${label}.state"
  local state
  if ! sudo test -f "${state_path}"; then
    echo "Deployment rollback metadata is missing for ${label}." >&2
    return 1
  fi
  state="$(sudo cat -- "${state_path}" | tr -d '\r\n')"
  if [[ "${state}" == "present" ]]; then
    restore_transaction_file "${label}" "${destination}"
  elif [[ "${state}" == "absent" ]]; then
    # The coordinator mounts the whole configuration directory. Keep neutral
    # marker files present so an older image that does not tolerate missing
    # optional rollout metadata can still start after rollback.
    install_transaction_value "${destination}" ""
  else
    echo "Deployment rollback metadata is invalid for ${label}." >&2
    return 1
  fi
}

record_service_state() {
  local service="$1"
  local label="$2"
  local active=0
  local enabled=0
  if sudo systemctl is-active --quiet "${service}"; then
    active=1
  fi
  if sudo systemctl is-enabled --quiet "${service}"; then
    enabled=1
  fi
  install_transaction_value \
    "${deployment_transaction_root}/${label}-active" \
    "${active}"
  install_transaction_value \
    "${deployment_transaction_root}/${label}-enabled" \
    "${enabled}"
}

restore_service_state() {
  local service="$1"
  local label="$2"
  local active
  local enabled
  active="$(
    sudo cat -- "${deployment_transaction_root}/${label}-active" \
      | tr -d '\r\n'
  )"
  enabled="$(
    sudo cat -- "${deployment_transaction_root}/${label}-enabled" \
      | tr -d '\r\n'
  )"
  if [[ "${enabled}" == "1" ]]; then
    sudo systemctl enable "${service}" >/dev/null
  elif [[ "${enabled}" == "0" ]]; then
    sudo systemctl disable "${service}" >/dev/null
  else
    echo "Deployment rollback enable state is invalid for ${service}." >&2
    return 1
  fi
  if [[ "${active}" == "1" ]]; then
    sudo systemctl restart "${service}"
    sudo systemctl is-active --quiet "${service}"
  elif [[ "${active}" == "0" ]]; then
    sudo systemctl stop "${service}"
  else
    echo "Deployment rollback active state is invalid for ${service}." >&2
    return 1
  fi
}

remove_deployment_transaction() {
  if sudo test -L "${deployment_transaction_root}"; then
    echo "Refusing to remove a symlinked deployment rollback directory." >&2
    return 1
  fi
  sudo rm -f -- "${service_stop_marker}"
  if sudo test -e "${deployment_transaction_root}"; then
    sudo rm -rf -- /etc/lingxing-erp/deploy-rollback
  fi
}

verify_restored_coordinator() {
  local previous_service_active
  local expected_previous_version
  local health_payload=""
  previous_service_active="$(
    sudo cat -- "${deployment_transaction_root}/coordinator-active" \
      | tr -d '\r\n'
  )"
  if [[ "${previous_service_active}" == "0" ]]; then
    return
  fi
  if [[ "${previous_service_active}" != "1" ]]; then
    echo "Deployment rollback coordinator state is invalid." >&2
    return 1
  fi
  expected_previous_version="$(
    sudo cat -- "${deployment_transaction_root}/previous-version" \
      | tr -d '\r\n'
  )"
  if [[ ! "${expected_previous_version}" =~ ^[0-9]{4}\.[0-9]{2}\.[0-9]{2}\.[0-9]+$ ]]; then
    echo "Deployment rollback coordinator version is invalid." >&2
    return 1
  fi
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
    echo "Restored coordinator did not become healthy within 45 seconds." >&2
    return 1
  fi
  python3 - "${expected_previous_version}" "${health_payload}" <<'PY'
import json
import sys

expected, raw_payload = sys.argv[1:]
payload = json.loads(raw_payload)
if payload.get("status") != "healthy":
    raise SystemExit("Restored coordinator did not report healthy.")
if str(payload.get("required_client_version") or "") != expected:
    raise SystemExit("Restored coordinator version does not match rollback state.")
PY
}

restore_interrupted_deployment() {
  echo "Recovering the previous coordinator after an interrupted deployment."
  if ! sudo test -d "${deployment_transaction_root}" \
    || sudo test -L "${deployment_transaction_root}"; then
    echo "Deployment rollback directory is missing or unsafe." >&2
    return 1
  fi
  restore_transaction_file coordination-env \
    /etc/lingxing-erp/coordination.env
  restore_transaction_file nas-service \
    /etc/systemd/system/lingxing-nas-sftp.service
  restore_transaction_file coordinator-service \
    /etc/systemd/system/lingxing-erp-coordinator.service
  restore_transaction_file cloudflared-service \
    /etc/systemd/system/lingxing-erp-cloudflared.service
  restore_transaction_file cloudflared-binary \
    /usr/local/bin/cloudflared
  restore_optional_rollout_file previous-client-version \
    /etc/lingxing-erp/previous-client-version
  restore_optional_rollout_file client-rollout-deadline \
    /etc/lingxing-erp/client-rollout-deadline
  restore_transaction_file deployed-main-commit \
    "${deployed_commit_file}"
  restore_transaction_file deploy-gate \
    /usr/local/sbin/lingxing-codex-deploy
  restore_transaction_file deploy-entry \
    /usr/local/sbin/lingxing-codex-deploy-entry

  local previous_service_active
  previous_service_active="$(
    sudo cat -- "${deployment_transaction_root}/coordinator-active" \
      | tr -d '\r\n'
  )"
  if [[ "${previous_service_active}" == "1" ]]; then
    local expected_previous_image_id
    local rollback_image_id
    expected_previous_image_id="$(
      sudo cat -- "${deployment_transaction_root}/previous-image-id" \
        | tr -d '\r\n'
    )"
    if ! sudo docker image inspect "${rollback_image}" >/dev/null 2>&1; then
      echo "Interrupted deployment has no verified rollback image." >&2
      return 1
    fi
    rollback_image_id="$(
      sudo docker image inspect \
        --format '{{.Id}}' \
        "${rollback_image}"
    )"
    if [[ ! "${expected_previous_image_id}" =~ ^sha256:[0-9a-f]{64}$ ]] \
      || [[ "${rollback_image_id}" != "${expected_previous_image_id}" ]]; then
      echo "Interrupted deployment rollback image identity changed." >&2
      return 1
    fi
    sudo docker tag "${rollback_image}" "${current_image}"
  elif [[ "${previous_service_active}" != "0" ]]; then
    echo "Deployment rollback coordinator state is invalid." >&2
    return 1
  fi

  sudo systemctl daemon-reload
  restore_service_state lingxing-nas-sftp.service nas
  restore_service_state lingxing-erp-coordinator.service coordinator
  restore_service_state lingxing-erp-cloudflared.service cloudflared
  verify_restored_coordinator
  clear_deployment_drain_unconditionally
  remove_deployment_transaction
}

committed_deployment_healthy() {
  local expected_recovery_commit
  local expected_recovery_version
  local recovery_health
  expected_recovery_commit="$(
    sudo cat -- "${deployment_transaction_root}/expected-commit" \
      | tr -d '\r\n'
  )"
  expected_recovery_version="$(
    sudo cat -- "${deployment_transaction_root}/expected-version" \
      | tr -d '\r\n'
  )"
  if [[ ! "${expected_recovery_commit}" =~ ^[0-9a-f]{40}$ ]] \
    || [[ ! "${expected_recovery_version}" =~ ^[0-9]{4}\.[0-9]{2}\.[0-9]{2}\.[0-9]+$ ]] \
    || ! sudo test -f "${deployed_commit_file}" \
    || [[ "$(
      sudo cat -- "${deployed_commit_file}" | tr -d '\r\n'
    )" != "${expected_recovery_commit}" ]]; then
    return 1
  fi
  recovery_health="$(
    curl --fail --silent \
      --connect-timeout 2 \
      --max-time 5 \
      http://127.0.0.1:18765/health
  )" || return 1
  python3 - "${expected_recovery_version}" "${recovery_health}" <<'PY'
import json
import sys

expected, raw_payload = sys.argv[1:]
payload = json.loads(raw_payload)
if payload.get("status") != "healthy":
    raise SystemExit(1)
if str(payload.get("required_client_version") or "") != expected:
    raise SystemExit(1)
PY
}

recover_deployment_transaction() {
  local interrupted_state
  interrupted_state="$(
    sudo cat -- "${service_stop_marker}" | tr -d '\r\n'
  )"
  if [[ "${interrupted_state}" == "committed" ]] \
    && committed_deployment_healthy; then
    echo "Finalizing a healthy deployment interrupted after commit."
    if [[ "$(
      sudo cat -- /etc/lingxing-erp/client-rollout-deadline 2>/dev/null \
        | tr -d '\r\n'
    )" != "pending" ]]; then
      clear_deployment_drain_unconditionally
    fi
    remove_deployment_transaction
  elif [[ "${interrupted_state}" == "stopping" ]] \
    || [[ "${interrupted_state}" == "committed" ]]; then
    restore_interrupted_deployment
  else
    echo "Interrupted deployment marker is invalid; refusing unsafe recovery." >&2
    return 1
  fi
}

if sudo test -f "${service_stop_marker}"; then
  recover_deployment_transaction
fi

if [[ ! -d "${repository}/.git" ]]; then
  echo "Repository checkout is missing: ${repository}" >&2
  exit 2
fi
if [[ -n "$(git -C "${repository}" status --porcelain --untracked-files=no)" ]]; then
  echo "Tracked server checkout changes must be reviewed before deployment." >&2
  exit 2
fi

previous_client_version=""
previous_coordinator_version=""
previous_service_active=0
if sudo systemctl is-active --quiet lingxing-erp-coordinator.service; then
  previous_service_active=1
  previous_health_payload="$(
    curl --fail --silent \
      --connect-timeout 2 \
      --max-time 5 \
      http://127.0.0.1:18765/health
  )" || {
    echo "Running coordinator did not provide its current version." >&2
    exit 2
  }
  previous_version_payload="$(
    python3 - "${previous_health_payload}" <<'PY'
import json
import re
import sys

payload = json.loads(sys.argv[1])
if payload.get("status") != "healthy":
    raise SystemExit("Running coordinator did not report healthy.")
value = str(payload.get("required_client_version") or "")
if not re.fullmatch(r"\d{4}\.\d{2}\.\d{2}\.\d+", value):
    raise SystemExit("Running coordinator reported an invalid client version.")
print(value)
rollout_previous = str(
    payload.get("rollout_previous_client_version") or ""
).strip()
if payload.get("client_rollout_pending_activation") is True:
    if not re.fullmatch(r"\d{4}\.\d{2}\.\d{2}\.\d+", rollout_previous):
        raise SystemExit(
            "Running coordinator reported an invalid pending stable version."
        )
    print(rollout_previous)
else:
    print(value)
PY
  )"
  previous_coordinator_version="$(
    sed -n '1p' <<<"${previous_version_payload}"
  )"
  previous_client_version="$(
    sed -n '2p' <<<"${previous_version_payload}"
  )"
elif [[ -s "${repository}/CLIENT_VERSION" ]]; then
  previous_coordinator_version="$(
    tr -d '\r\n' <"${repository}/CLIENT_VERSION"
  )"
  previous_client_version="${previous_coordinator_version}"
fi
git -C "${repository}" fetch origin main
remote_main_commit="$(
  git -C "${repository}" rev-parse origin/main
)"
if [[ "${remote_main_commit}" != "${expected_commit}" ]]; then
  echo "origin/main moved after release authorization; refusing a different commit." >&2
  exit 2
fi
git -C "${repository}" checkout main
git -C "${repository}" merge --ff-only origin/main
checked_out_commit="$(
  git -C "${repository}" rev-parse HEAD
)"
if [[ "${checked_out_commit}" != "${expected_commit}" ]]; then
  echo "Server checkout does not match the authorized main commit." >&2
  exit 2
fi
required_client_version="$(
  tr -d '\r\n' <"${repository}/CLIENT_VERSION"
)"
if [[ ! "${required_client_version}" =~ ^[0-9]{4}\.[0-9]{2}\.[0-9]{2}\.[0-9]+$ ]]; then
  echo "Repository CLIENT_VERSION is invalid: ${required_client_version}" >&2
  exit 2
fi
if [[ "${required_client_version}" != "${expected_client_version}" ]]; then
  echo "Repository CLIENT_VERSION does not match the authorized release." >&2
  exit 2
fi
persisted_deployed_commit=""
if sudo test -f "${deployed_commit_file}"; then
  persisted_deployed_commit="$(
    sudo cat -- "${deployed_commit_file}" | tr -d '\r\n'
  )"
fi
if [[ "${previous_service_active}" == "1" ]] \
  && [[ "${previous_coordinator_version}" == "${required_client_version}" ]] \
  && [[ "${persisted_deployed_commit}" == "${expected_commit}" ]]; then
  echo "Authorized commit is already deployed and healthy; preserving rollout state."
  exit 0
fi
rollout_grace_seconds="$(
  awk -F= \
    '$1 == "ERP_CLIENT_ROLLOUT_GRACE_SECONDS" { print $2 }' \
    "${repository}/deploy/server/coordination.env.example"
)"
if [[ ! "${rollout_grace_seconds}" =~ ^[0-9]+$ ]] \
  || (( rollout_grace_seconds < 1 || rollout_grace_seconds > 86400 )); then
  echo "Client rollout grace period is invalid: ${rollout_grace_seconds}" >&2
  exit 2
fi
candidate_image="lingxing-erp-coordinator:candidate-$(
  tr '.' '-' <<<"${required_client_version}"
)"

sudo install -d -o root -g root -m 0700 "${runtime}"
sudo install -d -o root -g root -m 0700 /etc/lingxing-erp

if ! sudo test -s /etc/lingxing-erp/host-key \
  || ! sudo test -s /etc/lingxing-erp/api-token; then
  echo "Run deploy/server/provision_debian.sh before deployment." >&2
  exit 2
fi
if ! sudo test -s /etc/lingxing-erp/cloudflare-access-audience; then
  echo "Cloudflare Access AUD tag is missing." >&2
  echo "Create /etc/lingxing-erp/cloudflare-access-audience with sudoedit." >&2
  exit 2
fi
if ! sudo test -s /etc/lingxing-erp/bootstrap-operator-email; then
  echo "The one-time legacy configuration recipient is not selected." >&2
  echo "Create /etc/lingxing-erp/bootstrap-operator-email with one approved @billyprint.com address." >&2
  exit 2
fi
if ! sudo test -s /etc/lingxing-erp/cloudflare-tunnel-token; then
  echo "The dedicated ERP Cloudflare Tunnel token is missing." >&2
  echo "Create /etc/lingxing-erp/cloudflare-tunnel-token as a root-only file." >&2
  exit 2
fi
tunnel_token_owner="$(sudo stat -c '%U:%G' /etc/lingxing-erp/cloudflare-tunnel-token)"
tunnel_token_mode="$(sudo stat -c '%a' /etc/lingxing-erp/cloudflare-tunnel-token)"
if [[ "${tunnel_token_owner}" != "root:root" || "${tunnel_token_mode}" != "600" ]]; then
  echo "Cloudflare Tunnel token must be owned by root:root with mode 0600." >&2
  exit 2
fi
if ! sudo test -s /etc/lingxing-erp/nas-sftp-password \
  || ! sudo test -s /etc/lingxing-erp/nas-sftp-known_hosts; then
  echo "NAS SFTP credentials or pinned host keys are missing." >&2
  exit 2
fi

if ! sudo mountpoint -q /mnt/lingxing-nas; then
  echo "NAS SFTP is not mounted at /mnt/lingxing-nas." >&2
  exit 2
fi
if ! sudo test -d "/mnt/lingxing-nas/Public/Amazon每日订单汇总"; then
  echo "NAS output directory is missing: Public/Amazon每日订单汇总" >&2
  exit 2
fi

sudo docker build \
  --file "${repository}/deploy/server/Dockerfile" \
  --tag "${candidate_image}" \
  "${repository}"

cloudflared_version=2026.7.3
cloudflared_sha256=9d71c677db00134c1bd4144b7783486b654ad281b1ea62b4972098d19f770f17
cloudflared_binary=/usr/local/bin/cloudflared
installed_cloudflared_sha256=""
staged_cloudflared_binary=""
cleanup_staged_artifacts() {
  if [[ -n "${staged_cloudflared_binary}" ]]; then
    rm -f -- "${staged_cloudflared_binary}"
  fi
  sudo docker image rm "${candidate_image}" >/dev/null 2>&1 || true
}
trap cleanup_staged_artifacts EXIT
if sudo test -f "${cloudflared_binary}"; then
  installed_cloudflared_sha256="$(
    sudo sha256sum "${cloudflared_binary}" | awk '{print $1}'
  )"
fi
if [[ "${installed_cloudflared_sha256}" == "${cloudflared_sha256}" ]]; then
  echo "Reusing verified cloudflared ${cloudflared_version}."
else
  staged_cloudflared_binary="$(mktemp)"
  curl -4 --fail --location --retry 8 --retry-all-errors --connect-timeout 15 \
    "https://github.com/cloudflare/cloudflared/releases/download/${cloudflared_version}/cloudflared-linux-amd64" \
    --output "${staged_cloudflared_binary}"
  printf '%s  %s\n' "${cloudflared_sha256}" "${staged_cloudflared_binary}" \
    | sha256sum --check --status
fi

deployment_drain_set=0

install_rollout_value() {
  local destination="$1"
  local value="$2"
  local temporary
  temporary="$(mktemp)"
  if [[ -n "${value}" ]]; then
    printf '%s\n' "${value}" >"${temporary}"
  fi
  sudo install -o root -g root -m 0644 \
    "${temporary}" \
    "${destination}"
  rm -f -- "${temporary}"
}

install_rollout_state() {
  install_rollout_value \
    /etc/lingxing-erp/previous-client-version \
    "$1"
  install_rollout_value \
    /etc/lingxing-erp/client-rollout-deadline \
    "$2"
}

clear_deployment_drain() {
  if [[ "${deployment_drain_set}" != "1" ]]; then
    return
  fi
  clear_deployment_drain_unconditionally
  deployment_drain_set=0
}

cleanup_deployment_transition() {
  local exit_code=$?
  local recovery_failed=0
  trap - EXIT
  set +e
  if sudo test -f "${service_stop_marker}"; then
    if ! (set -e; recover_deployment_transaction); then
      echo "Automatic deployment recovery failed; persistent rollback state was preserved." >&2
      recovery_failed=1
    fi
  elif sudo test -d "${deployment_transaction_root}"; then
    remove_deployment_transaction || recovery_failed=1
  fi
  if [[ -n "${staged_cloudflared_binary}" ]]; then
    rm -f -- "${staged_cloudflared_binary}"
  fi
  sudo docker image rm "${candidate_image}" >/dev/null 2>&1
  if [[ "${recovery_failed}" == "1" ]]; then
    exit 6
  fi
  exit "${exit_code}"
}
trap cleanup_deployment_transition EXIT

if sudo test -e "${deployment_transaction_root}"; then
  if sudo test -L "${deployment_transaction_root}"; then
    echo "Deployment rollback directory is an unsafe symlink." >&2
    exit 2
  fi
  sudo rm -rf -- /etc/lingxing-erp/deploy-rollback
fi
sudo install -d -o root -g root -m 0700 \
  "${deployment_transaction_root}"
backup_transaction_file coordination-env \
  /etc/lingxing-erp/coordination.env
backup_transaction_file nas-service \
  /etc/systemd/system/lingxing-nas-sftp.service
backup_transaction_file coordinator-service \
  /etc/systemd/system/lingxing-erp-coordinator.service
backup_transaction_file cloudflared-service \
  /etc/systemd/system/lingxing-erp-cloudflared.service
backup_transaction_file cloudflared-binary \
  /usr/local/bin/cloudflared
backup_transaction_file previous-client-version \
  /etc/lingxing-erp/previous-client-version
backup_transaction_file client-rollout-deadline \
  /etc/lingxing-erp/client-rollout-deadline
backup_transaction_file deployed-main-commit \
  "${deployed_commit_file}"
backup_transaction_file deploy-gate \
  /usr/local/sbin/lingxing-codex-deploy
backup_transaction_file deploy-entry \
  /usr/local/sbin/lingxing-codex-deploy-entry
record_service_state lingxing-nas-sftp.service nas
record_service_state lingxing-erp-coordinator.service coordinator
record_service_state lingxing-erp-cloudflared.service cloudflared
install_transaction_value \
  "${deployment_transaction_root}/expected-commit" \
  "${expected_commit}"
install_transaction_value \
  "${deployment_transaction_root}/expected-version" \
  "${expected_client_version}"
install_transaction_value \
  "${deployment_transaction_root}/previous-version" \
  "${previous_coordinator_version}"
install_transaction_value \
  "${deployment_transaction_root}/previous-image-id" \
  ""

if [[ "${previous_service_active}" == "1" ]]; then
  running_image_id="$(
    sudo docker inspect \
      --format '{{.Image}}' \
      lingxing-erp-coordinator
  )"
  if [[ -z "${running_image_id}" ]]; then
    echo "Could not identify the running coordinator image for rollback." >&2
    exit 2
  fi
  sudo docker image inspect "${running_image_id}" >/dev/null
  sudo docker tag "${running_image_id}" "${rollback_image}"
  install_transaction_value \
    "${deployment_transaction_root}/previous-image-id" \
    "${running_image_id}"
fi

drain_result="$(
  sudo python3 - "${coordination_db}" "${service_stop_marker}" <<'PY'
import sqlite3
import subprocess
import sys
import time
import os
from pathlib import Path

path = Path(sys.argv[1])
stop_marker = Path(sys.argv[2])
active = subprocess.run(
    ["systemctl", "is-active", "--quiet", "lingxing-erp-coordinator.service"],
    check=False,
).returncode == 0


def write_stop_marker() -> None:
    temporary = stop_marker.with_name(
        f".{stop_marker.name}.{os.getpid()}.tmp"
    )
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write("stopping\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, stop_marker)
        directory_fd = os.open(stop_marker.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


if not path.is_file():
    if active:
        raise SystemExit(
            "Coordinator is active but its coordination database is missing."
        )
    write_stop_marker()
    print("0 1")
    raise SystemExit(0)
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
        connection.execute(
            "DELETE FROM coordination_leases WHERE expires_at <= ?",
            (now,),
        )
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
    if active_tasks:
        connection.rollback()
        print(f"{active_tasks} 0")
        raise SystemExit(0)
    # Keep admission closed until the verified activation step (or rollback)
    # explicitly clears it. A timed drain could expire during an interrupted
    # release and let new work enter a half-finished rollout.
    connection.execute(
        """
        INSERT INTO coordination_meta(key, value)
        VALUES ('deployment_drain_until', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (4_102_444_800,),
    )
    write_stop_marker()
    # Keep the SQLite write lock while stopping the old service. Every task
    # submission acquires the same lock before it can create a lease, so even
    # the pre-drain server version cannot win a check-then-stop race.
    subprocess.run(
        ["systemctl", "stop", "lingxing-erp-coordinator.service"],
        check=True,
    )
    connection.commit()
print("0 1")
PY
)"
read -r active_tasks deployment_drain_set <<<"${drain_result}"
if [[ "${active_tasks}" != "0" ]]; then
  echo "Deployment refused after build: ${active_tasks} active write operation(s)." >&2
  exit 4
fi

if [[ "$(
  sudo cat -- "${deployment_transaction_root}/cloudflared-active" \
    | tr -d '\r\n'
)" == "1" ]]; then
  sudo systemctl stop lingxing-erp-cloudflared.service
fi
sudo install -o root -g root -m 0600 \
  "${repository}/deploy/server/coordination.env.example" \
  /etc/lingxing-erp/coordination.env
sudo install -o root -g root -m 0644 \
  "${repository}/deploy/server/lingxing-nas-sftp.service" \
  /etc/systemd/system/lingxing-nas-sftp.service
sudo install -o root -g root -m 0644 \
  "${repository}/deploy/server/lingxing-erp-coordinator.service" \
  /etc/systemd/system/lingxing-erp-coordinator.service
sudo install -o root -g root -m 0644 \
  "${repository}/deploy/server/lingxing-erp-cloudflared.service" \
  /etc/systemd/system/lingxing-erp-cloudflared.service
if [[ -n "${staged_cloudflared_binary}" ]]; then
  sudo install -o root -g root -m 0755 \
    "${staged_cloudflared_binary}" \
    "${cloudflared_binary}"
fi
sudo systemctl daemon-reload

rollout_previous_version=""
rollout_deadline_epoch=""
rollout_pending_activation=0
if [[ "${previous_client_version}" != "${required_client_version}" ]]; then
  rollout_previous_version="${previous_client_version}"
  if [[ -n "${rollout_previous_version}" ]]; then
    # Keep the previous client compatible until GitHub's stable latest URL
    # has been switched successfully. The separate activate-rollout command
    # replaces this marker with the absolute grace deadline.
    rollout_deadline_epoch="pending"
    rollout_pending_activation=1
  fi
fi
install_rollout_state \
  "${rollout_previous_version}" \
  "${rollout_deadline_epoch}"

sudo docker tag "${candidate_image}" "${current_image}"

sudo systemctl restart lingxing-nas-sftp.service
if ! sudo mountpoint -q /mnt/lingxing-nas \
  || ! sudo test -d "/mnt/lingxing-nas/Public/Amazon每日订单汇总"; then
  echo "NAS SFTP mount did not recover after restart." >&2
  exit 2
fi
sudo systemctl enable --now lingxing-erp-coordinator.service
sudo systemctl restart lingxing-erp-coordinator.service
sudo systemctl enable --now lingxing-erp-cloudflared.service
sudo systemctl restart lingxing-erp-cloudflared.service
sudo systemctl is-active --quiet lingxing-erp-coordinator.service
sudo systemctl is-active --quiet lingxing-erp-cloudflared.service

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
  echo "Candidate coordinator did not become healthy within 45 seconds." >&2
  exit 5
fi
python3 \
  - \
  "${required_client_version}" \
  "${rollout_previous_version}" \
  "${rollout_deadline_epoch}" \
  "${health_payload}" <<'PY'
import json
import sys

required, previous, expected_deadline, raw_payload = sys.argv[1:]
payload = json.loads(raw_payload)
if payload.get("status") != "healthy":
    raise SystemExit("Candidate coordinator did not report healthy.")
if str(payload.get("required_client_version") or "") != required:
    raise SystemExit("Candidate coordinator client version does not match.")
if str(payload.get("rollout_previous_client_version") or "") != previous:
    raise SystemExit("Candidate coordinator rollout version does not match.")
remaining = payload.get("client_rollout_grace_remaining_seconds")
if not isinstance(remaining, int) or not 0 <= remaining <= 86_400:
    raise SystemExit("Candidate coordinator rollout grace is invalid.")
pending = payload.get("client_rollout_pending_activation")
if not isinstance(pending, bool):
    raise SystemExit("Candidate coordinator rollout activation state is invalid.")
deadline = payload.get("client_rollout_grace_deadline_epoch")
if not isinstance(deadline, int):
    raise SystemExit("Candidate coordinator rollout deadline is invalid.")
expected_pending = expected_deadline == "pending"
expected_deadline_value = 0 if expected_pending else int(expected_deadline or 0)
if deadline != expected_deadline_value:
    raise SystemExit("Candidate coordinator rollout deadline does not match.")
if pending != expected_pending:
    raise SystemExit("Candidate coordinator rollout activation state does not match.")
if previous and not pending and remaining <= 0:
    raise SystemExit("Candidate coordinator rollout grace already expired.")
if pending and (not previous or remaining != 0 or deadline != 0):
    raise SystemExit("Candidate coordinator pending rollout state is inconsistent.")
if not previous and (remaining != 0 or pending):
    raise SystemExit("Candidate coordinator unexpectedly opened a rollout grace.")
PY

sudo install -o root -g root -m 0755 \
  "${repository}/deploy/server/codex_deploy_gate.sh" \
  /usr/local/sbin/lingxing-codex-deploy
sudo install -o root -g root -m 0755 \
  "${repository}/deploy/server/codex_deploy_entry.sh" \
  /usr/local/sbin/lingxing-codex-deploy-entry
install_rollout_value "${deployed_commit_file}" "${expected_commit}"
install_deployment_marker_state committed
if [[ "${rollout_pending_activation}" != "1" ]]; then
  clear_deployment_drain
fi
remove_deployment_transaction
if [[ -n "${staged_cloudflared_binary}" ]]; then
  rm -f -- "${staged_cloudflared_binary}"
  staged_cloudflared_binary=""
fi
sudo docker image rm "${candidate_image}" >/dev/null 2>&1 || true
trap - EXIT

sudo systemctl --no-pager --full status lingxing-erp-coordinator.service
sudo systemctl --no-pager --full status lingxing-erp-cloudflared.service

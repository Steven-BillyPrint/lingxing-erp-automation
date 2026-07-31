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
service_stop_marker=/run/lock/lingxing-erp-coordinator-deploy-stopped
deployed_commit_file=/etc/lingxing-erp/deployed-main-commit

if [[ ! -d "${repository}/.git" ]]; then
  echo "Repository checkout is missing: ${repository}" >&2
  exit 2
fi
if [[ -n "$(git -C "${repository}" status --porcelain --untracked-files=no)" ]]; then
  echo "Tracked server checkout changes must be reviewed before deployment." >&2
  exit 2
fi

if sudo test -f "${service_stop_marker}"; then
  if ! sudo systemctl is-active --quiet lingxing-erp-coordinator.service; then
    if ! sudo docker image inspect "${rollback_image}" >/dev/null 2>&1; then
      echo "An interrupted deployment stopped the coordinator without a rollback image." >&2
      exit 2
    fi
    sudo docker tag "${rollback_image}" "${current_image}"
    sudo systemctl restart lingxing-erp-coordinator.service
    sudo systemctl is-active --quiet lingxing-erp-coordinator.service
  fi
  sudo rm -f -- "${service_stop_marker}"
fi

previous_client_version=""
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
  previous_client_version="$(
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
PY
  )"
elif [[ -s "${repository}/CLIENT_VERSION" ]]; then
  previous_client_version="$(
    tr -d '\r\n' <"${repository}/CLIENT_VERSION"
  )"
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
    sudo tr -d '\r\n' <"${deployed_commit_file}"
  )"
fi
if [[ "${previous_service_active}" == "1" ]] \
  && [[ "${previous_client_version}" == "${required_client_version}" ]] \
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
sudo install -o root -g root -m 0600 \
  "${repository}/deploy/server/coordination.env.example" \
  /etc/lingxing-erp/coordination.env

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

sudo install -o root -g root -m 0644 \
  "${repository}/deploy/server/lingxing-nas-sftp.service" \
  /etc/systemd/system/lingxing-nas-sftp.service
sudo systemctl daemon-reload
sudo systemctl enable lingxing-nas-sftp.service
if ! sudo systemctl is-active --quiet lingxing-nas-sftp.service; then
  sudo systemctl start lingxing-nas-sftp.service
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
if sudo test -f "${cloudflared_binary}"; then
  installed_cloudflared_sha256="$(
    sudo sha256sum "${cloudflared_binary}" | awk '{print $1}'
  )"
fi
if [[ "${installed_cloudflared_sha256}" == "${cloudflared_sha256}" ]]; then
  echo "Reusing verified cloudflared ${cloudflared_version}."
else
  cloudflared_download="$(mktemp)"
  cleanup_cloudflared_download() {
    rm -f -- "${cloudflared_download}"
  }
  trap cleanup_cloudflared_download EXIT
  curl -4 --fail --location --retry 8 --retry-all-errors --connect-timeout 15 \
    "https://github.com/cloudflare/cloudflared/releases/download/${cloudflared_version}/cloudflared-linux-amd64" \
    --output "${cloudflared_download}"
  printf '%s  %s\n' "${cloudflared_sha256}" "${cloudflared_download}" \
    | sha256sum --check --status
  sudo install -o root -g root -m 0755 \
    "${cloudflared_download}" "${cloudflared_binary}"
  cleanup_cloudflared_download
  trap - EXIT
fi

sudo install -o root -g root -m 0644 \
  "${repository}/deploy/server/lingxing-erp-coordinator.service" \
  /etc/systemd/system/lingxing-erp-coordinator.service
sudo install -o root -g root -m 0644 \
  "${repository}/deploy/server/lingxing-erp-cloudflared.service" \
  /etc/systemd/system/lingxing-erp-cloudflared.service
sudo systemctl daemon-reload

original_rollout_version=""
if sudo test -f /etc/lingxing-erp/previous-client-version; then
  original_rollout_version="$(
    sudo cat /etc/lingxing-erp/previous-client-version
  )"
fi
original_rollout_deadline=""
if sudo test -f /etc/lingxing-erp/client-rollout-deadline; then
  original_rollout_deadline="$(
    sudo cat /etc/lingxing-erp/client-rollout-deadline
  )"
fi
deployment_drain_set=0
candidate_promoted=0
deployment_verified=0
rollback_available=0
rollout_file_changed=0

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
  if [[ "${deployment_drain_set}" != "1" ]] \
    || ! sudo test -f "${coordination_db}"; then
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
  deployment_drain_set=0
}

cleanup_deployment_transition() {
  local exit_code=$?
  trap - EXIT
  set +e
  if [[ "${deployment_verified}" != "1" ]]; then
    if [[ "${rollout_file_changed}" == "1" ]]; then
      install_rollout_state \
        "${original_rollout_version}" \
        "${original_rollout_deadline}"
    fi
    if [[ "${previous_service_active}" == "1" ]] \
      && sudo test -f "${service_stop_marker}"; then
      if [[ "${rollback_available}" == "1" ]]; then
        sudo docker tag "${rollback_image}" "${current_image}"
      fi
      sudo systemctl restart lingxing-erp-coordinator.service
      sudo systemctl is-active --quiet lingxing-erp-coordinator.service
    fi
  fi
  sudo rm -f -- "${service_stop_marker}"
  clear_deployment_drain
  sudo docker image rm "${candidate_image}" >/dev/null 2>&1
  exit "${exit_code}"
}
trap cleanup_deployment_transition EXIT

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
  rollback_available=1
fi

drain_result="$(
  sudo python3 - "${coordination_db}" "${service_stop_marker}" <<'PY'
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

path = Path(sys.argv[1])
stop_marker = Path(sys.argv[2])
if not path.is_file():
    active = subprocess.run(
        ["systemctl", "is-active", "--quiet", "lingxing-erp-coordinator.service"],
        check=False,
    ).returncode == 0
    if active:
        raise SystemExit(
            "Coordinator is active but its coordination database is missing."
        )
    print("0 0")
    raise SystemExit(0)
with sqlite3.connect(path, timeout=15) as connection:
    connection.execute("BEGIN IMMEDIATE")
    now = time.time()
    connection.execute(
        "DELETE FROM coordination_leases WHERE expires_at <= ?",
        (now,),
    )
    row = connection.execute(
        """
        SELECT COUNT(DISTINCT request_id)
        FROM coordination_leases
        WHERE expires_at > ?
        """,
        (now,),
    ).fetchone()
    active_tasks = int(row[0] if row else 0)
    if active_tasks:
        connection.rollback()
        print(f"{active_tasks} 0")
        raise SystemExit(0)
    connection.execute(
        """
        INSERT INTO coordination_meta(key, value)
        VALUES ('deployment_drain_until', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (int(now + 60 * 60),),
    )
    stop_marker.write_text("stopping\n", encoding="utf-8")
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

rollout_previous_version=""
rollout_deadline_epoch=""
if [[ "${previous_client_version}" != "${required_client_version}" ]]; then
  rollout_previous_version="${previous_client_version}"
  if [[ -n "${rollout_previous_version}" ]]; then
    rollout_deadline_epoch="$(
      printf '%s\n' "$(( $(date +%s) + rollout_grace_seconds ))"
    )"
  fi
fi
install_rollout_state \
  "${rollout_previous_version}" \
  "${rollout_deadline_epoch}"
rollout_file_changed=1

sudo docker tag "${candidate_image}" "${current_image}"
candidate_promoted=1

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
deadline = payload.get("client_rollout_grace_deadline_epoch")
if not isinstance(deadline, int):
    raise SystemExit("Candidate coordinator rollout deadline is invalid.")
expected_deadline_value = int(expected_deadline or 0)
if deadline != expected_deadline_value:
    raise SystemExit("Candidate coordinator rollout deadline does not match.")
if previous and remaining <= 0:
    raise SystemExit("Candidate coordinator rollout grace already expired.")
if not previous and remaining != 0:
    raise SystemExit("Candidate coordinator unexpectedly opened a rollout grace.")
PY

sudo install -o root -g root -m 0755 \
  "${repository}/deploy/server/codex_deploy_gate.sh" \
  /usr/local/sbin/lingxing-codex-deploy
sudo install -o root -g root -m 0755 \
  "${repository}/deploy/server/codex_deploy_entry.sh" \
  /usr/local/sbin/lingxing-codex-deploy-entry
clear_deployment_drain
sudo rm -f -- "${service_stop_marker}"
install_rollout_value "${deployed_commit_file}" "${expected_commit}"
deployment_verified=1
sudo docker image rm "${candidate_image}" >/dev/null 2>&1 || true
trap - EXIT

sudo systemctl --no-pager --full status lingxing-erp-coordinator.service
sudo systemctl --no-pager --full status lingxing-erp-cloudflared.service

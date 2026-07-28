#!/usr/bin/env bash
set -euo pipefail

repository=/srv/lingxing-erp-automation/repo
runtime=/srv/lingxing-erp-automation/runtime

if [[ ! -d "${repository}/.git" ]]; then
  echo "Repository checkout is missing: ${repository}" >&2
  exit 2
fi
if [[ -n "$(git -C "${repository}" status --porcelain --untracked-files=no)" ]]; then
  echo "Tracked server checkout changes must be reviewed before deployment." >&2
  exit 2
fi

git -C "${repository}" fetch origin main
git -C "${repository}" checkout main
git -C "${repository}" merge --ff-only origin/main

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
  --tag lingxing-erp-coordinator:1.0 \
  "${repository}"
cloudflared_version=2026.7.3
cloudflared_sha256=9d71c677db00134c1bd4144b7783486b654ad281b1ea62b4972098d19f770f17
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
  "${cloudflared_download}" /usr/local/bin/cloudflared
cleanup_cloudflared_download
trap - EXIT

sudo install -o root -g root -m 0644 \
  "${repository}/deploy/server/lingxing-erp-coordinator.service" \
  /etc/systemd/system/lingxing-erp-coordinator.service
sudo install -o root -g root -m 0644 \
  "${repository}/deploy/server/lingxing-erp-cloudflared.service" \
  /etc/systemd/system/lingxing-erp-cloudflared.service
sudo systemctl daemon-reload
sudo systemctl stop lingxing-erp-coordinator.service || true
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
sudo systemctl --no-pager --full status lingxing-erp-coordinator.service
sudo systemctl --no-pager --full status lingxing-erp-cloudflared.service

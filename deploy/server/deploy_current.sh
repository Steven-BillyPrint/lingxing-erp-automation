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

sudo docker build \
  --file "${repository}/deploy/server/Dockerfile" \
  --tag lingxing-erp-coordinator:1.0 \
  "${repository}"

sudo install -o root -g root -m 0644 \
  "${repository}/deploy/server/lingxing-erp-coordinator.service" \
  /etc/systemd/system/lingxing-erp-coordinator.service
sudo systemctl daemon-reload
sudo systemctl enable --now lingxing-erp-coordinator.service
sudo systemctl restart lingxing-erp-coordinator.service
sudo systemctl --no-pager --full status lingxing-erp-coordinator.service

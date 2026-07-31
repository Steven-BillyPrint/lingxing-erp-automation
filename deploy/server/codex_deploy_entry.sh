#!/usr/bin/env bash
set -euo pipefail

if [[ "${SSH_ORIGINAL_COMMAND:-}" != "deploy-main" ]]; then
  echo "This key is restricted to the reviewed production deployment command." >&2
  exit 2
fi

IFS=' ' read -r expected_commit expected_version unexpected || {
  echo "Deployment authorization payload is missing." >&2
  exit 2
}
if [[ -n "${unexpected:-}" ]] \
  || [[ ! "${expected_commit}" =~ ^[0-9a-f]{40}$ ]] \
  || [[ ! "${expected_version}" =~ ^[0-9]{4}\.[0-9]{2}\.[0-9]{2}\.[0-9]+$ ]]; then
  echo "Deployment authorization payload is invalid." >&2
  exit 2
fi

exec sudo -n /usr/local/sbin/lingxing-codex-deploy \
  "${expected_commit}" \
  "${expected_version}"


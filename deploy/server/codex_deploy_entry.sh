#!/usr/bin/env bash
set -euo pipefail

if [[ "${SSH_ORIGINAL_COMMAND:-}" != "deploy-main" ]]; then
  echo "This key is restricted to the reviewed production deployment command." >&2
  exit 2
fi

exec sudo -n /usr/local/sbin/lingxing-codex-deploy


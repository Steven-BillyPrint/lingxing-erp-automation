#!/usr/bin/env bash
set -euo pipefail

case "${SSH_ORIGINAL_COMMAND:-}" in
  deploy-main)
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
    ;;
  report-deployed)
    exec sudo -n /usr/local/sbin/lingxing-codex-deploy \
      --report-deployed
    ;;
  activate-rollout)
    IFS=' ' read -r expected_commit expected_version unexpected || {
      echo "Rollout activation authorization payload is missing." >&2
      exit 2
    }
    if [[ -n "${unexpected:-}" ]] \
      || [[ ! "${expected_commit}" =~ ^[0-9a-f]{40}$ ]] \
      || [[ ! "${expected_version}" =~ ^[0-9]{4}\.[0-9]{2}\.[0-9]{2}\.[0-9]+$ ]]; then
      echo "Rollout activation authorization payload is invalid." >&2
      exit 2
    fi
    exec sudo -n /usr/local/sbin/lingxing-codex-deploy \
      --activate-rollout \
      "${expected_commit}" \
      "${expected_version}"
    ;;
  *)
    echo "This key is restricted to reviewed production deployment operations." >&2
    exit 2
    ;;
esac


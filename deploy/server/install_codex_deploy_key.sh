#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ne 1 ]]; then
  echo "Usage: sudo bash install_codex_deploy_key.sh '<ssh-ed25519 public-key>'" >&2
  exit 2
fi
if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this installer with sudo." >&2
  exit 2
fi

repository=/srv/lingxing-erp-automation/repo
public_key="$1"
read -r key_type key_blob _comment <<<"${public_key}"
if [[ "${key_type}" != "ssh-ed25519" ]] \
  || [[ ! "${key_blob}" =~ ^[A-Za-z0-9+/]+={0,3}$ ]]; then
  echo "Only one valid Ed25519 public key is accepted." >&2
  exit 2
fi

install -o root -g root -m 0755 \
  "${repository}/deploy/server/codex_deploy_entry.sh" \
  /usr/local/sbin/lingxing-codex-deploy-entry
install -o root -g root -m 0755 \
  "${repository}/deploy/server/codex_deploy_gate.sh" \
  /usr/local/sbin/lingxing-codex-deploy
install -o root -g root -m 0440 \
  "${repository}/deploy/server/lingxing-codex-deploy.sudoers" \
  /etc/sudoers.d/lingxing-codex-deploy
visudo -cf /etc/sudoers.d/lingxing-codex-deploy >/dev/null

install -d -o admin -g admin -m 0700 /home/admin/.ssh
authorized_keys=/home/admin/.ssh/authorized_keys
temporary_keys="$(mktemp)"
cleanup() {
  rm -f -- "${temporary_keys}"
}
trap cleanup EXIT
if [[ -f "${authorized_keys}" ]]; then
  grep -v 'lingxing-codex-controlled-deploy$' \
    "${authorized_keys}" >"${temporary_keys}" || true
fi
printf '%s\n' \
  "restrict,command=\"/usr/local/sbin/lingxing-codex-deploy-entry\" ${key_type} ${key_blob} lingxing-codex-controlled-deploy" \
  >>"${temporary_keys}"
install -o admin -g admin -m 0600 "${temporary_keys}" "${authorized_keys}"

echo "Controlled Codex deployment key installed."


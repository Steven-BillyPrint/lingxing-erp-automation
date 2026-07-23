#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run this provisioning script with sudo." >&2
  exit 2
fi

. /etc/os-release
if [[ "${ID:-}" != "debian" || "${VERSION_CODENAME:-}" != "bullseye" ]]; then
  echo "This reviewed provisioner is limited to Debian 11 (bullseye)." >&2
  exit 2
fi

apt-get update
apt-get install -y ca-certificates curl
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg \
  -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc

architecture="$(dpkg --print-architecture)"
cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/debian
Suites: bullseye
Components: stable
Architectures: ${architecture}
Signed-By: /etc/apt/keyrings/docker.asc
EOF

apt-get update
apt-get install -y \
  docker-ce \
  docker-ce-cli \
  containerd.io \
  docker-buildx-plugin \
  docker-compose-plugin

install -d -o root -g root -m 0700 /etc/lingxing-erp
install -d -o root -g root -m 0700 \
  /srv/lingxing-erp-automation/runtime

if [[ ! -s /etc/lingxing-erp/host-key ]]; then
  openssl rand -base64 32 > /etc/lingxing-erp/host-key
fi
if [[ ! -s /etc/lingxing-erp/api-token ]]; then
  openssl rand -base64 48 > /etc/lingxing-erp/api-token
fi
chmod 0600 /etc/lingxing-erp/host-key /etc/lingxing-erp/api-token

systemctl enable --now docker
docker version

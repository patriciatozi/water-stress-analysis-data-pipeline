#!/usr/bin/env bash
# Run with sudo on a fresh Ubuntu 24.04 x86_64 EC2 host, or as EC2 user data.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo 'Run with sudo: sudo bash deployment/aws/bootstrap_ubuntu.sh' >&2
  exit 1
fi
. /etc/os-release
if [ "$ID" != ubuntu ] || [ "$VERSION_ID" != 24.04 ] || [ "$(uname -m)" != x86_64 ]; then
  echo 'This bootstrap supports Ubuntu 24.04 on x86_64 only.' >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl unzip git python3 python3-venv
install -m 0755 -d /etc/apt/keyrings
curl --fail --silent --show-error --retry 3 --connect-timeout 20 --max-time 120 \
  https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: noble
Components: stable
Architectures: amd64
Signed-By: /etc/apt/keyrings/docker.asc
EOF
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
usermod -aG docker ubuntu

# Keep host tooling separate from the pipeline's environment and OS Python.
python3 -m venv /opt/water-stress-tools
/opt/water-stress-tools/bin/pip install --disable-pip-version-check --timeout 60 --retries 3 \
  uv==0.12.2
ln -sfn /opt/water-stress-tools/bin/uv /usr/local/bin/uv

bootstrap_directory=$(mktemp -d)
trap 'rm -rf "$bootstrap_directory"' EXIT
curl --fail --silent --show-error --retry 3 --connect-timeout 20 --max-time 300 \
  https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip -o "$bootstrap_directory/aws.zip"
unzip -q "$bootstrap_directory/aws.zip" -d "$bootstrap_directory"
"$bootstrap_directory/aws/install" --update

docker compose version
aws --version
uv --version
echo 'Host ready. Reconnect SSH to activate the docker group; then restore data and start services.'

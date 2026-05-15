#!/usr/bin/env bash
# scripts/setup-host.sh — set up a fresh Ubuntu host for running snake_arena
# Tested on Ubuntu 24.04. Run with sudo or as a user with sudo privileges.

set -euo pipefail

# ---- config ----
INSTALL_GVISOR="${INSTALL_GVISOR:-1}"          # set to 0 to skip
INSTALL_UV="${INSTALL_UV:-1}"                  # set to 0 to skip
DOCKER_USER="${DOCKER_USER:-${SUDO_USER:-$USER}}"   # who gets added to the docker group

# ---- helpers ----
log() { printf '\n==> %s\n' "$*"; }

require_ubuntu() {
    if ! grep -qi ubuntu /etc/os-release; then
        echo "this script targets Ubuntu; refusing to run on other distros" >&2
        exit 1
    fi
}

# ---- main ----
require_ubuntu

log "updating apt"
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg lsb-release

# ---- Docker ----
log "installing docker"
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

UBUNTU_CODENAME="$(. /etc/os-release && echo "${VERSION_CODENAME}")"
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu ${UBUNTU_CODENAME} stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt-get update
sudo apt-get install -y --no-install-recommends \
    docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

sudo systemctl enable --now docker

log "adding ${DOCKER_USER} to docker group"
sudo usermod -aG docker "${DOCKER_USER}"

# ---- gVisor ----
if [[ "${INSTALL_GVISOR}" == "1" ]]; then
    log "installing gVisor (runsc)"
    curl -fsSL https://gvisor.dev/archive.key \
        | sudo gpg --dearmor -o /usr/share/keyrings/gvisor-archive-keyring.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] \
https://storage.googleapis.com/gvisor/releases release main" \
        | sudo tee /etc/apt/sources.list.d/gvisor.list > /dev/null

    sudo apt-get update
    sudo apt-get install -y --no-install-recommends runsc

    log "registering runsc with docker"
    sudo runsc install
    sudo systemctl restart docker

    log "verifying gVisor"
    if docker info 2>/dev/null | grep -q runsc; then
        echo "    runsc registered ✓"
    else
        echo "    warning: runsc not visible in 'docker info' — check /etc/docker/daemon.json" >&2
    fi
fi

# ---- Python / uv ----
if [[ "${INSTALL_UV}" == "1" ]]; then
    log "installing uv (Python package manager)"
    if ! command -v uv >/dev/null 2>&1; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
        echo "    uv installed to ~/.local/bin (ensure it's in your PATH)"
    else
        echo "    uv already installed at $(command -v uv)"
    fi
fi

# ---- summary ----
log "done"
cat <<EOF

next steps:
  - log out and back in for docker group membership to apply
    (or run: newgrp docker)
  - clone the repo and run: uv sync
  - build base images: ./scripts/build-base-images.sh
  - run hostile tests: ./scripts/run-hostile.sh

verify install:
  docker run --rm hello-world
$( [[ "${INSTALL_GVISOR}" == "1" ]] && echo "  docker run --rm --runtime=runsc alpine uname -a    # should mention gVisor" )
EOF
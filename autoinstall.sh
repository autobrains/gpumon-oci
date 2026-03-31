#!/bin/bash
set -euo pipefail

INSTALL_LOG="/var/log/gpumon.finished"
REPO_DIR="/root/gpumon"

install_all() {
    # ---- Docker ----
    if ! command -v docker > /dev/null 2>&1; then
        echo "[ $(date) ] Installing Docker..."
        curl -fsSL https://get.docker.com | sh
    else
        echo "[ $(date) ] Docker already installed: $(docker --version)"
    fi

    # ---- NVIDIA Container Toolkit (GPU instances only) ----
    if nvidia-smi > /dev/null 2>&1; then
        if ! dpkg -l | grep -q nvidia-container-toolkit; then
            echo "[ $(date) ] Installing NVIDIA Container Toolkit..."
            curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | \
                gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
            curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
                sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
                tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
            apt-get update && apt-get install -y nvidia-container-toolkit
            nvidia-ctk runtime configure --runtime=docker
            systemctl restart docker
        else
            echo "[ $(date) ] NVIDIA Container Toolkit already installed."
        fi
        GPU_INSTANCE=true
    else
        echo "[ $(date) ] No GPU detected, skipping NVIDIA Container Toolkit."
        GPU_INSTANCE=false
    fi

    # ---- Build image ----
    echo "[ $(date) ] Building Docker image..."
    docker compose -f "${REPO_DIR}/docker-compose.yml" build

    # ---- Start container ----
    echo "[ $(date) ] Starting gpumon container..."
    if [ "${GPU_INSTANCE}" = true ]; then
        docker compose -f "${REPO_DIR}/docker-compose.yml" up -d
    else
        docker compose \
            -f "${REPO_DIR}/docker-compose.yml" \
            -f "${REPO_DIR}/docker-compose.cpu.yml" \
            up -d
    fi

    echo "$(date)" >> "${INSTALL_LOG}"
    echo "[ $(date) ] Install complete. Check logs with: docker compose -f ${REPO_DIR}/docker-compose.yml logs -f"
}

install_date=$(cat "${INSTALL_LOG}" 2>/dev/null || true)
if [ -n "${install_date}" ]; then
    echo "[ $(date) ] Already installed at: ${install_date}. Skipping."
    echo "To reinstall, remove ${INSTALL_LOG} and re-run."
else
    install_all
fi

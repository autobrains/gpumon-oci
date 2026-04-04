#!/bin/bash
set -euo pipefail

INSTALL_LOG="/var/log/gpumon.finished"
REPO_DIR="/root/gpumon"

install_all() {
    # ---- Base dependencies (git, cron) ----
    PKGS_NEEDED=""
    command -v git  > /dev/null 2>&1 || PKGS_NEEDED="${PKGS_NEEDED} git"
    command -v cron > /dev/null 2>&1 || PKGS_NEEDED="${PKGS_NEEDED} cron"
    if [ -n "${PKGS_NEEDED}" ]; then
        echo "[ $(date) ] Installing:${PKGS_NEEDED}..."
        apt-get update && apt-get install -y --no-install-recommends ${PKGS_NEEDED}
    fi
    systemctl enable --now cron

    # ---- Docker ----
    if ! command -v docker > /dev/null 2>&1; then
        echo "[ $(date) ] Installing Docker..."
        curl -fsSL https://get.docker.com | sh
    else
        echo "[ $(date) ] Docker already installed: $(docker --version)"
    fi
    # Ensure the compose plugin is present — Ubuntu-repo docker does not bundle it
    if ! docker compose version > /dev/null 2>&1; then
        echo "[ $(date) ] docker compose plugin not found, adding Docker official repo and installing..."
        curl -fsSL https://download.docker.com/linux/ubuntu/gpg | \
            gpg --batch --yes --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg
        echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] \
            https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
            > /etc/apt/sources.list.d/docker.list
        apt-get update && apt-get install -y --no-install-recommends docker-compose-plugin
    fi
    systemctl enable --now docker
    # Wait for the daemon to be ready before running compose
    timeout 30 sh -c 'until docker info > /dev/null 2>&1; do sleep 1; done' \
        || { echo "[ $(date) ] ERROR: Docker daemon did not start in time"; exit 1; }

    # ---- NVIDIA Container Toolkit (GPU instances only) ----
    if nvidia-smi > /dev/null 2>&1; then
        if ! dpkg -l | grep -q nvidia-container-toolkit; then
            echo "[ $(date) ] Installing NVIDIA Container Toolkit..."
            curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | \
                gpg --batch --yes --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
            curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
                sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
                tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
            apt-get update && apt-get install -y nvidia-container-toolkit
            nvidia-ctk runtime configure --runtime=docker
            systemctl restart docker
            timeout 30 sh -c 'until docker info > /dev/null 2>&1; do sleep 1; done' \
                || { echo "[ $(date) ] ERROR: Docker daemon did not restart in time after NVIDIA config"; exit 1; }
        else
            echo "[ $(date) ] NVIDIA Container Toolkit already installed."
        fi
        GPU_INSTANCE=true
    else
        echo "[ $(date) ] No GPU detected, skipping NVIDIA Container Toolkit."
        GPU_INSTANCE=false
    fi

    # ---- Slack webhook env file ----
    if [ ! -f "${REPO_DIR}/.env" ]; then
        echo "[ $(date) ] WARNING: ${REPO_DIR}/.env not found. Slack alerts will not fire."
        echo "[ $(date) ] Copy ${REPO_DIR}/.env.example to ${REPO_DIR}/.env and fill in webhook URLs, then restart the container."
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

    # ---- Install halt_it.sh into host crontab ----
    echo "[ $(date) ] Installing halt_it.sh into host crontab..."
    cp "${REPO_DIR}/halt_it.sh" /usr/local/sbin/halt_it.sh
    chmod +x /usr/local/sbin/halt_it.sh
    (crontab -l 2>/dev/null | grep -v halt_it; echo "*/10 * * * * /usr/local/sbin/halt_it.sh >> /var/log/halt_it.log 2>&1") | crontab -
    echo "[ $(date) ] halt_it.sh scheduled via host crontab (every 10 min)."

    # ---- Systemd auto-update service + timer ----
    echo "[ $(date) ] Installing gpumon-oci auto-update systemd service..."

    cat > /usr/local/sbin/gpumon-oci-update.sh << EOF
#!/bin/bash
set -euo pipefail
REPO_DIR="${REPO_DIR}"

BEFORE=\$(git -C "\${REPO_DIR}" rev-parse HEAD)
git -C "\${REPO_DIR}" pull --force
AFTER=\$(git -C "\${REPO_DIR}" rev-parse HEAD)

if [ "\${BEFORE}" = "\${AFTER}" ]; then
    echo "[ \$(date) ] No changes pulled, container unchanged."
    exit 0
fi

echo "[ \$(date) ] New commits (\${BEFORE:0:7} -> \${AFTER:0:7}), rebuilding container..."

# Re-deploy halt_it.sh in case it changed
cp "\${REPO_DIR}/halt_it.sh" /usr/local/sbin/halt_it.sh
chmod +x /usr/local/sbin/halt_it.sh

# Rebuild and restart — detect GPU at update time
if nvidia-smi --list-gpus > /dev/null 2>&1; then
    docker compose -f "\${REPO_DIR}/docker-compose.yml" up -d --build
else
    docker compose \\
        -f "\${REPO_DIR}/docker-compose.yml" \\
        -f "\${REPO_DIR}/docker-compose.cpu.yml" \\
        up -d --build
fi

echo "[ \$(date) ] Container updated and restarted."
EOF
    chmod +x /usr/local/sbin/gpumon-oci-update.sh

    cat > /etc/systemd/system/gpumon-oci.service << 'EOF'
[Unit]
Description=gpumon-oci: pull latest from GitHub and restart container if changed
After=docker.service network-online.target
Requires=docker.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/gpumon-oci-update.sh
StandardOutput=journal
StandardError=journal
EOF

    cat > /etc/systemd/system/gpumon-oci.timer << 'EOF'
[Unit]
Description=Run gpumon-oci auto-update every hour

[Timer]
OnBootSec=5min
OnUnitActiveSec=1h
Unit=gpumon-oci.service

[Install]
WantedBy=timers.target
EOF

    systemctl daemon-reload
    systemctl enable --now gpumon-oci.timer
    echo "[ $(date) ] gpumon-oci.timer enabled (runs 5 min after boot, then every hour)."

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

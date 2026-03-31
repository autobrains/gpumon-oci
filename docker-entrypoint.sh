#!/bin/bash
set -euo pipefail

# Export all current env vars so cron jobs inherit them (Slack webhooks etc.)
printenv | grep -v "^_=" >> /etc/environment

# Seed halt_it.sh into the container's crontab
(crontab -l 2>/dev/null; echo "*/10 * * * * /bin/bash /app/halt_it.sh | /usr/bin/tee -a /tmp/halt_it_log.txt") | crontab -

# Start cron daemon in background
cron

# Detect GPU and exec into the appropriate monitor
if nvidia-smi --list-gpus > /dev/null 2>&1; then
    echo "[ $(date) ] GPU detected, starting gpumon.py"
    exec python3 /app/gpumon.py
else
    echo "[ $(date) ] No GPU detected, starting cpumon.py"
    exec python3 /app/cpumon.py
fi

#!/bin/bash
set -euo pipefail

# Detect GPU and exec into the appropriate monitor
if nvidia-smi --list-gpus > /dev/null 2>&1; then
    echo "[ $(date) ] GPU detected, starting gpumon.py"
    exec python3 /app/gpumon.py
else
    echo "[ $(date) ] No GPU detected, starting cpumon.py"
    exec python3 /app/cpumon.py
fi

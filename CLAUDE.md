# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

An idle-detection and auto-shutdown system for OCI (Oracle Cloud Infrastructure) GPU and CPU instances. When an instance appears idle (low CPU, GPU, and network utilization for ~2 hours), it sends a Slack alert and eventually stops itself via the OCI CLI.

## Running the Monitor

`docker-entrypoint.sh` auto-detects GPU via `nvidia-smi` and starts the right script. It also seeds `halt_it.sh` into the container's crontab and starts the cron daemon before exec-ing into Python.

**One-liner install on a fresh OCI instance:**
```bash
sudo git clone https://github.com/autobrains/gpumon-oci.git /root/gpumon/ && sudo bash /root/gpumon/autoinstall.sh
```

**Manual Docker commands:**
```bash
# GPU instance:
docker compose up -d --build

# CPU-only instance (no NVIDIA Container Toolkit):
docker compose -f docker-compose.yml -f docker-compose.cpu.yml up -d --build

# Logs:
docker compose logs -f

# Stop:
docker compose down
```

**Slack webhooks** — copy `.env.example` to `.env` and fill in your webhook URLs. The entrypoint writes env vars to `/etc/environment` so cron-invoked `halt_it.sh` inherits them.

**Note:** `wall` messages in `halt_it.sh` do not reach host terminal sessions from inside the container. Slack alerts are the primary notification.

### Legacy (systemd, pre-Docker)
```bash
# Service auto-selected gpumon.py or cpumon.py via nvidia-smi check
sudo systemctl status gpumon
```

## Architecture

### Two-phase shutdown flow

1. **Monitor phase** (`gpumon.py` / `cpumon.py`): Runs in a loop every 10s. Tracks CPU (per-core rolling average over 5 min), GPU utilization (NVML, GPU only), and network packets (psutil rolling window). When all metrics are below threshold after a `RESTART_BACKOFF` grace period, sets `alarm_pilot_light = 1` and logs it. Sends a Slack alert to the team webhook.
   - `gpumon.py` measures network as a cumulative packet delta over a 5-min sliding window (global `_net_samples` list).
   - `cpumon.py` measures network as packets-per-second per interval, summed over a 5-min rolling window.

2. **Shutdown phase** (`halt_it.sh`): Runs via cron every 10 minutes. Reads the most recent log file in `/tmp/`, checks if the last N lines show `alarm_pilot_light=1` with no activity spikes. If so, waits 3 minutes (broadcasting a `wall` message), then stops the instance via `oci compute instance action --action STOP --auth instance_principal`.

### Log files

- GPU instances write to: `/tmp/GPU_TEMP_{YYYY-MM-DDTHH}`
- CPU instances write to: `/tmp/CPUMON_LOGS_{YYYY-MM-DDTHH}`
- `halt_it.sh` reads from `/tmp/halt_it_oci.info` (cached IMDS metadata)
- `halt_it.sh` writes cooldown state to `/tmp/timestamp.txt`

### Policy via OCI freeform tags

The instance's freeform tags control behavior at runtime (fetched from OCI IMDS on startup):

| Tag | Values | Effect |
|-----|--------|--------|
| `GPUMON_POLICY` | `STANDARD` (default) / `SEVERE` | `SEVERE` lowers thresholds and shortens backoff from 2h to 10min |
| `Team` | e.g. `ML` | Selects Slack webhook env var `ML_TEAM_WEBHOOK_URL` |
| `Employee` | name | Logged to the metrics file |

### Slack webhooks (environment variables)

```bash
export DEBUG_WEBHOOK_URL="https://hooks.slack.com/..."        # fallback
export ML_TEAM_WEBHOOK_URL="https://hooks.slack.com/..."      # team-specific
```

### OCI authentication

`halt_it.sh` uses **instance principals** (`--auth instance_principal`). The instance's dynamic group must have an IAM policy granting:
```
allow dynamic-group <DG_NAME> to use instance-family in compartment <COMPARTMENT>
```

## Key thresholds (STANDARD policy)

- `RESTART_BACKOFF`: 7200s — no alarm fires before 2 hours of uptime
- `GPU_THRESHOLD`: 10% average GPU utilization
- `THRESHOLD_PERCENTAGE`: 10% average CPU utilization
- `NETWORK_THRESHOLD`: 10,000 packets over the rolling 5-minute window

## `halt_it.sh` log line requirements

`halt_it.sh` requires a minimum number of log lines before it will act — this effectively enforces ~4 hours of observed idle state:

| Instance type | `STEP` | ~Duration |
|---|---|---|
| CPU-only | 1000 | ~4 hours |
| 1–3 GPU | 1000 | ~4 hours |
| 4+ GPU | 4000 | ~4 hours (4 lines/loop) |

To cancel a pending shutdown from a wall broadcast, run:
```bash
sudo date +%s > /tmp/timestamp.txt
```
This resets the 2-hour cooldown timer (`/tmp/timestamp.txt`).

## Testing changes locally

Neither script imports cleanly without the full OCI IMDS endpoint and NVML libraries. For unit testing individual helpers (e.g., `get_packets_last_5m`, `calc_avg_core_utilization`), mock `psutil.net_io_counters()` and `psutil.cpu_percent()`.

```bash
pytest --cov=. --cov-report=term-missing
```

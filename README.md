# gpumon-oci

Idle-detection and auto-shutdown for OCI GPU and CPU instances. Monitors CPU, GPU, and network utilisation; sends a Slack alert when the instance appears idle; shuts it down via the OCI API after a configurable grace period.

## Quick install

Run as root (or with sudo) on a fresh Ubuntu instance:

```bash
sudo git clone https://github.com/autobrains/gpumon-oci.git /root/gpumon/ && sudo bash /root/gpumon/autoinstall.sh
```

`autoinstall.sh` is idempotent — re-running it on an already-installed instance is a no-op.

## What the installer does

1. Installs `git` and `cron` if missing
2. Installs Docker (if not present) and waits until the daemon is ready
3. Installs the NVIDIA Container Toolkit on GPU instances and waits for Docker to restart
4. Warns if `.env` is missing (Slack alerts will be silent until it is created)
5. Builds and starts the monitor container
6. Installs `halt_it.sh` into root's crontab (runs every 10 minutes)
7. Installs a systemd timer (`gpumon-oci.timer`) that pulls the latest code from GitHub every hour and rebuilds the container if anything changed

## Slack webhooks

Copy `.env.example` to `.env` and fill in your webhook URLs before (or just after) running the installer:

```bash
cp /root/gpumon/.env.example /root/gpumon/.env
# edit /root/gpumon/.env and set DEBUG_WEBHOOK_URL and/or <TEAM>_TEAM_WEBHOOK_URL
```

Then restart the container:

```bash
docker compose -f /root/gpumon/docker-compose.yml up -d
```

## Manual Docker commands

```bash
# GPU instance
docker compose -f /root/gpumon/docker-compose.yml up -d --build

# CPU-only instance
docker compose -f /root/gpumon/docker-compose.yml -f /root/gpumon/docker-compose.cpu.yml up -d --build

# Logs
docker compose -f /root/gpumon/docker-compose.yml logs -f

# Stop
docker compose -f /root/gpumon/docker-compose.yml down
```

## Cancelling a pending shutdown

If `halt_it.sh` has triggered a 3-minute countdown, run:

```bash
sudo date +%s > /tmp/timestamp.txt
```

This resets the 2-hour cooldown timer and cancels the current shutdown sequence.

## Checking status

```bash
# Monitor logs
docker compose -f /root/gpumon/docker-compose.yml logs -f

# halt_it.sh cron output
tail -f /var/log/halt_it.log

# Auto-update timer
systemctl status gpumon-oci.timer
journalctl -u gpumon-oci.service -n 50
```

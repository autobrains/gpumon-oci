#!/usr/bin/env python3
# Copyright 2017 Amazon...
# Rewritten for Oracle Cloud Infrastructure by Paul Seifer + ChatGPT (Python 3.9+)

# ---- Standard libs ----
import os
import time
import json
import subprocess
from datetime import datetime, timedelta
from time import sleep

# ---- Third-party libs ----
import psutil
import requests
try:
    from nvidia_ml_py import *
    nvml_available = True
except ImportError:
    try:
        from pynvml import *
        nvml_available = True
        print("Warning: Using deprecated pynvml. Install nvidia-ml-py instead.")
    except ImportError:
        nvml_available = False
        print("No NVML library available")

# ---- OCI SDK ----
try:
    import oci
except ImportError:
    raise SystemExit(
        "Missing dependency: oci (OCI Python SDK). Install with: pip install oci"
    )

# ==============================
# Tunables
# ==============================
CACHE_DURATION = 300
THRESHOLD_PERCENTAGE = 10
sleep_interval = 10

my_NameSpace = "GPU-metrics-with-team-tag"

# ==============================
# Network via psutil (5-minute rolling packets)  <<< NEW
# ==============================
NET_SAMPLE_INTERVAL = sleep_interval
NET_WINDOW_SECONDS = 300
_net_samples = []

def get_total_packets():
    c = psutil.net_io_counters()
    return int(c.packets_sent + c.packets_recv)

def get_packets_last_5m():
    global _net_samples
    now = time.time()
    total = get_total_packets()
    _net_samples.append((now, total))

    cutoff = now - NET_WINDOW_SECONDS
    _net_samples = [(t, v) for (t, v) in _net_samples if t >= cutoff]

    if len(_net_samples) < 2:
        return 0

    oldest_t, oldest_v = _net_samples[0]
    newest_t, newest_v = _net_samples[-1]
    return max(0, newest_v - oldest_v)

# ==============================
# Helpers: crontab
# ==============================
def check_root_crontab(search_string):
    try:
        result = subprocess.run(['sudo', 'crontab', '-l'], capture_output=True, text=True, check=True)
        return search_string in result.stdout
    except subprocess.CalledProcessError:
        return False
    except PermissionError:
        print("Permission denied reading root crontab.")
        return False

def add_to_root_crontab(new_cron_job):
    try:
        current = subprocess.run(['sudo', 'crontab', '-l'], capture_output=True, text=True, check=True)
        new_spec = current.stdout + new_cron_job + "\n"
        p = subprocess.Popen(['sudo', 'crontab', '-'], stdin=subprocess.PIPE, text=True)
        p.communicate(input=new_spec)
        print("New cron job added successfully." if p.returncode == 0 else "Failed to add new cron job.")
        return p.returncode == 0
    except Exception as e:
        print(f"Error updating root crontab: {e}")
        return False

# ==============================
# CPU sampling
# ==============================
core_utilization_cache = [[] for _ in range(psutil.cpu_count())]

def seconds_elapsed():
    return time.time() - psutil.boot_time()

def get_per_core_cpu_utilization():
    return psutil.cpu_percent(interval=1, percpu=True)

def calculate_average_core_utilization():
    return [sum(c)/len(c) if c else 0 for c in core_utilization_cache]

# ==============================
# OCI metadata (unchanged)
# ==============================
IMDS = "http://169.254.169.254/opc/v2"
IMDS_HEADERS = {"Authorization": "Bearer Oracle"}

def imds(path):
    url = f"{IMDS}{path}"
    r = requests.get(url, headers=IMDS_HEADERS, timeout=2)
    r.raise_for_status()
    try:
        return r.json()
    except Exception:
        return r.text

def get_instance_identity():
    data = imds("/instance/")
    return {
        "INSTANCE_ID": data.get("id"),
        "DISPLAY_NAME": data.get("displayName"),
        "REGION": data.get("canonicalRegionName") or data.get("region"),
        "COMPARTMENT_ID": data.get("compartmentId"),
        "IMAGE_ID": data.get("image"),
        "SHAPE": data.get("shape"),
        "HOSTNAME": data.get("hostname"),
        "FREEFORM_TAGS": data.get("freeformTags", {}),
        "DEFINED_TAGS": data.get("definedTags", {}),
    }

# ==============================
# Slack (unchanged)
# ==============================
def send_slack(webhook_url, message):
    try:
        payload = {"text": f"{message}"}
        r = requests.post(webhook_url, json=payload, timeout=5)
        if r.status_code != 200:
            print(f"Failed to send Slack message: HTTP {r.status_code}")
    except Exception as e:
        print(f"Slack error: {e}")

# ==============================
# NVML helpers (unchanged)
# ==============================
def getPowerDraw(handle):
    try:
        return float(nvmlDeviceGetPowerUsage(handle) / 1000.0)
    except NVMLError:
        return 0.0

def getTemp(handle):
    try:
        return int(nvmlDeviceGetTemperature(handle, NVML_TEMPERATURE_GPU))
    except NVMLError:
        return 0

def getUtilization(handle):
    try:
        util = nvmlDeviceGetUtilizationRates(handle)
        return util, float(util.gpu), float(util.memory)
    except NVMLError:
        class Dummy:
            gpu = 0
            memory = 0
        return Dummy(), 0.0, 0.0

# ==============================
# Main
# ==============================
def main():
    if check_root_crontab("halt_it.sh"):
        print("halt_it.sh presence in crontab detected, continue")
    else:
        print("updating crontab with new halt_it.sh call")
        add_to_root_crontab("*/10 * * * * bash /root/gpumon/halt_it.sh | tee -a /tmp/halt_it_log.txt")

    ident = get_instance_identity()
    print(ident)
    INSTANCE_ID = ident["INSTANCE_ID"]
    DISPLAY_NAME = ident["DISPLAY_NAME"]
    REGION = ident["REGION"]
    COMPARTMENT_ID = ident["COMPARTMENT_ID"]
    IMAGE_ID = ident["IMAGE_ID"]
    SHAPE = ident["SHAPE"]
    HOSTNAME = ident["HOSTNAME"]
    freeform = ident["FREEFORM_TAGS"]

    team = freeform.get("Team", "NO_TAG")
    emp_name = freeform.get("Employee", "NO_TAG")
    policy = freeform.get("GPUMON_POLICY", "STANDARD")

    if policy != "SEVERE":
        RESTART_BACKOFF = 7200
        THRESHOLD_PERCENTAGE_LOCAL = 10
        GPU_THRESHOLD = 10
        NETWORK_THRESHOLD = 10000
    else:
        RESTART_BACKOFF = 600
        THRESHOLD_PERCENTAGE_LOCAL = 40
        GPU_THRESHOLD = 10
        NETWORK_THRESHOLD = 200000

    debug_webhook = os.getenv("DEBUG_WEBHOOK_URL")
    team_var = f"{team}_TEAM_WEBHOOK_URL"
    team_webhook = os.getenv(team_var, debug_webhook)

    nvmlInit()
    deviceCount = nvmlDeviceGetCount()

    global core_utilization_cache

    TMP_FILE_SAVED = "/tmp/GPU_TEMP_" + datetime.now().strftime('%Y-%m-%dT%H')

    try:
        alarm_pilot_light = 0
        network_tripped = 0

        while True:
            cpu_util_tripped = False
            try:
                per_core = get_per_core_cpu_utilization()
                for i, core_util in enumerate(per_core):
                    core_utilization_cache[i].append(core_util)
                core_utilization_cache = [c[-int(CACHE_DURATION/1):] for c in core_utilization_cache]
                avg_core = calculate_average_core_utilization()
                THR = THRESHOLD_PERCENTAGE_LOCAL
                if any(u > THR for u in avg_core):
                    cpu_util_tripped = True
            except Exception as e:
                print(f"CPU sampling error: {e}")
                per_core = []
                avg_core = []

            total_gpu_util = 0.0
            for i in range(deviceCount):
                h = nvmlDeviceGetHandleByIndex(i)
                util, gpu_util, mem_util = getUtilization(h)
                total_gpu_util += gpu_util

            average_gpu_util = (total_gpu_util / deviceCount) if deviceCount > 0 else 0.0
            seconds = round(float(seconds_elapsed()))
            now = datetime.now()

            # >>> NETWORK via psutil (REPLACED)
            network = get_packets_last_5m()
            if network_tripped == 0 and network <= NETWORK_THRESHOLD:
                network_tripped = 1

            if seconds >= RESTART_BACKOFF:
                if round(average_gpu_util) <= GPU_THRESHOLD and not cpu_util_tripped and network <= NETWORK_THRESHOLD:
                    if alarm_pilot_light == 0:
                        alarm_pilot_light = 1
                        msg = (f"[ {now} ] INSTANCE: {DISPLAY_NAME} - {INSTANCE_ID} ({HOSTNAME}) "
                               f"CPU, GPU and NETWORK seem idle, TURNED ALARM PILOT LIGHT: ON, "
                               f"instance is expected to stop in: 3 hours")
                        if team_webhook:
                            send_slack(team_webhook, msg)
                else:
                    if alarm_pilot_light == 1:
                        alarm_pilot_light = 0
                        msg = (f"[ {now} ] INSTANCE: {DISPLAY_NAME} - {INSTANCE_ID} ({HOSTNAME}) "
                               f"CPU, GPU and NETWORK over minimum threshold, TURNED ALARM PILOT LIGHT: OFF")
                        if team_webhook:
                            send_slack(team_webhook, msg)
            else:
                alarm_pilot_light = 0

            for i in range(deviceCount):
                h = nvmlDeviceGetHandleByIndex(i)
                util, gpu_util, mem_util = getUtilization(h)
                pow_w = getPowerDraw(h)
                temp_c = getTemp(h)

                # *** CRITICAL LOG SECTION — UNCHANGED ***
                try:
                    with open(TMP_FILE_SAVED, 'a+') as f:
                        writeString = (
                            f"[ {now} ] tag:{team},Employee:{emp_name},GPU_ID:{i},"
                            f"GPU_Util:{gpu_util},MemUtil:{mem_util},powDrawStr:{pow_w},Temp:{temp_c},"
                            f"AverageGPUUtil:{average_gpu_util},Alarm_Pilot_value:{alarm_pilot_light},"
                            f"CPU_Util_Tripped:{cpu_util_tripped},Seconds:{seconds},"
                            f"Per-Core CPU Util:{per_core},NetworkStats:{network},Network_Tripped:{network_tripped}\n"
                        )
                        f.write(writeString)
                except Exception as e:
                    print(f"Log write error: {e}")

            sleep(sleep_interval)
    finally:
        nvmlShutdown()

if __name__ == "__main__":
    main()


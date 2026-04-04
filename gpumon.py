#!/usr/bin/env python3
# Copyright 2017 Amazon...
# Rewritten for Oracle Cloud Infrastructure by Paul Seifer + ChatGPT (Python 3.9+)

# ---- Standard libs ----
import os
import time
import json
from datetime import datetime, timedelta, timezone
from time import sleep

# ---- Third-party libs ----
import psutil
import requests
try:
    from nvidia_ml_py import (
        nvmlInit, nvmlShutdown, nvmlDeviceGetCount, nvmlDeviceGetHandleByIndex,
        nvmlDeviceGetPowerUsage, nvmlDeviceGetTemperature, nvmlDeviceGetUtilizationRates,
        NVMLError, NVML_TEMPERATURE_GPU,
    )
    nvml_available = True
except ImportError:
    try:
        from pynvml import (  # type: ignore[no-redef]
            nvmlInit, nvmlShutdown, nvmlDeviceGetCount, nvmlDeviceGetHandleByIndex,
            nvmlDeviceGetPowerUsage, nvmlDeviceGetTemperature, nvmlDeviceGetUtilizationRates,
            NVMLError, NVML_TEMPERATURE_GPU,
        )
        nvml_available = True
        print("Warning: Using deprecated pynvml. Install nvidia-ml-py instead.")
    except ImportError:
        nvml_available = False
        print("No NVML library available")

# ---- OCI SDK ----
try:
    import oci
    from oci.monitoring import MonitoringClient
    from oci.monitoring.models import PostMetricDataDetails, MetricDataDetails, Datapoint
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

METRICS_NAMESPACE = "gpu_metrics_with_team_tag"
METRICS_INTERVAL = 60  # post metrics every N seconds

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
# OCI Monitoring
# ==============================
def make_monitoring_client(region):
    signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
    return MonitoringClient(
        config={"region": region},
        signer=signer,
        service_endpoint=f"https://telemetry-ingestion.{region}.oraclecloud.com",
    )

def post_metrics(client, compartment_id, namespace, dimensions, metrics):
    """
    metrics: list of (name, value) tuples
    """
    now = datetime.now(timezone.utc)
    metric_data = [
        MetricDataDetails(
            namespace=namespace,
            compartment_id=compartment_id,
            name=name,
            dimensions=dimensions,
            datapoints=[Datapoint(timestamp=now, value=float(value))],
        )
        for name, value in metrics
    ]
    try:
        client.post_metric_data(PostMetricDataDetails(metric_data=metric_data))
    except Exception as e:
        print(f"Metrics upload error: {e}")

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

    monitoring_client = make_monitoring_client(REGION)
    base_dimensions = {
        "instanceId": INSTANCE_ID,
        "displayName": DISPLAY_NAME,
        "team": team,
        "employee": emp_name,
    }
    last_metrics_post = 0.0

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
                try:
                    h = nvmlDeviceGetHandleByIndex(i)
                except NVMLError as e:
                    print(f"nvmlDeviceGetHandleByIndex({i}) error: {e}")
                    continue
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
                try:
                    h = nvmlDeviceGetHandleByIndex(i)
                except NVMLError as e:
                    print(f"nvmlDeviceGetHandleByIndex({i}) error: {e}")
                    continue
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

            # Post to OCI Monitoring once per METRICS_INTERVAL
            if time.time() - last_metrics_post >= METRICS_INTERVAL:
                last_metrics_post = time.time()
                ram = psutil.virtual_memory()
                disk = psutil.disk_usage('/')
                avg_cpu = sum(avg_core) / len(avg_core) if avg_core else 0.0

                # Instance-level metrics
                post_metrics(monitoring_client, COMPARTMENT_ID, METRICS_NAMESPACE, base_dimensions, [
                    ("CpuUtilization",    avg_cpu),
                    ("MemoryUtilization", ram.percent),
                    ("DiskUtilization",   disk.percent),
                    ("NetworkPackets5m",  network),
                ])

                # Per-GPU metrics
                for i in range(deviceCount):
                    h = nvmlDeviceGetHandleByIndex(i)
                    _, gpu_util_m, mem_util_m = getUtilization(h)
                    pow_w_m = getPowerDraw(h)
                    temp_c_m = getTemp(h)
                    gpu_dims = {**base_dimensions, "gpu": str(i)}
                    post_metrics(monitoring_client, COMPARTMENT_ID, METRICS_NAMESPACE, gpu_dims, [
                        ("GpuUtilization",       gpu_util_m),
                        ("GpuMemoryUtilization", mem_util_m),
                        ("GpuPowerDraw",         pow_w_m),
                        ("GpuTemperature",       temp_c_m),
                    ])

            sleep(sleep_interval)
    finally:
        nvmlShutdown()

if __name__ == "__main__":
    main()


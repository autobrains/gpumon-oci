#!/usr/bin/env python3
# Copyright 2017 Amazon...
# Rewritten for Oracle Cloud Infrastructure by Paul Seifer + ChatGPT (Python 3.9+)

import os
import time
import json
import subprocess
from datetime import datetime, timedelta
from time import sleep

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

try:
    import oci
except ImportError:
    raise SystemExit("Missing dependency: oci (OCI Python SDK). Install with: pip install oci")

# ==============================
# Tunables
# ==============================
CACHE_DURATION = 300
THRESHOLD_PERCENTAGE = 10
sleep_interval = 10

my_NameSpace = "GPU-metrics-with-team-tag"

# ==============================
# Helpers: crontab
# ==============================
def check_root_crontab(search_string):
    try:
        result = subprocess.run(['sudo', 'crontab', '-l'], capture_output=True, text=True, check=True)
        return search_string in result.stdout
    except Exception:
        return False

def add_to_root_crontab(new_cron_job):
    try:
        current = subprocess.run(['sudo', 'crontab', '-l'], capture_output=True, text=True, check=True)
        new_spec = current.stdout + new_cron_job + "\n"
        p = subprocess.Popen(['sudo', 'crontab', '-'], stdin=subprocess.PIPE, text=True)
        p.communicate(input=new_spec)
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
# OCI metadata (IMDS v2)
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

def get_vnic_ocids():
    try:
        vnics = imds("/vnics/")
        vnic_ids = []
        if isinstance(vnics, list):
            for v in vnics:
                vid = v.get("vnicId") or v.get("vnicOCID") or v.get("vnicid")
                if vid:
                    vnic_ids.append(vid)
        return vnic_ids
    except Exception as e:
        print(f"Could not read VNIC metadata: {e}")
        return []

# ==============================
# Slack
# ==============================
def send_slack(webhook_url, message):
    try:
        payload = {"text": message}
        r = requests.post(webhook_url, json=payload, timeout=5)
        if r.status_code != 200:
            print(f"Failed to send Slack message: HTTP {r.status_code}")
    except Exception as e:
        print(f"Slack error: {e}")

# ==============================
# OCI SDK Clients
# ==============================
def make_oci_clients(region):
    signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
    config = {"region": region, "tenancy": signer.tenancy_id}

    mon_query = oci.monitoring.MonitoringClient(config=config, signer=signer)

    ingest_endpoint = f"https://telemetry-ingestion.{region}.oraclecloud.com"
    mon_ingest = oci.monitoring.MonitoringClient(
        config=config, signer=signer, service_endpoint=ingest_endpoint
    )

    compute = oci.core.ComputeClient(config=config, signer=signer)

    return mon_query, mon_ingest, compute

# ==============================
# Network monitoring (SAFE)
# ==============================
def oci_sum_packets_last_5m(mon_query, compartment_id, vnic_ids):
    if not mon_query or not vnic_ids:
        return 0

    try:
        end = datetime.utcnow()
        start = end - timedelta(minutes=5)
        total = 0.0

        for metric in ("VnicFromNetworkPackets", "VnicToNetworkPackets"):
            for vid in vnic_ids:
                q = f'{metric}[1m]{{resourceId = "{vid}"}}.max()'
                details = oci.monitoring.models.SummarizeMetricsDataDetails(
                    namespace="oci_vcn",
                    query=q,
                    start_time=start,
                    end_time=end,
                    resolution="1m",
                )

                resp = mon_query.summarize_metrics_data(
                    compartment_id=compartment_id,
                    summarize_metrics_data_details=details
                )

                data = None
                if hasattr(resp, "data"):
                    data = resp.data
                elif isinstance(resp, tuple) and resp and hasattr(resp[0], "data"):
                    data = resp[0].data

                if not data:
                    continue

                for series in data:
                    for dp in (series.aggregated_datapoints or []):
                        if dp.value is not None:
                            total += float(dp.value)

        return int(total)

    except Exception as e:
        print(f"[WARN] Network monitoring failed, disabling for this cycle: {e}")
        return 0

# ==============================
# NVML helpers
# ==============================
def getPowerDraw(handle):
    try:
        return float(nvmlDeviceGetPowerUsage(handle) / 1000.0)
    except Exception:
        return 0.0

def getTemp(handle):
    try:
        return int(nvmlDeviceGetTemperature(handle, NVML_TEMPERATURE_GPU))
    except Exception:
        return 0

def getUtilization(handle):
    try:
        util = nvmlDeviceGetUtilizationRates(handle)
        return util, float(util.gpu), float(util.memory)
    except Exception:
        class Dummy:
            gpu = 0
            memory = 0
        return Dummy(), 0.0, 0.0

# ==============================
# Main
# ==============================
def main():
    if not nvml_available:
        print("NVML not available, exiting.")
        return

    if check_root_crontab("halt_it.sh"):
        print("halt_it.sh presence in crontab detected, continue")
    else:
        add_to_root_crontab("*/10 * * * * bash /root/gpumon/halt_it.sh | tee -a /tmp/halt_it_log.txt")

    ident = get_instance_identity()

    INSTANCE_ID = ident["INSTANCE_ID"]
    DISPLAY_NAME = ident["DISPLAY_NAME"]
    REGION = ident["REGION"]
    COMPARTMENT_ID = ident["COMPARTMENT_ID"]
    IMAGE_ID = ident["IMAGE_ID"]
    SHAPE = ident["SHAPE"]
    HOSTNAME = ident["HOSTNAME"]
    freeform = ident["FREEFORM_TAGS"]

    # FIX: Proper unpacking
    mon_query, mon_ingest, compute_client = make_oci_clients(REGION)

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
    team_webhook = os.getenv(f"{team}_TEAM_WEBHOOK_URL", debug_webhook)

    nvmlInit()
    deviceCount = nvmlDeviceGetCount()

    vnic_ids = get_vnic_ocids()

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

                max_samples = int(CACHE_DURATION)
                core_utilization_cache[:] = [c[-max_samples:] for c in core_utilization_cache]

                avg_core = calculate_average_core_utilization()
                if any(u > THRESHOLD_PERCENTAGE_LOCAL for u in avg_core):
                    cpu_util_tripped = True
            except Exception as e:
                print(f"CPU sampling error: {e}")
                per_core = []

            total_gpu_util = 0.0
            for i in range(deviceCount):
                h = nvmlDeviceGetHandleByIndex(i)
                _, gpu_util, _ = getUtilization(h)
                total_gpu_util += gpu_util

            average_gpu_util = (total_gpu_util / deviceCount) if deviceCount else 0.0
            seconds = round(float(seconds_elapsed()))
            now = datetime.now()

            network = oci_sum_packets_last_5m(mon_query, COMPARTMENT_ID, vnic_ids)
            if network_tripped == 0 and network <= NETWORK_THRESHOLD:
                network_tripped = 1

            if seconds >= RESTART_BACKOFF:
                if round(average_gpu_util) <= GPU_THRESHOLD and not cpu_util_tripped and network <= NETWORK_THRESHOLD:
                    if alarm_pilot_light == 0:
                        alarm_pilot_light = 1
                        if team_webhook:
                            send_slack(team_webhook, f"[ {now} ] {DISPLAY_NAME} idle, pilot light ON")
                else:
                    if alarm_pilot_light == 1:
                        alarm_pilot_light = 0
                        if team_webhook:
                            send_slack(team_webhook, f"[ {now} ] {DISPLAY_NAME} active, pilot light OFF")
            else:
                alarm_pilot_light = 0

            for i in range(deviceCount):
                h = nvmlDeviceGetHandleByIndex(i)
                util, gpu_util, mem_util = getUtilization(h)
                pow_w = getPowerDraw(h)
                temp_c = getTemp(h)

                # ==============================
                # UNCHANGED LOG FORMAT (AS REQUESTED)
                # ==============================
                try:
                    with open(TMP_FILE_SAVED, 'a+') as f:
                        f.write(
                            f"[ {now} ] tag:{team},Employee:{emp_name},GPU_ID:{i},"
                            f"GPU_Util:{gpu_util},MemUtil:{mem_util},powDrawStr:{pow_w},Temp:{temp_c},"
                            f"AverageGPUUtil:{average_gpu_util},Alarm_Pilot_value:{alarm_pilot_light},"
                            f"CPU_Util_Tripped:{cpu_util_tripped},Seconds:{seconds},"
                            f"Per-Core CPU Util:{per_core},NetworkStats:{network},Network_Tripped:{network_tripped}\n"
                        )
                except Exception as e:
                    print(f"Log write error: {e}")

            sleep(sleep_interval)

    finally:
        try:
            nvmlShutdown()
        except Exception:
            pass

if __name__ == "__main__":
    main()


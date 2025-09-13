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
CACHE_DURATION = 300   # seconds to keep per-core CPU samples
THRESHOLD_PERCENTAGE = 10   # avg per-core CPU utilization threshold
sleep_interval = 10    # seconds between loops

# Custom metrics namespace in OCI Monitoring (avoid prefixes 'oci_' or 'oracle_')
# Ref: Publishing Custom Metrics docs
my_NameSpace = "GPU-metrics-with-team-tag"

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
# OCI metadata (IMDS v2)
# ==============================
IMDS = "http://169.254.169.254/opc/v2"  # requires header Authorization: Bearer Oracle
IMDS_HEADERS = {"Authorization": "Bearer Oracle"}  # per OCI IMDSv2 docs

def imds(path):
    url = f"{IMDS}{path}"
    r = requests.get(url, headers=IMDS_HEADERS, timeout=2)
    r.raise_for_status()
    try:
        return r.json()
    except Exception:
        return r.text

def get_instance_identity():
    # Single call returns a dict with id, displayName, region, compartmentId, image, shape, etc.
    # Docs: /opc/v2/instance/ with Authorization: Bearer Oracle
    data = imds("/instance/")
    # Important fields
    return {
        "INSTANCE_ID": data.get("id"),
        "DISPLAY_NAME": data.get("displayName"),
        "REGION": data.get("canonicalRegionName") or data.get("region"),  # ex: eu-frankfurt-1
        "COMPARTMENT_ID": data.get("compartmentId"),
        "IMAGE_ID": data.get("image"),
        "SHAPE": data.get("shape"),
        "HOSTNAME": data.get("hostname"),
        # Tags
        "FREEFORM_TAGS": data.get("freeformTags", {}),
        "DEFINED_TAGS": data.get("definedTags", {}),
    }

def get_vnic_ocids():
    """
    Return a list of attached VNIC OCIDs from metadata (primary + secondary).
    Docs indicate VNIC metadata under /opc/v2/vnics/.
    """
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
        payload = {"text": f"{message}"}
        r = requests.post(webhook_url, json=payload, timeout=5)
        if r.status_code != 200:
            print(f"Failed to send Slack message: HTTP {r.status_code}")
    except Exception as e:
        print(f"Slack error: {e}")

# ==============================
# OCI SDK Clients (Instance Principals)
# ==============================
def make_oci_clients(region):
    # Use instance principals for auth (no local config)
    signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
    config = {"region": region, "tenancy": signer.tenancy_id}

    # Query client (telemetry.* endpoint)
    mon_query = oci.monitoring.MonitoringClient(config=config, signer=signer)

    # Ingestion client must use telemetry-ingestion.* endpoint for post_metric_data
    # Ref: MonitoringClient docs
    ingest_endpoint = f"https://telemetry-ingestion.{region}.oraclecloud.com"
    mon_ingest = oci.monitoring.MonitoringClient(
        config=config, signer=signer, service_endpoint=ingest_endpoint
    )

    # For optional tag updates
    compute = oci.core.ComputeClient(config=config, signer=signer)

    return mon_query, mon_ingest, compute

# ==============================
# Tags (freeform)
# ==============================
def ensure_freeform_tag(compute_client, instance_id, key, default_value, current_tags):
    """
    Ensures a freeform tag exists; if missing, tries to set it on the instance.
    Requires IAM policy permitting instance principal to update instances.
    """
    if key in current_tags:
        return current_tags[key]

    try:
        new_tags = dict(current_tags)
        new_tags[key] = default_value
        details = oci.core.models.UpdateInstanceDetails(freeform_tags=new_tags)
        compute_client.update_instance(instance_id=instance_id, update_instance_details=details)
        print(f"Added freeform tag {key}={default_value}")
        return default_value
    except Exception as e:
        print(f"Could not set freeform tag {key}: {e}")
        return default_value  # fallback to default in runtime

# ==============================
# OCI Monitoring: custom metrics (ingest) + network (query)
# ==============================
def post_metrics(mon_ingest, compartment_id, namespace, dimensions, datapoints_dict):
    """
    datapoints_dict: { 'Metric Name': float_value, ... }
    All data points posted with current UTC timestamp.
    """
    now = datetime.utcnow()
    dps = []
    for name, value in datapoints_dict.items():
        md = oci.monitoring.models.MetricDataDetails(
            namespace=namespace,
            compartment_id=compartment_id,
            name=name,
            dimensions=dimensions,
            datapoints=[oci.monitoring.models.Datapoint(timestamp=now, value=float(value))],
        )
        dps.append(md)

    body = oci.monitoring.models.PostMetricDataDetails(metric_data=dps)
    try:
        mon_ingest.post_metric_data(post_metric_data_details=body)
    except Exception as e:
        print(f"post_metric_data failed: {e}")

def oci_sum_packets_last_5m(mon_query, compartment_id, vnic_ids):
    """
    Sum of (VnicFromNetworkPackets + VnicToNetworkPackets) over last 5 minutes across all VNICs.
    Uses MQL with 1m resolution and .max() (per-minute peak), then sums on the client.
    Ref: VNIC metrics (oci_vcn) and MQL reference.
    """
    if not vnic_ids:
        return 0

    end = datetime.utcnow()
    start = end - timedelta(minutes=5)

    total = 0.0
    for m in ("VnicFromNetworkPackets", "VnicToNetworkPackets"):
        for vid in vnic_ids:
            q = f'{m}[1m]{{resourceId = "{vid}"}}.max()'
            details = oci.monitoring.models.SummarizeMetricsDataDetails(
                namespace="oci_vcn",
                query=q,
                start_time=start,
                end_time=end,
                resolution="1m",
            )
            try:
                resp = mon_query.summarize_metrics_data(
                    compartment_id=compartment_id,
                    summarize_metrics_data_details=details
                )
                
                # Fix: Handle different response formats
                if hasattr(resp, 'data'):
                    # Standard response format
                    metrics_data = resp.data
                elif isinstance(resp, tuple):
                    # Handle tuple response (common issue)
                    if len(resp) > 0 and hasattr(resp[0], 'data'):
                        metrics_data = resp[0].data
                    else:
                        print(f"Unexpected tuple format for {m}/{vid}: {resp}")
                        continue
                else:
                    print(f"Unexpected response type for {m}/{vid}: {type(resp)}")
                    continue
                
                # Sum all aggregated datapoints
                if metrics_data:
                    for series in metrics_data:
                        if hasattr(series, 'aggregated_datapoints') and series.aggregated_datapoints:
                            for dp in series.aggregated_datapoints:
                                if dp.value is not None:
                                    total += float(dp.value)
                                    
            except Exception as e:
                print(f"summarize_metrics_data error for {m}/{vid}: {e}")
                # Add more detailed error info for debugging
                print(f"Error type: {type(e)}")
                if hasattr(e, 'status') and hasattr(e, 'message'):
                    print(f"Status: {e.status}, Message: {e.message}")
                
    return int(total)

# ==============================
# NVML helpers
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
    # Ensure halt cron
    if check_root_crontab("halt_it.sh"):
        print("halt_it.sh presence in crontab detected, continue")
    else:
        print("updating crontab with new halt_it.sh call")
        add_to_root_crontab("*/10 * * * * bash /root/gpumon/halt_it.sh | tee -a /tmp/halt_it_log.txt")

    # Metadata
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

    # OCI clients
    mon_query = make_oci_clients(REGION)
    #, mon_ingest, compute_client 

    # Tags we rely on (use freeform tags on the instance)
    team = freeform.get("Team", "NO_TAG")
    emp_name = freeform.get("Employee", "NO_TAG")
    policy = freeform.get("GPUMON_POLICY", "STANDARD")

    # Attempt to ensure GPUMON_POLICY exists if missing
    #if "GPUMON_POLICY" not in freeform:
    #    policy = ensure_freeform_tag(compute_client, INSTANCE_ID, "GPUMON_POLICY", "STANDARD", freeform)

    # Policy knobs
    if policy != "SEVERE":
        print(f'POLICY TAG detected: {policy}')
        RESTART_BACKOFF = 7200
        THRESHOLD_PERCENTAGE_LOCAL = 10
        GPU_THRESHOLD = 10
        NETWORK_THRESHOLD = 10000
    else:
        print(f'POLICY TAG detected: {policy}')
        RESTART_BACKOFF = 600
        THRESHOLD_PERCENTAGE_LOCAL = 40
        GPU_THRESHOLD = 10
        NETWORK_THRESHOLD = 200000

    # Slack webhooks
    debug_webhook = os.getenv("DEBUG_WEBHOOK_URL")
    team_var = f"{team}_TEAM_WEBHOOK_URL"
    team_webhook = os.getenv(team_var, debug_webhook)

    # NVML
    nvmlInit()
    deviceCount = nvmlDeviceGetCount()

    # Per-core cache globals
    global core_utilization_cache

    # VNICs for network check
    vnic_ids = get_vnic_ocids()

    TMP_FILE_SAVED = "/tmp/GPU_TEMP_" + datetime.now().strftime('%Y-%m-%dT%H')

    try:
        alarm_pilot_light = 0
        network_tripped = 0

        while True:
            # CPU core sampling and thresholding
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

            # GPU sampling
            total_gpu_util = 0.0
            last_util = None  # for logging if needed
            for i in range(deviceCount):
                h = nvmlDeviceGetHandleByIndex(i)
                util, gpu_util, mem_util = getUtilization(h)
                total_gpu_util += gpu_util
                last_util = (util, gpu_util, mem_util)

            average_gpu_util = (total_gpu_util / deviceCount) if deviceCount > 0 else 0.0
            seconds = round(float(seconds_elapsed()))
            now = datetime.now()

            # Network last 5 minutes (packets in/out across VNICs)
            network = oci_sum_packets_last_5m(mon_query, COMPARTMENT_ID, vnic_ids) or 0
            if network_tripped == 0 and network <= NETWORK_THRESHOLD:
                network_tripped = 1

            # Pilot light logic + Slack
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

            # Log + Push custom metrics per GPU
            for i in range(deviceCount):
                h = nvmlDeviceGetHandleByIndex(i)
                util, gpu_util, mem_util = getUtilization(h)
                pow_w = getPowerDraw(h)
                temp_c = getTemp(h)

                # Append to temp log
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

                # Dimensions (free-form)
                dims = {
                    "InstanceId": INSTANCE_ID,
                    "ImageId": IMAGE_ID,
                    "Shape": SHAPE,
                    "GpuNumber": str(i),
                    "InstanceTag": team,
                    "EmployeeTag": emp_name,
                }

                # Push to OCI Monitoring (custom namespace)
                #post_metrics(
                #    mon_ingest,
                #    COMPARTMENT_ID,
                #    my_NameSpace,
                #    dims,
                #    {
                #        "GPU Usage": util.gpu if hasattr(util, 'gpu') else gpu_util,
                #        "Memory Usage": util.memory if hasattr(util, 'memory') else mem_util,
                #        "Power Usage (Watts)": pow_w,
                #        "Temperature (C)": temp_c,
                #        "Alarm Pilot Light (1/0)": float(alarm_pilot_light),
                #        "Average GPU Utilization": float(average_gpu_util),
                #        "CPU Utilization Low Tripped": float(bool(cpu_util_tripped)),
                #        "Network Tripped": float(bool(network_tripped)),
                #    }
                #)

            sleep(sleep_interval)
    finally:
        nvmlShutdown()

if __name__ == "__main__":
    main()


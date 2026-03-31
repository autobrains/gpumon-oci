#!/usr/bin/env python3
# gpumon_oci.py
# Refactored for OCI — no metric upload

import os
import time
import json
import psutil
import requests
from datetime import datetime, timedelta, timezone
from time import sleep

import oci
from oci.monitoring import MonitoringClient
from oci.monitoring.models import PostMetricDataDetails, MetricDataDetails, Datapoint

# =======================
# Tunables / Constants
# =======================
CACHE_DURATION = 300            # seconds
SLEEP_INTERVAL = 10             # seconds
THRESHOLD_PERCENTAGE_DEFAULT = 10
THRESHOLD_PERCENTAGE_SEVERE = 40
NETWORK_THRESHOLD_DEFAULT = 10_000
NETWORK_THRESHOLD_SEVERE = 200_000
RESTART_BACKOFF_DEFAULT = 7200
RESTART_BACKOFF_SEVERE = 600
TMP_FILE = '/tmp/CPUMON_LOGS_'
METRICS_NAMESPACE = "gpu_metrics_with_team_tag"
METRICS_INTERVAL = 60  # post metrics every N seconds

# =======================
# Helpers: IMDSv2 (OCI)
# =======================
IMDS_ROOT = "http://169.254.169.254/opc/v2"
IMDS_HEADERS = {"Authorization": "Bearer Oracle"}

def imds_get(path: str):
    url = f"{IMDS_ROOT}/{path.lstrip('/')}"
    try:
        r = requests.get(url, headers=IMDS_HEADERS, timeout=2)
        if r.ok:
            return r.json() if r.headers.get('Content-Type','').startswith('application/json') else r.text
    except requests.RequestException:
        pass
    return None

def load_instance_identity():
    inst = imds_get("/instance/")
    if isinstance(inst, str):
        try:
            inst = json.loads(inst)
        except Exception:
            pass
    return inst or {}

# =======================
# OCI Monitoring
# =======================
def make_monitoring_client(region):
    signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
    return MonitoringClient(
        config={"region": region},
        signer=signer,
        service_endpoint=f"https://telemetry-ingestion.{region}.oraclecloud.com",
    )

def post_metrics(client, compartment_id, namespace, dimensions, metrics):
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

# =======================
# Slack
# =======================
def send_slack(webhook_url, message):
    if not webhook_url:
        return
    try:
        r = requests.post(webhook_url, json={"text": f"{message}"}, timeout=3)
        if r.status_code != 200:
            print(f"Slack webhook failed: {r.status_code}")
    except requests.RequestException as e:
        print(f"Slack error: {e}")

# =======================
# CPU sampling helpers
# =======================
core_utilization_cache = [[] for _ in range(psutil.cpu_count())]

def seconds_elapsed():
    return time.time() - psutil.boot_time()

def get_per_core_cpu_utilization():
    return psutil.cpu_percent(interval=1, percpu=True)

def calc_avg_core_utilization():
    return [sum(c)/len(c) if c else 0.0 for c in core_utilization_cache]

# =======================
# Network (OS-level packets, Option A)
# =======================
def get_network_packets_last_interval(prev_counters, interval_sec):
    """
    Returns estimated total packets/sec (recv + sent) over last interval.
    """
    now = psutil.net_io_counters()
    if not prev_counters:
        return now, 0

    delta_packets = (
        (now.packets_recv - prev_counters.packets_recv) +
        (now.packets_sent - prev_counters.packets_sent)
    )
    packets_per_sec = int(delta_packets / max(1, interval_sec))
    return now, packets_per_sec

# =======================
# Logging
# =======================
def log_results(tmp_file_saved, team, emp_name, alarm_pilot, cpu_tripped, seconds, now, per_core, network, network_tripped):
    try:
        with open(tmp_file_saved, 'a+') as f:
            f.write(f"[ {now} ] tag:{team},Employee:{emp_name},"
                    f"Alarm_Pilot_value:{alarm_pilot},CPU_Util_Tripped:{cpu_tripped},"
                    f"Seconds_Elapsed:{seconds},Per-Core_CPU_Util:{per_core},"
                    f"NetworkPackets(5m):{network},Network_Tripped:{network_tripped}\n")
    except Exception as e:
        print(f"Error writing to file: {e}")

# =======================
# Main
# =======================
def main():
    inst = load_instance_identity()
    instance_ocid = inst.get('id', 'UNKNOWN')
    hostname = inst.get('hostname', 'UNKNOWN')
    display_name = inst.get('displayName') or hostname or instance_ocid[-8:]

    freeform = inst.get('freeformTags', {}) or {}
    name_tag = freeform.get('Name', display_name)
    team = freeform.get('Team', 'NO_TAG')
    emp_name = freeform.get('Employee', 'NO_TAG')
    policy = freeform.get('GPUMON_POLICY', 'STANDARD')

    if policy != 'SEVERE':
        RESTART_BACKOFF = RESTART_BACKOFF_DEFAULT
        THRESHOLD_PERCENTAGE = THRESHOLD_PERCENTAGE_DEFAULT
        NETWORK_THRESHOLD = NETWORK_THRESHOLD_DEFAULT
    else:
        RESTART_BACKOFF = RESTART_BACKOFF_SEVERE
        THRESHOLD_PERCENTAGE = THRESHOLD_PERCENTAGE_SEVERE
        NETWORK_THRESHOLD = NETWORK_THRESHOLD_SEVERE
    print(f"POLICY TAG detected: {policy}")

    debug_webhook = os.getenv("DEBUG_WEBHOOK_URL")
    team_var = f"{str(team).upper().replace('-', '_').replace(' ', '_')}_TEAM_WEBHOOK_URL"
    team_webhook = os.getenv(team_var) or debug_webhook

    timestamp_hour = datetime.now().strftime('%Y-%m-%dT%H')
    tmp_file_saved = TMP_FILE + timestamp_hour

    monitoring_client = make_monitoring_client(inst.get('canonicalRegionName') or inst.get('region', 'eu-frankfurt-1'))
    compartment_id = inst.get('compartmentId', '')
    base_dimensions = {
        "instanceId": instance_ocid,
        "displayName": display_name,
        "team": team,
        "employee": emp_name,
    }
    last_metrics_post = 0.0

    alarm_pilot_light = 0
    network_tripped = 0
    cpu_util_tripped = False
    network_last = 99

    # NEW: previous network counters + rolling window
    prev_net_counters = None
    network_window = []

    try:
        while True:
            cpu_util_tripped = False
            try:
                per_core = get_per_core_cpu_utilization()
                for i, u in enumerate(per_core):
                    core_utilization_cache[i].append(u)
                window_samples = max(1, int(CACHE_DURATION / SLEEP_INTERVAL))
                for i in range(len(core_utilization_cache)):
                    core_utilization_cache[i] = core_utilization_cache[i][-window_samples:]
                avg_util = calc_avg_core_utilization()
                if any(u > THRESHOLD_PERCENTAGE for u in avg_util):
                    cpu_util_tripped = True
            except Exception as e:
                print(f"CPU sampling error: {e}")
                avg_util = []

            now = datetime.now(timezone.utc)
            seconds = round(float(seconds_elapsed()))

            # ===== NEW: 5-minute rolling packet window =====
            prev_net_counters, net_pps = get_network_packets_last_interval(
                prev_net_counters, SLEEP_INTERVAL
            )

            network_window.append(net_pps)
            max_samples = max(1, int(300 / SLEEP_INTERVAL))
            network_window = network_window[-max_samples:]

            network_last = sum(network_window)
            # ==============================================

            if network_tripped == 0 and network_last <= NETWORK_THRESHOLD:
                network_tripped = 1

            if seconds >= RESTART_BACKOFF:
                if not cpu_util_tripped and network_last <= NETWORK_THRESHOLD:
                    if alarm_pilot_light == 0:
                        alarm_pilot_light = 1
                        msg = (f"[{now}] INSTANCE: {name_tag} - {instance_ocid} ({hostname}) "
                               f"CPU+NET idle, TURNED ALARM PILOT LIGHT: ON")
                        send_slack(team_webhook, msg)
                else:
                    if alarm_pilot_light == 1:
                        alarm_pilot_light = 0
                        msg = (f"[{now}] INSTANCE: {name_tag} - {instance_ocid} ({hostname}) "
                               f"CPU/NET above threshold, TURNED ALARM PILOT LIGHT: OFF")
                        send_slack(team_webhook, msg)
            else:
                alarm_pilot_light = 0

            log_results(tmp_file_saved, team, emp_name, alarm_pilot_light, cpu_util_tripped,
                        seconds, now, per_core, network_last, network_tripped)

            # Post to OCI Monitoring once per METRICS_INTERVAL
            if time.time() - last_metrics_post >= METRICS_INTERVAL:
                last_metrics_post = time.time()
                ram = psutil.virtual_memory()
                disk = psutil.disk_usage('/')
                avg_cpu = sum(avg_util) / len(avg_util) if avg_util else 0.0
                post_metrics(monitoring_client, compartment_id, METRICS_NAMESPACE, base_dimensions, [
                    ("CpuUtilization",    avg_cpu),
                    ("MemoryUtilization", ram.percent),
                    ("DiskUtilization",   disk.percent),
                    ("NetworkPackets5m",  network_last),
                ])

            sleep(SLEEP_INTERVAL)
    finally:
        pass

if __name__ == '__main__':
    main()


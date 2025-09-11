#!/usr/bin/env python3
# gpumon_oci.py
# Refactored for OCI — no metric upload

import os
import time
import json
import psutil
import requests
import subprocess
from datetime import datetime, timedelta, timezone
from time import sleep

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
CRON_JOB = "*/10 * * * * /bin/bash /root/gpumon/halt_it.sh | /usr/bin/tee -a /tmp/halt_it_log.txt"

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
# Cron seeding
# =======================
def check_root_crontab(search_string):
    try:
        result = subprocess.run(['crontab', '-l'], capture_output=True, text=True, check=True)
        return search_string in result.stdout
    except subprocess.CalledProcessError:
        return False

def add_to_root_crontab(new_cron_job):
    try:
        current = subprocess.run(['crontab','-l'], capture_output=True, text=True, check=False).stdout
        new_crontab = current + ("\n" if current and not current.endswith("\n") else "") + new_cron_job + "\n"
        p = subprocess.Popen(['crontab','-'], stdin=subprocess.PIPE, text=True)
        p.communicate(input=new_crontab)
        if p.returncode == 0:
            print("New cron job added successfully.")
            return True
    except Exception as e:
        print(f"crontab add error: {e}")
    return False

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
# Logging
# =======================
def log_results(tmp_file_saved, team, emp_name, alarm_pilot, cpu_tripped, seconds, now, per_core, network, network_tripped):
    try:
        with open(tmp_file_saved, 'a+') as f:
            f.write(f"[ {now} ] tag:{team},Employee:{emp_name},"
                    f"Alarm_Pilot_value:{alarm_pilot},CPU_Util_Tripped:{cpu_tripped},"
                    f"Seconds_Elapsed:{seconds},Per-Core_CPU_Util:{per_core},"
                    f"NetworkBytes(5m):{network},Network_Tripped:{network_tripped}\n")
    except Exception as e:
        print(f"Error writing to file: {e}")

# =======================
# Main
# =======================
def main():
    if not check_root_crontab("halt_it.sh"):
        print("Updating crontab with new halt_it.sh call")
        add_to_root_crontab(CRON_JOB)

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

    alarm_pilot_light = 0
    network_tripped = 0
    cpu_util_tripped = False
    network_last = 99

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

            # placeholder: no OCI Monitoring query, use dummy value
            network_last = 50  # you can replace with actual OS-level check if needed
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

            sleep(SLEEP_INTERVAL)
    finally:
        pass

if __name__ == '__main__':
    main()

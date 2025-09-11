# (c) Paul Seifer, Autobrains LTD — OCI refactor

set -euo pipefail

TIMESTAMP_FILE="/tmp/timestamp.txt"

# --- Cooldown gate (same behavior) ---
if [ ! -f "${TIMESTAMP_FILE}" ]; then
  echo "[ $(date) ] Stop file not found, will continue the script"
else
  current_time=$(date +%s)
  _timestamp=$(cat "${TIMESTAMP_FILE}")
  timestamp=$(date -d "${_timestamp}" +%s)
  time_diff=$((current_time - timestamp))
  two_hours=$((2 * 60 * 60))

  if [ $time_diff -ge $two_hours ]; then
    echo "[ $(date) ] More than 2 hours have passed since the timestamp."
    rm -f ${TIMESTAMP_FILE} 2>/dev/null || true
  else
    remaining=$((two_hours - time_diff))
    echo "[ $(date) ] Less than 2 hours have passed. $remaining seconds remaining. will not halt the server now"
    exit 1
  fi
fi

# --- Instance metadata (OCI IMDSv2) ---
IMDS="http://169.254.169.254/opc/v2"
AUTH_HDR="Authorization: Bearer Oracle"

# Functions to fetch single IMDS values without jq dependency
imds_get() {
  local path="$1"
  curl -s --fail -H "${AUTH_HDR}" "${IMDS}/${path}"
}

# Cache file for quick re-use (optional)
if [ ! -s "/tmp/halt_it_oci.info" ]; then
  echo "[ $(date) ] Reading OCI instance metadata..."
  INSTANCE_ID="$(imds_get instance/id || true)"
  OCIREGION="$(imds_get instance/canonicalRegionName || imds_get instance/region || true)"

  # GPU detection stays as before
  if nvidia-smi --list-gpus >/dev/null 2>&1; then
    DTYPE=$(nvidia-smi --list-gpus | wc -l)
    [ "${DTYPE}" = "0" ] && DTYPE="0"
  else
    DTYPE="0"
  fi

  {
    echo "INSTANCE_ID=${INSTANCE_ID}"
    echo "DTYPE=${DTYPE}"
    echo "OCIREGION=${OCIREGION}"
  } > /tmp/halt_it_oci.info
else
  echo "[ $(date) ] Using cached metadata from /tmp/halt_it_oci.info"
  INSTANCE_ID=$(grep "^INSTANCE_ID=" /tmp/halt_it_oci.info | cut -d "=" -f2-)
  DTYPE=$(grep "^DTYPE=" /tmp/halt_it_oci.info | cut -d "=" -f2-)
  OCIREGION=$(grep "^OCIREGION=" /tmp/halt_it_oci.info | cut -d "=" -f2-)
fi

# --- Sanity checks ---
if [[ -z "${INSTANCE_ID:-}" ]] || ! echo "$INSTANCE_ID" | grep -q "^ocid1\.instance\." ; then
  echo "[ $(date) ] Need valid OCI INSTANCE_ID (ocid1.instance....), exiting"
  exit 1
fi
if [[ -z "${OCIREGION:-}" ]]; then
  echo "[ $(date) ] Need OCIREGION, exiting"
  exit 1
fi
if ! command -v oci >/dev/null 2>&1 ; then
  echo "[ $(date) ] OCI CLI not found. Please install the OCI CLI and ensure instance principals are enabled."
  exit 1
fi

# --- Log selection logic (unchanged) ---
if [[ -z "${DTYPE}" ]] || [[ "${DTYPE}" == "0" ]]; then
  SEP=6
  FILE="CPUMON_LOGS_"
  STEP=500
else
  SEP=12
  FILE="GPU_TEMP_"
  if [ "${DTYPE}" -lt "4" ]; then
    STEP=500   # single GPU → single line; 500 lines ≈ 2 hours
  else
    STEP=2000  # 4 GPUs → 4 lines
  fi
fi

# --- No AWS CLI checks; we operate with OCI CLI only ---

NOGO="TRUE"
REASON="NO_REASON"
wall_message="""
++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
This instance: ${INSTANCE_ID} seems to have been idle for the last
2 hours, will shut it down in 3 minutes from now. If you have just logged in,
please wait a couple of minutes and start it again from the console, or run:

sudo bash /root/gpumon/kill_halt.sh

to cancel shutdown now. The shutdown pause in this case will last 2 hours,
after which the shutdown sequence will resume.
++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
"""

# --- Locate latest log file (unchanged) ---
filename=$(ls -lit /tmp/${FILE}* 2>/dev/null | head -1 | awk '{print $NF}')
if [ -z "${filename}" ]; then
  NOGO="TRUE"
  REASON="LOG_NOT_FOUND:${filename}"
else
  NOGO="FALSE"
  if [ "$(wc -l < "${filename}")" -lt "${STEP}" ]; then
    NOGO="TRUE"
    REASON="LOG_EXISTS_BUT_NOT_ENOUGH_DATA_IN_IT_SO_FAR:($(wc -l < "${filename}"))_LINES"
  fi
fi

# --- Analyze content (unchanged logic) ---
if [ "${NOGO}" == "FALSE" ]; then
  check=$(tail -${STEP} "${filename}" | cut -d ":" -f${SEP} | sort | uniq -c | grep "CPU_Util" || true)
  if [ -z "${check}" ]; then
    NOGO="TRUE"
    REASON="LOG_FILE_EXISTS_BUT_NO_VALID_DATA"
  else
    result=$(tail -${STEP} "${filename}" | cut -d ":" -f${SEP} | sort | uniq -c | grep "0,CPU_Util_Tripped" || true)
    if [ -z "${result}" ]; then
      positive=$(tail -${STEP} "${filename}" | cut -d ":" -f${SEP} | sort | uniq -c | grep "1,CPU_Util_Tripped" || true)
      if [ -z "${positive}" ]; then
        NOGO="TRUE"
        REASON="ALARM_WASNT_ON_DURING_LAST_2_HOURS,INCONCLUSIVE"
      else
        NOGO="FALSE"
        REASON="WE_GOT_PILOT_ONLY_FOR_2_HOURS_ITS_A_GO:${positive}"
      fi
    else
      NOGO="TRUE"
      REASON="DATA_IS_OK_BUT_GOT_ACTIVITY_SPIKE:${check}"
    fi
  fi
fi

# --- Act ---
if [ "${NOGO}" == "TRUE" ]; then
  echo "[ $(date) ] WE ARE A NO-GO, BECAUSE:${REASON} — TILL LATER, TA TA"
else
  echo "[ $(date) ] LOOKS LIKE WE ARE A GO - WILL SHUT THE INSTANCE DOWN, REASON:${REASON}" | tee -a /root/gpumon_persistent.log
  wall "[ $(date) ] ${wall_message}"
  sleep 180
  wall "[ $(date) ] Well, 3 minutes have passed, shutdown is now... Bye Bye"

  # Stop via OCI CLI, using instance principals
  # Requires IAM policy for the instance's dynamic group:
  #   allow dynamic-group <DG_NAME> to use instance-family in compartment <COMPARTMENT_NAME>
  res=$(oci compute instance action \
    --instance-id "${INSTANCE_ID}" \
    --action STOP \
    --region "${OCIREGION}" \
    --auth instance_principal 2>&1) || true

  echo "[ $(date) ] debug: got result for oci compute instance action STOP: ${res}"
fi

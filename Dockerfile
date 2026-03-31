FROM nvidia/cuda:12.2.0-base-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    cron \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt

# Install OCI CLI via pip — lands at /usr/local/bin/oci
RUN pip3 install --no-cache-dir oci-cli

WORKDIR /app
COPY gpumon.py cpumon.py halt_it.sh docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh halt_it.sh

ENTRYPOINT ["/app/docker-entrypoint.sh"]

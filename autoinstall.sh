#!/bin/bash

function install_all(){

sudo apt update -y && sudo apt install curl -y && sudo apt install -y python3-pip
sudo pip install psutil && sudo pip install pynvml
sudo apt install -y python3-pip python3-venv python3-setuptools python3-wheel
sudo curl -L https://raw.githubusercontent.com/oracle/oci-cli/master/scripts/install/install.sh -o install.sh
sudo sed -i install.sh -e "s|v3.2.1|v2.22.0|g"
echo "Install script patched"
sudo bash install.sh --accept-all-defaults
sudo python3 -m pip install oci_cli --break-system-packages
(crontab -l 2>/dev/null; echo '*/10 * * * * bash /root/gpumon/halt_it.sh | tee -a /tmp/halt_it_log.txt') | crontab -
sudo git clone https://github.com/autobrains/gpumon-oci.git /root/gpumon
sudo rm -f /etc/systemd/system/gpumon.service
sudo touch /etc/systemd/system/gpumon.service
sudo chmod 664 /etc/systemd/system/gpumon.service
sudo tee /etc/systemd/system/gpumon.service > /dev/null <<EOT
[Unit]
Description=GPU Monitoring Service
After=network.target
Wants=network.target
[Service]
User=root
Group=root
Type=simple
Restart=on-failure
ExecStartPre=git -C /root/gpumon pull
ExecStart=sudo bash -c 'sudo /usr/bin/nvidia-smi >/dev/null;err=\$?;echo err:\$err;if [ \$err -eq 0 ]; then sudo python3 /root/gpumon/gpumon.py; else sudo python3 /root/gpumon/cpumon.py; fi'
[Install]
WantedBy=multi-user.target
EOT
sudo systemctl daemon-reload
sudo systemctl start gpumon
sudo systemctl enable gpumon
sudo systemctl restart gpumon
sudo systemctl status gpumon
echo "$(date)" >> /var/log/gpumon.finished
}
install_date=$(cat /var/log/gpumon.finished || true)
if [ "${install_date}" != "" ]; then
   echo "The install has been activated already at least once on:${install_date}, skipping"
else
  echo "Will run install..."
  install_all
fi

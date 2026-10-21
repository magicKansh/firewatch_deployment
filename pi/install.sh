#!/usr/bin/env bash
# Install or update the Firewatch node on a Raspberry Pi (Pi 5 / Raspberry Pi OS Bookworm or later).
# Usage, from the repository folder:   sudo ./pi/install.sh
# Re-run it after `git pull` to update the installed copy.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then echo "Run with sudo: sudo $0"; exit 1; fi
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP=/opt/firewatch
CONF=/etc/firewatch

echo "==> System packages"
apt-get update -q
apt-get install -y -q python3-venv python3-numpy python3-pil python3-lgpio i2c-tools
# Only needed for an optional Pi Camera; harmless without one.
apt-get install -y -q python3-picamera2 ffmpeg || echo "   (Pi Camera packages unavailable; thermal-only is fine)"

echo "==> I2C at 1 MHz (the MLX90640 needs a fast bus for 4-8 Hz frames)"
raspi-config nonint do_i2c 0 || true
BOOTCFG=/boot/firmware/config.txt; [[ -f $BOOTCFG ]] || BOOTCFG=/boot/config.txt
REBOOT=0
if ! grep -q '^dtparam=i2c_arm_baudrate=1000000' "$BOOTCFG"; then
  sed -i '/^dtparam=i2c_arm_baudrate=/d' "$BOOTCFG"
  echo 'dtparam=i2c_arm_baudrate=1000000' >> "$BOOTCFG"
  REBOOT=1
fi

echo "==> Service user"
id firewatch &>/dev/null || useradd --system --home-dir "$APP" --shell /usr/sbin/nologin firewatch
for g in i2c video gpio; do getent group "$g" >/dev/null && usermod -aG "$g" firewatch; done

echo "==> Application in $APP"
install -d -o firewatch -g firewatch "$APP"
install -m 644 "$REPO"/sensor_monitor.py "$REPO"/thermal.py "$REPO"/node_storage.py "$REPO"/requirements-pi.txt "$APP"/
[[ -d $APP/.venv ]] || python3 -m venv --system-site-packages "$APP/.venv"
"$APP/.venv/bin/pip" install -q -r "$APP/requirements-pi.txt"
chown -R firewatch:firewatch "$APP"

echo "==> Configuration in $CONF"
install -d -m 750 -g firewatch "$CONF"
if [[ ! -f $CONF/camera_config.json ]]; then
  install -m 640 -g firewatch "$REPO/camera_config.json.example" "$CONF/camera_config.json"
  NEEDS_CONFIG=1
fi
[[ -f $CONF/node.env ]] || install -m 640 -g firewatch "$REPO/pi/node.env.example" "$CONF/node.env"

echo "==> systemd service"
install -m 644 "$REPO/pi/firewatch-node.service" /etc/systemd/system/firewatch-node.service
systemctl daemon-reload
systemctl enable firewatch-node >/dev/null

echo
if [[ ${NEEDS_CONFIG:-0} == 1 ]] || grep -q CHANGE_THIS "$CONF/camera_config.json" \
   || grep -q your-service "$CONF/node.env"; then
  echo "Next: put this camera's ID, key and coordinates in $CONF/camera_config.json"
  echo "      and your server address in $CONF/node.env, then run:"
  [[ $REBOOT == 1 ]] && echo "      sudo reboot" || echo "      sudo systemctl restart firewatch-node"
elif [[ $REBOOT == 1 ]]; then
  echo "Reboot to apply the I2C settings:  sudo reboot"
  echo "After rebooting, check the sensor: i2cdetect -y 1   (should show 33)"
else
  systemctl restart firewatch-node
  echo "Service restarted. Follow the logs with:  journalctl -u firewatch-node -f"
fi

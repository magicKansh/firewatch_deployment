#!/bin/sh
# Install the Firewatch node as a systemd service: starts at boot, restarts if it crashes.
# Run from the repo folder on the Pi, as your normal user (it asks for sudo):
#   ./install-pi-service.sh
set -e

DIR=$(cd "$(dirname "$0")" && pwd)
RUN_AS=$(id -un)

[ -x "$DIR/.venv/bin/python" ] || { echo "Missing $DIR/.venv - follow the Raspberry Pi setup in README_DISTRIBUTED.md first."; exit 1; }
[ -f "$DIR/camera_config.json" ] || { echo "Missing $DIR/camera_config.json - copy camera_config.json.example and add your camera key."; exit 1; }
grep -q '^FIREWATCH_SERVER_URL=' "$DIR/.env" 2>/dev/null || { echo "Add FIREWATCH_SERVER_URL=https://your-service.onrender.com to $DIR/.env first."; exit 1; }

sed -e "s|__USER__|$RUN_AS|g" -e "s|__DIR__|$DIR|g" "$DIR/firewatch-node.service" \
    | sudo tee /etc/systemd/system/firewatch-node.service > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now firewatch-node
sudo systemctl restart firewatch-node  # pick up changes when re-run

echo "Firewatch node installed and running as $RUN_AS."
echo "  Status: systemctl status firewatch-node"
echo "  Logs:   journalctl -u firewatch-node -f"

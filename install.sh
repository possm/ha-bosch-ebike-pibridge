#!/usr/bin/env bash
# Bosch eBike BLE Bridge – one-shot installer for Raspberry Pi OS (Bookworm/Bullseye).
# Run as root: sudo bash install.sh
set -euo pipefail

INSTALL_DIR=/opt/bosch-ebike-bridge
CONFIG_DIR=/etc/bosch-ebike-bridge
SERVICE_NAME=bosch-ebike-bridge
DASHBOARD_SERVICE=bosch-ebike-dashboard

echo "=== Bosch eBike BLE Bridge – installer ==="

# ── 1. System packages ────────────────────────────────────────────────────────
echo "[1/4] Installing system packages..."
apt-get update -q
apt-get install -y \
    python3 \
    python3-pip \
    python3-dbus \
    python3-gi \
    python3-yaml \
    bluetooth \
    bluez

# ── 2. Python packages ────────────────────────────────────────────────────────
echo "[2/4] Installing Python packages..."
# Prefer apt over pip to avoid PEP 668 issues on Bookworm
apt-get install -y python3-paho-mqtt python3-flask 2>/dev/null \
    || pip3 install --break-system-packages paho-mqtt flask 2>/dev/null \
    || pip3 install paho-mqtt flask

# ── 3. Copy application files ─────────────────────────────────────────────────
echo "[3/4] Installing application files to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR" "$CONFIG_DIR"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cp "$SCRIPT_DIR/bridge.py"    "$INSTALL_DIR/"
cp "$SCRIPT_DIR/livedata.py"  "$INSTALL_DIR/"
cp "$SCRIPT_DIR/ha_mqtt.py"   "$INSTALL_DIR/"
cp "$SCRIPT_DIR/dashboard.py" "$INSTALL_DIR/"
# Logo shown in the dashboard header (served via /logo.png).
cp "$SCRIPT_DIR/docs/BOSCH-EBIKE-SYSTEMS.png" "$INSTALL_DIR/" 2>/dev/null || true

if [ ! -f "$CONFIG_DIR/config.yaml" ]; then
    cp "$SCRIPT_DIR/config.yaml" "$CONFIG_DIR/config.yaml"
    echo ""
    echo "  *** Default config copied to $CONFIG_DIR/config.yaml ***"
    echo "  *** Edit it to set your MQTT broker IP and bike names ***"
    echo ""
fi

# ── 4. Systemd services ───────────────────────────────────────────────────────
echo "[4/4] Installing and starting systemd services..."
cp "$SCRIPT_DIR/$SERVICE_NAME.service"    /etc/systemd/system/
cp "$SCRIPT_DIR/$DASHBOARD_SERVICE.service" /etc/systemd/system/
systemctl daemon-reload
# enable: start on every boot  --now: also start immediately
systemctl enable --now "$SERVICE_NAME"
systemctl enable --now "$DASHBOARD_SERVICE"

echo ""
echo "=== Installation complete ==="
echo ""
echo "Both services are running and will start automatically on every reboot."
echo ""
echo "  BLE bridge:  sudo systemctl status $SERVICE_NAME"
echo "  Dashboard:   http://e-bike-bridge.local:8080"
echo ""
echo "Next steps:"
echo "  1. Edit $CONFIG_DIR/config.yaml"
echo "     - Set mqtt.broker to your Home Assistant / Mosquitto IP"
echo "     - Set mqtt.username / mqtt.password if required"
echo "     Then apply:  sudo systemctl restart $SERVICE_NAME $DASHBOARD_SERVICE"
echo "  2. Pair your eBike via the Bosch Flow App:"
echo "     ⚙ gear icon → Components → Add new device"
echo "     The bike should find 'HA eBike Bridge' within ~30 seconds."
echo "  3. Find the bike's BLE address (after pairing):"
echo "       sudo bluetoothctl -- devices"
echo "     Add it to config.yaml under 'bikes:' to give it a friendly name."
echo "  4. Check logs:"
echo "       sudo journalctl -u $SERVICE_NAME -f"
echo "       sudo journalctl -u $DASHBOARD_SERVICE -f"

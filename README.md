# ha-bosch-pibridge

Raspberry Pi BLE → MQTT bridge for the **Bosch eBike Live Data Interface (LDI)**.

Replaces the ESP32 bridge from [ha-bosch-ebike](https://github.com/Xunil99/ha-bosch-ebike) with a Python service that runs directly on a Raspberry Pi 4 (or any Pi with Bluetooth 4.x/5.0). One Pi handles **two bikes simultaneously**.

---

## How it works

```
eBike (BLE central)
  └─connects to──▶ Raspberry Pi (BLE peripheral, advertising SolicitUUID)
                       └─GATT client──▶ reads eb21 notifications
                           └─MQTT──▶ Home Assistant (auto-discovery)
```

1. The Pi advertises a **Service Solicitation UUID** matching the Bosch LDI GATT service
2. The eBike scans, finds the advertisement, connects and bonds (Just-Works LE Secure Connections)
3. The Pi subscribes to GATT notifications on the Live Data characteristic (`eb21`)
4. Each notification is decoded from protobuf and published as JSON to MQTT
5. Home Assistant auto-discovers all 13 entities via MQTT discovery

---

## Requirements

| | |
|---|---|
| **Hardware** | Raspberry Pi 3B+ / 4 / 5 / Zero 2W (built-in BT required) |
| **OS** | Raspberry Pi OS Lite 64-bit (Bookworm) |
| **eBike** | Bosch smart system, control unit firmware **≥ v19** |
| **HA** | Home Assistant with Mosquitto MQTT broker add-on |

---

## Installation

```bash
git clone https://github.com/possm/ha-bosch-ebike-pibridge.git
cd ha-bosch-ebike-pibridge
sudo bash install.sh
```

The script installs all system dependencies (`python3-dbus`, `python3-gi`, `python3-yaml`, `bluetooth`, `bluez`, `paho-mqtt`) and registers the systemd service.

---

## Configuration

Edit `/etc/bosch-ebike-bridge/config.yaml`:

```yaml
bridge_name: "HA eBike Bridge"

mqtt:
  broker: 192.168.x.x     # your HA / Mosquitto IP
  port: 1883
  username: mqtt-user
  password: yourpassword
  base_topic: bosch_ebike

# Optional: give bikes a friendly name by BLE address.
# Find the address after first pairing: sudo bluetoothctl -- devices
bikes:
  - address: "AA:BB:CC:DD:EE:FF"
    name: "My eBike"
  - address: "AA:BB:CC:DD:EE:FF"
    name: "Partner's eBike"
```

Then restart the service:
```bash
sudo systemctl restart bosch-ebike-bridge
```

---

## Pairing a new bike

The eBike only scans for accessories when explicitly triggered via the **Bosch Flow App**:

1. Power on the eBike, make sure the Pi bridge is running
2. Open the **Bosch Flow App** → your bike → **⚙ gear icon** (top-right)
3. Tap **Components** → **Add new device**
4. The bike enters scan mode — it should find **"HA eBike Bridge"** within ~30 seconds
5. Confirm pairing on the bike's display (no PIN needed)

After pairing the bike reconnects automatically whenever it powers on within range.

---

## Entities in Home Assistant

All entities appear automatically under **Settings → Devices & Services → MQTT**:

| Entity | Unit |
|---|---|
| Speed | km/h |
| Cadence | rpm |
| Rider Power | W |
| Ambient Brightness | lx |
| Battery SoC | % |
| Odometer | km |
| Connected | — |
| Light | — |
| System Locked | — |
| Charger Connected | — |
| Light Reserve | — |
| Diagnosis Active | — |
| In Motion | — |

---

## Service management

```bash
# Status and live logs
sudo systemctl status bosch-ebike-bridge
sudo journalctl -u bosch-ebike-bridge -f

# Restart
sudo systemctl restart bosch-ebike-bridge
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `MQTT connect failed (rc=5)` | Wrong credentials | Check `config.yaml` and verify the user exists in Mosquitto |
| `org.bluez.Error.Failed` on start | Bluetooth soft-blocked | `sudo rfkill unblock bluetooth` (the service does this automatically on next restart) |
| Bike not found in Flow App scan | Bridge not advertising | Check `Active Instances: 1` via `sudo bluetoothctl show` |
| `LDI characteristic not found` | eBike firmware < v19 | Update via the Bosch Flow App |
| Entities show `unknown` after pairing | Bike disconnected before initial read | Power-cycle the bike |

---

## Credits

Protocol details and protobuf decoder ported from
[ha-bosch-ebike](https://github.com/Xunil99/ha-bosch-ebike) by Xunil99.

## License

Apache-2.0

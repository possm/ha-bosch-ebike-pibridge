<p align="center">
  <img src="docs/BOSCH-EBIKE-SYSTEMS.png" alt="Bosch eBike Systems" width="420">
</p>

# ha-bosch-ebike-pibridge

Raspberry Pi BLE → Home Assistant / MQTT bridge for the **Bosch eBike Live Data Interface (LDI)**.

Runs directly on a Raspberry Pi. One Pi handles **two bikes simultaneously** using its built-in Bluetooth 5.0. Includes a live web dashboard viewable in any browser.

<table>
  <tr>
    <td align="center" width="50%">
      <img src="docs/dashboard.png" alt="eBike web dashboard" width="320"><br>
      <sub><b>Built-in web dashboard</b> — live speed, battery, cadence, power and status, updating in real time in any browser.</sub>
    </td>
    <td align="center" width="50%">
      <img src="docs/ha-sensors.png" alt="Home Assistant sensors" width="320"><br>
      <sub><b>Home Assistant entities</b> — all 13 sensors auto-discovered via MQTT, ready for dashboards and automations.</sub>
    </td>
  </tr>
</table>

---

## How it works

```
eBike (BLE central)
  └─connects to──▶ Raspberry Pi (BLE peripheral, advertising SolicitUUID)
                       └─GATT client──▶ reads eb21 notifications
                           └─MQTT──▶ Home Assistant (auto-discovery)
                           └─HTTP──▶ Web dashboard (port 8080)
```

1. The Pi advertises a **Service Solicitation UUID** matching the Bosch LDI GATT service
2. The eBike scans, finds the advertisement, connects and bonds (Just-Works LE Secure Connections)
3. The Pi subscribes to GATT notifications on the Live Data characteristic (`eb21`)
4. Each notification is decoded from protobuf and published as JSON to MQTT
5. Home Assistant auto-discovers all 13 entities via MQTT discovery
6. The web dashboard reads the same MQTT feed and displays live data in the browser

> Protocol details based on [ha-bosch-ebike](https://github.com/Xunil99/ha-bosch-ebike) by Xunil99.

---

## Requirements

| | |
|---|---|
| **Hardware** | Raspberry Pi 3B+ / 4 / 5 / Zero 2W (built-in Bluetooth required) |
| **OS** | Raspberry Pi OS Lite 64-bit (Bookworm) |
| **eBike** | Bosch smart system, control unit firmware **≥ v19** |
| **Home Assistant** | Mosquitto MQTT broker add-on |

---

## Installation

```bash
git clone https://github.com/possm/ha-bosch-ebike-pibridge.git
cd ha-bosch-ebike-pibridge
sudo bash install.sh
```

The script installs all dependencies (`python3-dbus`, `python3-gi`, `python3-yaml`, `python3-flask`, `bluetooth`, `bluez`, `paho-mqtt`), copies files to `/opt/bosch-ebike-bridge/`, and enables both systemd services so they start on every boot.

---

## Configuration

Edit `/etc/bosch-ebike-bridge/config.yaml`:

```yaml
bridge_name: "HA eBike Bridge"

mqtt:
  broker: 192.168.x.x       # your HA / Mosquitto IP
  port: 1883
  username: mqtt-user
  password: yourpassword
  base_topic: bosch_ebike

# Optional: change the dashboard port (default 8080)
# dashboard_port: 8080

# Optional: give bikes a friendly name by BLE address.
# Find the address after first pairing:
#   sudo bluetoothctl -- devices
bikes:
  - address: "AA:BB:CC:DD:EE:FF"
    name: "My eBike"
  - address: "AA:BB:CC:DD:EE:GG"
    name: "Partner's eBike"
```

Apply changes:
```bash
sudo systemctl restart bosch-ebike-bridge bosch-ebike-dashboard
```

---

## Pairing a new bike

The eBike only scans for accessories when explicitly triggered via the **Bosch Flow App**. It does not connect automatically on first use.

**Open a pairing window first.** For privacy, the bridge is only discoverable to Flow apps during a 5 minute pairing window. That window opens automatically for 5 minutes after the bridge boots, and you can reopen it any time by pressing the **Start pairing** button on the Bosch eBike Bridge device in Home Assistant. The **Pairing active** sensor shows whether the window is currently open. Outside the window the bridge is invisible to other people's Flow apps, but your already bonded bikes still reconnect automatically. See [Privacy](#privacy).

1. Press **Start pairing** in Home Assistant (or reboot the bridge) to open the window
2. Power on the eBike and make sure the bridge is running
3. Open the **Bosch Flow App**, your bike, **⚙ gear icon** (top right)
4. Tap **Components**, then **Add new device**
5. The bike enters scan mode. It should find the bridge within about 30 seconds
6. Confirm pairing on the bike's display (no PIN needed)

> **Note on the bridge name during pairing:** in the bike's accessory list the
> bridge may appear as a raw Bluetooth address (`A4:0D:BC:…`) rather than
> "HA eBike Bridge". This is a BlueZ limitation — the device name is sent in the
> BLE scan response, which the bike's pairing scan does not read. Just select
> the bridge anyway. It's cosmetic: after bonding, each bike reconnects
> automatically and shows its correct name in Home Assistant and the dashboard.

To pair a **second bike**, repeat the same steps with the other bike. The bridge
keeps advertising while a slot is free and one Pi handles both bikes at once.

After pairing, a bike reconnects automatically every time it powers on within
range of the Pi.

---

## Privacy

By default the bridge only broadcasts the Bosch LDI solicitation (the signal a Flow app looks for to add a new accessory) during a **5 minute pairing window**.

Outside the window the bridge advertises privately: name only, not discoverable, and without the solicitation. This means:

- Other people nearby with the Flow app cannot see or try to add your bridge.
- Your already bonded bikes still reconnect automatically, because a bonded bike reconnects by its stored bond, not by the solicitation. Verified on hardware.

**Opening the pairing window.** Any of these opens it for 5 minutes:

- The bridge boots (the window opens automatically on every startup).
- The **Start pairing** button on the web dashboard.
- The **Start pairing** button in Home Assistant (see [Entities in Home Assistant](#entities-in-home-assistant)).

While the window is open, the web dashboard button highlights and the **Pairing active** sensor in Home Assistant is on.

To keep the old always discoverable behaviour, set `private_advertising: false` in `config.yaml`.

---

## Web dashboard

Open **http://e-bike-bridge.local:8080** in any browser (phone, tablet, laptop).

- Live speed, cadence, power, battery %, odometer. Updates instantly via Server-Sent Events, no page reload.
- Battery bar with colour coding (green, orange, red).
- Status chips: light, in motion, charging, locked, light reserve, diagnosis.
- When a bike is **offline**, the card dims and shows last known values with a "Last seen X ago" label that counts up every 10 seconds.
- Both bikes shown side by side when both are connected.
- A **Start pairing** button (under the bridge status) opens the 5 minute pairing window so you can add a bike from the Flow app. See [Privacy](#privacy).

---

## Entities in Home Assistant

All 15 entities appear automatically under **Settings → Devices & Services → MQTT**:

| Entity | Type | Unit |
|---|---|---|
| Speed | Sensor | km/h |
| Cadence | Sensor | rpm |
| Rider Power | Sensor | W |
| Ambient Brightness | Sensor | lx |
| Battery SoC | Sensor | % |
| Odometer | Sensor | km |
| Charge Time to 80% | Sensor | min |
| Charge Time to 100% | Sensor | min |
| Connected | Binary sensor | — |
| Light | Binary sensor | — |
| System Locked | Binary sensor | — |
| Charging | Binary sensor | — |
| Light Reserve | Binary sensor | — |
| Diagnosis Active | Binary sensor | — |
| In Motion | Binary sensor | — |

**When the bike is off or out of range**, all entities keep their last known values — speed, battery %, odometer, etc. remain visible in HA dashboards and automations. The **Connected** binary sensor is the explicit online/offline indicator. State values are published as retained MQTT messages so they survive HA and Pi reboots.

The **Charge Time to 80% / 100%** sensors are estimates derived from the rising
SoC while charging (rate measured live, with a slowdown factor for the CV phase
above 85%). They read *unknown* until a reliable charging rate has built up, and
whenever the bike isn't charging. The 80% figure is the more accurate of the two
(the whole region up to 80% charges at a roughly constant rate).

### The Bosch eBike Bridge device

A separate **Bosch eBike Bridge** device (manufacturer "Raspberry Pi", grouped apart from the bikes) exposes three controls, all auto discovered over MQTT:

| Entity | Type | What it does |
|---|---|---|
| `binary_sensor.bridge` | Binary sensor | **On** while the Pi bridge is running. Flips to **off** automatically via an MQTT Last Will message if the Pi loses power, crashes, or drops off the network. Use it to alert when the bridge itself goes down. |
| `button.start_pairing` | Button | Opens the 5 minute pairing window so a Flow app can add a bike. Same effect as the dashboard button. See [Privacy](#privacy). |
| `binary_sensor.pairing_active` | Binary sensor | **On** while the pairing window is open. |

**Where to find it.** Go to **Settings, Devices and services, MQTT** and open the **Bosch eBike Bridge** device. The three entities are listed there.

**Putting the button on a dashboard.** Edit a Lovelace dashboard, **Add card**, choose a **Button** card, and set the entity to `button.start_pairing`. Tapping the card presses the button. Add `binary_sensor.pairing_active` to an Entities card if you want to see when the window is open.

Nothing needs to be written in YAML. The entities appear on their own, and the bridge republishes discovery on every MQTT connect, so restarting the bridge brings them back if they ever go missing.

### Note on the `Charging` sensor (smart-charging automations)

The **Charging** sensor (entity `binary_sensor.bosch_ebike_<bike>_charger_connected`)
reflects the Bosch LDI's raw charger field, which tracks an **active charging
session** (power flowing) rather than mere cable presence — hence the
`battery_charging` device class showing *Charging* / *Not charging* (confirmed on
a Bosch smart system bike). So if an automation cuts power via a smart plug, this
sensor flips to **off** even though the cable is still physically plugged in —
keep that in mind when building charging automations.

The bridge only sees the bike over Bluetooth — it has **no knowledge of smart-plug
state**. So any "is power flowing?" vs "is the cable in?" logic, and an 80%
charge-limit (cut the plug when `Battery SoC` ≥ 80%), must live in Home Assistant,
not the bridge.

---

## Service management

```bash
# Status
sudo systemctl status bosch-ebike-bridge
sudo systemctl status bosch-ebike-dashboard

# Live logs
sudo journalctl -u bosch-ebike-bridge -f
sudo journalctl -u bosch-ebike-dashboard -f

# Restart both
sudo systemctl restart bosch-ebike-bridge bosch-ebike-dashboard
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `MQTT connect failed (rc=5)` | Wrong credentials | Check `config.yaml`; verify the user exists in the Mosquitto add-on config |
| `org.bluez.Error.Failed` on start | Bluetooth soft-blocked by rfkill | `sudo rfkill unblock bluetooth` — the service does this automatically on the next restart |
| Bike not found in Flow App scan | Bridge not advertising | Check `ActiveInstances: 1` via `sudo bluetoothctl show` |
| Bridge shows as a MAC address, not its name, during pairing | BlueZ sends the name in the scan response, which the bike's passive scan ignores | Cosmetic — select it anyway; correct names appear in HA after bonding |
| Second bike won't connect while first is connected | Older version, or advertising stopped after the first connect | Update to the latest version (re-advertises after each connection) |
| `LDI characteristic not found` | eBike firmware < v19 | Update firmware via the Bosch Flow App |
| Entities show `unknown` after pairing | Bike disconnected before initial GATT read | Power-cycle the bike |
| Entities show `unavailable` after updating | HA still has old discovery config with availability topic | Delete the MQTT device in HA and let it re-discover on next bike connection |

---

## Smart charging

The bridge's `Battery SoC` and `Charging` sensors, combined with a
smart plug controlling the charger, let Home Assistant do smart charging —
either a simple 80% limit, or price-based scheduling via
[EV Smart Charging](https://github.com/jonasbkarlsson/ev_smart_charging)
(Tibber, Nord Pool, etc.).

> **Note:** the `Charging` sensor reflects an **active charging session** (power
> flowing), not mere cable presence — so it goes **off** when a smart plug cuts
> power even with the cable still plugged in. The template sensor below accounts
> for this.

**You need:** this bridge in HA · a smart plug for the charger · a price
integration · EV Smart Charging (HACS).

### Find your entity IDs

Entities are named `bosch_ebike_<bike>_<field>`. Look them up under
**Settings → Devices & Services → MQTT → your bike**. You'll need:
- `binary_sensor.bosch_ebike_<bike>_charger_connected` — note: a **binary_sensor**, state `on`/`off`
- `sensor.bosch_ebike_<bike>_battery_soc`

### 1. Target SOC helper

```yaml
input_number:
  ebike_target_soc:
    name: "eBike target SOC"
    min: 20
    max: 100
    step: 5
    unit_of_measurement: "%"
    icon: mdi:battery-charging
```

### 2. "eBike connected" template sensor

EV Smart Charging needs a "vehicle connected" signal. But when it pauses by
switching the plug **off**, the `Charging` sensor also goes **off** — which would
look like the bike was unplugged. This template keeps "connected" true whenever
the plug is off (paused) or the charger is actively reporting:

```yaml
template:
  - binary_sensor:
      - name: "eBike connected"
        device_class: plug
        state: >
          {% set plug = states('switch.YOUR_SMART_PLUG') %}
          {% set chg  = states('binary_sensor.bosch_ebike_YOURBIKE_charger_connected') %}
          {% if plug in ['unavailable', 'unknown'] or chg in ['unavailable', 'unknown'] %}
            true
          {% elif plug == 'off' %}
            true
          {% elif chg == 'on' %}
            true
          {% else %}
            false
          {% endif %}
```

Replace `switch.YOUR_SMART_PLUG` and the `..._charger_connected` entity with
your real IDs.

| Plug | Charging | "eBike connected" |
|------|----------|-------------------|
| off  | any   | **on** — paused, can still schedule |
| on   | on    | **on** — actively charging |
| on   | off   | **off** — bike not plugged in |
| unavailable | any | **on** — safe default during restarts |

### Configure EV Smart Charging

Settings → Devices & Services → **EV Smart Charging** → Add entry:

| Field | Entity |
|-------|--------|
| Price sensor | your Tibber / Nord Pool sensor |
| SOC sensor | `sensor.bosch_ebike_YOURBIKE_battery_soc` |
| Charger switch | `switch.YOUR_SMART_PLUG` |
| EV connected | `binary_sensor.ebike_connected` (template above) |
| Target SOC | `input_number.ebike_target_soc` |

---

## Credits

Inspired by [ha-bosch-ebike](https://github.com/Xunil99/ha-bosch-ebike) by [Xunil99](https://github.com/Xunil99) — great work on reverse-engineering the Bosch LDI protocol. I didn't have a spare ESP32 lying around, so I took the same idea and built a Python version that runs straight on a Raspberry Pi instead.

## License

Apache-2.0

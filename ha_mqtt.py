"""
Home Assistant MQTT discovery + state publishing for the Bosch eBike bridge.

Each connected bike is represented as a separate HA device.  Discovery topics
are published once per bike (retained) so entities appear automatically.
State updates are published as a single JSON payload; each sensor uses a
value_template to extract its field.
"""
from __future__ import annotations
import json
import logging

import paho.mqtt.client as mqtt

log = logging.getLogger(__name__)

# ── Sensor definitions ────────────────────────────────────────────────────────
# (state_key, friendly_name, unit, device_class, state_class, icon)
SENSOR_DEFS: list[tuple] = [
    ("speed_kmh",    "Speed",              "km/h", "speed",        "measurement",      None),
    ("cadence",      "Cadence",            "rpm",  None,           "measurement",      "mdi:bike"),
    ("rider_power",  "Rider Power",        "W",    "power",        "measurement",      None),
    ("ambient_lux",  "Ambient Brightness", "lx",   "illuminance",  "measurement",      None),
    ("battery_soc",  "Battery SoC",        "%",    "battery",      "measurement",      None),
    ("odometer_km",  "Odometer",           "km",   "distance",     "total_increasing", None),
]

# (state_key, friendly_name, device_class, icon)
BINARY_SENSOR_DEFS: list[tuple] = [
    ("connected",          "Connected",         "connectivity", None),
    ("bike_light",         "Light",             "light",        None),
    ("system_locked",      "System Locked",     "lock",         None),
    ("charger_connected",  "Charger Connected", "plug",         None),
    ("light_reserve",      "Light Reserve",     "battery",      None),
    ("diagnosis_active",   "Diagnosis Active",  "problem",      None),
    ("in_motion",          "In Motion",         "moving",       None),
]


def _slug(bike_id: str) -> str:
    """Convert a BLE address like AB:CD:EF:01:23:45 to ab_cd_ef_01_23_45."""
    return bike_id.replace(":", "_").lower()


class HaMqttPublisher:
    """Thread-safe MQTT publisher with HA auto-discovery support."""

    def __init__(
        self,
        broker: str,
        port: int = 1883,
        username: str | None = None,
        password: str | None = None,
        base_topic: str = "bosch_ebike",
    ) -> None:
        self._base = base_topic.rstrip("/")
        self._discovered: set[str] = set()
        # Topic the broker watches for the bridge's own online/offline state.
        self._bridge_avail_topic = f"{self._base}/bridge/availability"
        self._bridge_discovered = False

        self._client = mqtt.Client(
            client_id="bosch-ebike-bridge",
            clean_session=False,
            protocol=mqtt.MQTTv311,
        )
        if username:
            self._client.username_pw_set(username, password)

        # Last Will & Testament: if the bridge dies (power loss, crash, network
        # drop) the broker publishes "offline" on our behalf, so Home Assistant
        # sees the Bridge connectivity sensor flip to "off" automatically.
        self._client.will_set(
            self._bridge_avail_topic, payload="offline", qos=1, retain=True
        )

        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.connect_async(broker, port, keepalive=60)
        self._client.loop_start()

    # ── Internal callbacks ────────────────────────────────────────────────────

    def _on_connect(self, client, userdata, flags, rc: int) -> None:
        if rc == 0:
            log.info("MQTT connected")
            # Re-publish discovery for all known bikes after reconnect
            self._discovered.clear()
            self._bridge_discovered = False
            # Announce the bridge itself as online (counterpart to the LWT).
            self.publish_bridge_discovery()
            self._client.publish(
                self._bridge_avail_topic, "online", qos=1, retain=True
            )
        else:
            log.error("MQTT connect failed (rc=%d)", rc)

    def _on_disconnect(self, client, userdata, rc: int) -> None:
        if rc != 0:
            log.warning("MQTT disconnected unexpectedly (rc=%d), will retry", rc)

    # ── Public API ────────────────────────────────────────────────────────────

    def publish_bridge_discovery(self) -> None:
        """Publish HA discovery for the bridge's own connectivity sensor.

        Backed by the LWT availability topic: ON while the bridge is running,
        OFF (via the broker's will message) if it loses power, crashes, or drops
        off the network. Lives under its own HA device so it groups separately
        from the bikes.
        """
        if self._bridge_discovered:
            return
        self._bridge_discovered = True

        unique_id = "bosch_ebike_bridge_online"
        payload = {
            "name": "Bridge",
            "unique_id": unique_id,
            # The availability topic IS the state: 'online' → ON, 'offline' → OFF.
            "state_topic": self._bridge_avail_topic,
            "payload_on": "online",
            "payload_off": "offline",
            "device_class": "connectivity",
            "device": {
                "identifiers": ["bosch_ebike_pi_bridge"],
                "name": "Bosch eBike Bridge",
                "manufacturer": "Raspberry Pi",
                "model": "BLE → MQTT bridge",
            },
        }
        self._client.publish(
            f"homeassistant/binary_sensor/{unique_id}/config",
            json.dumps(payload),
            retain=True,
        )
        log.info("Published HA discovery for bridge connectivity sensor")

    def publish_discovery(self, bike_id: str, bike_name: str) -> None:
        """Publish HA MQTT discovery config for all entities of one bike.

        Idempotent – only publishes once per bike_id per session.
        """
        if bike_id in self._discovered:
            return
        self._discovered.add(bike_id)

        slug = _slug(bike_id)
        state_topic = f"{self._base}/{slug}/state"
        avail_topic = f"{self._base}/{slug}/availability"
        device = {
            "identifiers": [f"bosch_ebike_{slug}"],
            "name": bike_name,
            "manufacturer": "Bosch",
            "model": "eBike (LDI)",
            "via_device": "bosch_ebike_pi_bridge",
        }

        for key, name, unit, dev_class, state_class, icon in SENSOR_DEFS:
            unique_id = f"bosch_ebike_{slug}_{key}"
            payload: dict = {
                "name": name,
                "unique_id": unique_id,
                "state_topic": state_topic,
                "value_template": "{{ value_json." + key + " | default('') }}",
                "unit_of_measurement": unit,
                # No availability_topic: sensors keep their last known value
                # when the bike is off. The 'connected' binary sensor is the
                # explicit online/offline indicator.
                "device": device,
            }
            if dev_class:
                payload["device_class"] = dev_class
            if state_class:
                payload["state_class"] = state_class
            if icon:
                payload["icon"] = icon
            self._client.publish(
                f"homeassistant/sensor/{unique_id}/config",
                json.dumps(payload),
                retain=True,
            )

        for key, name, dev_class, icon in BINARY_SENSOR_DEFS:
            unique_id = f"bosch_ebike_{slug}_{key}"

            if key == "connected":
                # The 'connected' sensor reflects the bike's BLE connection,
                # which is tracked via the availability topic (online/offline) —
                # NOT a field in the state JSON (the decoder never emits a
                # 'connected' key). Drive the sensor straight from that topic.
                payload = {
                    "name": name,
                    "unique_id": unique_id,
                    "state_topic": avail_topic,
                    "payload_on": "online",
                    "payload_off": "offline",
                    "device": device,
                }
            else:
                payload = {
                    "name": name,
                    "unique_id": unique_id,
                    "state_topic": state_topic,
                    "value_template": (
                        "{{ 'ON' if value_json." + key + " else 'OFF' }}"
                    ),
                    # No availability_topic: these keep their last known value
                    # when the bike is off.
                    "device": device,
                }
            if dev_class:
                payload["device_class"] = dev_class
            if icon:
                payload["icon"] = icon
            self._client.publish(
                f"homeassistant/binary_sensor/{unique_id}/config",
                json.dumps(payload),
                retain=True,
            )

        log.info("Published HA discovery for '%s' (%s)", bike_name, bike_id)

    def publish_state(self, bike_id: str, state: dict) -> None:
        """Publish current sensor state for one bike (retained so HA keeps
        last known values across restarts and when the bike is offline)."""
        slug = _slug(bike_id)
        self._client.publish(
            f"{self._base}/{slug}/state",
            json.dumps(state),
            retain=True,
        )

    def publish_availability(self, bike_id: str, online: bool) -> None:
        """Publish online/offline availability for one bike (retained)."""
        slug = _slug(bike_id)
        self._client.publish(
            f"{self._base}/{slug}/availability",
            "online" if online else "offline",
            retain=True,
        )

    def stop(self) -> None:
        # Mark the bridge offline on a clean shutdown (systemctl stop / restart)
        # so HA reflects it immediately rather than waiting for the LWT timeout.
        try:
            self._client.publish(
                self._bridge_avail_topic, "offline", qos=1, retain=True
            ).wait_for_publish(timeout=2)
        except Exception:
            pass
        self._client.loop_stop()
        self._client.disconnect()

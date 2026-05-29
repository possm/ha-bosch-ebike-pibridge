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

        self._client = mqtt.Client(
            client_id="bosch-ebike-bridge",
            clean_session=False,
            protocol=mqtt.MQTTv311,
        )
        if username:
            self._client.username_pw_set(username, password)

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
        else:
            log.error("MQTT connect failed (rc=%d)", rc)

    def _on_disconnect(self, client, userdata, rc: int) -> None:
        if rc != 0:
            log.warning("MQTT disconnected unexpectedly (rc=%d), will retry", rc)

    # ── Public API ────────────────────────────────────────────────────────────

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
                "availability_topic": avail_topic,
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
            payload = {
                "name": name,
                "unique_id": unique_id,
                "state_topic": state_topic,
                "value_template": (
                    "{{ 'ON' if value_json." + key + " else 'OFF' }}"
                ),
                "availability_topic": avail_topic,
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
        """Publish current sensor state for one bike."""
        slug = _slug(bike_id)
        self._client.publish(
            f"{self._base}/{slug}/state",
            json.dumps(state),
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
        self._client.loop_stop()
        self._client.disconnect()

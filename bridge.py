#!/usr/bin/env python3
"""
Bosch eBike Live Data Interface – Raspberry Pi BLE → MQTT bridge.

Architecture
------------
The Bosch eBike acts as a BLE *central*: it scans for peripherals that
advertise a Service Solicitation UUID matching its own LDI GATT service.
When it finds one it connects, pairs (Just-Works LE Secure Connections),
and the bridge then acts as a GATT *client* on that connection to subscribe
to notifications on the Live Data characteristic (eb21).

On the Pi:
  1. BlueZ LE advertising with SolicitUUIDs = LDI service UUID  →  eBike connects
  2. JustWorksAgent accepts pairing automatically
  3. When ServicesResolved fires, find eb21 and call StartNotify()
  4. PropertiesChanged(Value) delivers each notification payload
  5. Decode protobuf → publish JSON to MQTT → HA auto-discovery

Both bikes can be active simultaneously; BlueZ handles two concurrent
connections on the Pi 4's CYW43455 BT 5.0 controller.
"""
from __future__ import annotations

import logging
import signal
import sys
import yaml

import dbus
import dbus.service
import dbus.mainloop.glib
from gi.repository import GLib

from livedata import decode_live_data
from ha_mqtt import HaMqttPublisher

log = logging.getLogger(__name__)

# ── BlueZ D-Bus interface names ───────────────────────────────────────────────
BLUEZ_SERVICE    = "org.bluez"
ADAPTER_IFACE    = "org.bluez.Adapter1"
DEVICE_IFACE     = "org.bluez.Device1"
GATT_CHR_IFACE   = "org.bluez.GattCharacteristic1"
LE_ADV_MGR_IFACE = "org.bluez.LEAdvertisingManager1"
LE_ADV_IFACE     = "org.bluez.LEAdvertisement1"
AGENT_MGR_IFACE  = "org.bluez.AgentManager1"
AGENT_IFACE      = "org.bluez.Agent1"
OM_IFACE         = "org.freedesktop.DBus.ObjectManager"
PROPS_IFACE      = "org.freedesktop.DBus.Properties"

# Bosch LDI GATT UUIDs (spec V1.0: 0000xxxx-eaa2-11e9-81b4-2a2ae2dbcce4)
LDI_SERVICE_UUID = "0000eb20-eaa2-11e9-81b4-2a2ae2dbcce4"
LDI_CHAR_UUID    = "0000eb21-eaa2-11e9-81b4-2a2ae2dbcce4"

AGENT_PATH       = "/org/bosch_ebike_bridge/agent"
ADV_PATH         = "/org/bosch_ebike_bridge/advertisement0"


# ── Pairing agent ─────────────────────────────────────────────────────────────

class JustWorksAgent(dbus.service.Object):
    """Auto-accept BLE Just-Works (NoInputNoOutput) pairing requests."""

    def __init__(self, bus: dbus.SystemBus) -> None:
        dbus.service.Object.__init__(self, bus, AGENT_PATH)

    @dbus.service.method(AGENT_IFACE, in_signature="", out_signature="")
    def Release(self) -> None:
        pass

    @dbus.service.method(AGENT_IFACE, in_signature="os", out_signature="")
    def AuthorizeService(self, device: str, uuid: str) -> None:
        log.info("AuthorizeService %s uuid=%s – accepted", device, uuid)

    @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="")
    def RequestAuthorization(self, device: str) -> None:
        log.info("RequestAuthorization %s – accepted", device)

    @dbus.service.method(AGENT_IFACE, in_signature="ou", out_signature="")
    def DisplayPasskey(self, device: str, passkey: int) -> None:
        log.info("DisplayPasskey %s passkey=%06d", device, passkey)

    @dbus.service.method(AGENT_IFACE, in_signature="ou", out_signature="")
    def RequestConfirmation(self, device: str, passkey: int) -> None:
        # Just-Works: confirm without checking
        log.info("RequestConfirmation %s passkey=%06d – confirmed", device, passkey)

    @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="s")
    def RequestPinCode(self, device: str) -> str:
        return "0000"

    @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="u")
    def RequestPasskey(self, device: str) -> dbus.UInt32:
        return dbus.UInt32(0)

    @dbus.service.method(AGENT_IFACE, in_signature="", out_signature="")
    def Cancel(self) -> None:
        pass


# ── LE Advertisement ──────────────────────────────────────────────────────────

class LDIAdvertisement(dbus.service.Object):
    """
    LE peripheral advertisement with Service Solicitation UUID.

    BlueZ's SolicitUUIDs property maps directly to AD type 0x15 (128-bit
    Service Solicitation), which is exactly what the eBike scans for.
    Appearance 0x0480 = Cycling Generic.
    """

    def __init__(self, bus: dbus.SystemBus, local_name: str) -> None:
        dbus.service.Object.__init__(self, bus, ADV_PATH)
        self._local_name = local_name

    def _props(self) -> dict:
        return {
            "Type":          dbus.String("peripheral"),
            "SolicitUUIDs":  dbus.Array([LDI_SERVICE_UUID], signature="s"),
            "LocalName":     dbus.String(self._local_name),
            "Appearance":    dbus.UInt16(0x0480),
            "Discoverable":  dbus.Boolean(True),
        }

    @dbus.service.method(PROPS_IFACE, in_signature="ss", out_signature="v")
    def Get(self, interface: str, prop: str):
        return self._props()[prop]

    @dbus.service.method(PROPS_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface: str) -> dict:
        return self._props()

    @dbus.service.method(LE_ADV_IFACE, in_signature="", out_signature="")
    def Release(self) -> None:
        log.info("Advertisement released by BlueZ")


# ── Main bridge ───────────────────────────────────────────────────────────────

class EbikeBridge:
    def __init__(self, config: dict) -> None:
        self._cfg = config
        self._bus = dbus.SystemBus()
        self._loop = GLib.MainLoop()

        # Per-bike state: addr (upper) → {name, state, char_path}
        self._bikes: dict[str, dict] = {}
        for bike in config.get("bikes", []):
            addr = bike["address"].upper()
            self._bikes[addr] = {
                "name":      bike.get("name", f"eBike {addr[-5:].replace(':', '')}"),
                "state":     {},
                "char_path": None,
            }

        mqtt_cfg = config["mqtt"]
        self._mqtt = HaMqttPublisher(
            broker=mqtt_cfg["broker"],
            port=int(mqtt_cfg.get("port", 1883)),
            username=mqtt_cfg.get("username"),
            password=mqtt_cfg.get("password"),
            base_topic=mqtt_cfg.get("base_topic", "bosch_ebike"),
        )

    # ── BlueZ helpers ─────────────────────────────────────────────────────────

    def _get_adapter_path(self) -> str:
        om = dbus.Interface(self._bus.get_object(BLUEZ_SERVICE, "/"), OM_IFACE)
        for path, ifaces in om.GetManagedObjects().items():
            if ADAPTER_IFACE in ifaces:
                return str(path)
        raise RuntimeError("No Bluetooth adapter found. Is bluetoothd running?")

    def _setup_adapter(self, adapter_path: str) -> None:
        adapter = self._bus.get_object(BLUEZ_SERVICE, adapter_path)
        props = dbus.Interface(adapter, PROPS_IFACE)
        props.Set(ADAPTER_IFACE, "Powered",       dbus.Boolean(True))
        props.Set(ADAPTER_IFACE, "Discoverable",  dbus.Boolean(True))
        props.Set(ADAPTER_IFACE, "Pairable",       dbus.Boolean(True))
        log.info("Adapter %s: powered, discoverable, pairable", adapter_path)

    def _register_agent(self) -> JustWorksAgent:
        agent = JustWorksAgent(self._bus)
        mgr = dbus.Interface(
            self._bus.get_object(BLUEZ_SERVICE, "/org/bluez"), AGENT_MGR_IFACE
        )
        mgr.RegisterAgent(AGENT_PATH, "NoInputNoOutput")
        mgr.RequestDefaultAgent(AGENT_PATH)
        log.info("Just-Works pairing agent registered")
        return agent

    def _register_advertisement(self, adapter_path: str) -> LDIAdvertisement:
        local_name = self._cfg.get("bridge_name", "HA eBike Bridge")
        adv = LDIAdvertisement(self._bus, local_name)
        mgr = dbus.Interface(
            self._bus.get_object(BLUEZ_SERVICE, adapter_path), LE_ADV_MGR_IFACE
        )
        mgr.RegisterAdvertisement(
            ADV_PATH, {},
            reply_handler=lambda: log.info(
                "Advertising as '%s' with SolicitUUID=%s", local_name, LDI_SERVICE_UUID
            ),
            error_handler=lambda e: log.error("RegisterAdvertisement failed: %s", e),
        )
        return adv

    def _find_ldi_char(self, device_path: str) -> str | None:
        """Return the D-Bus path of the LDI Live Data characteristic, or None."""
        om = dbus.Interface(self._bus.get_object(BLUEZ_SERVICE, "/"), OM_IFACE)
        for path, ifaces in om.GetManagedObjects().items():
            if GATT_CHR_IFACE not in ifaces:
                continue
            if not str(path).startswith(device_path):
                continue
            uuid = str(ifaces[GATT_CHR_IFACE].get("UUID", "")).lower()
            if uuid == LDI_CHAR_UUID:
                return str(path)
        return None

    # ── Notification handling ─────────────────────────────────────────────────

    def _subscribe(self, char_path: str, bike_addr: str) -> None:
        """Enable GATT notifications on the LDI characteristic."""
        try:
            char_obj = self._bus.get_object(BLUEZ_SERVICE, char_path)

            # Connect PropertiesChanged signal *before* StartNotify so we
            # don't miss the first notification.
            self._bus.add_signal_receiver(
                handler_function=lambda iface, changed, inv, path=char_path:
                    self._on_char_changed(path, bike_addr, iface, changed),
                signal_name="PropertiesChanged",
                dbus_interface=PROPS_IFACE,
                path=char_path,
                path_keyword=None,
            )

            dbus.Interface(char_obj, GATT_CHR_IFACE).StartNotify(
                reply_handler=lambda: log.info(
                    "Notifications enabled for %s", bike_addr
                ),
                error_handler=lambda e: log.error(
                    "StartNotify failed for %s: %s", bike_addr, e
                ),
            )

            # Initial read for a full snapshot (notifications only carry changed
            # fields; without a read we'd wait for the first change per field).
            try:
                raw = bytes(
                    dbus.Interface(char_obj, GATT_CHR_IFACE).ReadValue({})
                )
                self._handle_payload(bike_addr, raw, source="initial-read")
            except dbus.DBusException as exc:
                log.warning(
                    "Initial read failed for %s (will rely on notifications): %s",
                    bike_addr, exc,
                )

        except dbus.DBusException as exc:
            log.error("Failed to subscribe for %s: %s", bike_addr, exc)

    def _on_char_changed(
        self, char_path: str, bike_addr: str, iface: str, changed: dict
    ) -> None:
        if iface != GATT_CHR_IFACE or "Value" not in changed:
            return
        raw = bytes(changed["Value"])
        self._handle_payload(bike_addr, raw, source="notify")

    def _handle_payload(self, bike_addr: str, raw: bytes, source: str) -> None:
        log.debug("[%s] %s: %d bytes", bike_addr, source, len(raw))
        try:
            data = decode_live_data(raw)
        except Exception as exc:
            log.error("Decode error for %s: %s", bike_addr, exc)
            return

        if not data:
            return

        # Auto-register unknown bikes (addr not in config)
        if bike_addr not in self._bikes:
            name = f"eBike {bike_addr[-5:].replace(':', '')}"
            self._bikes[bike_addr] = {"name": name, "state": {}, "char_path": None}

        bike = self._bikes[bike_addr]
        bike["state"].update(data)   # merge sparse notification into running state

        self._mqtt.publish_discovery(bike_addr, bike["name"])
        self._mqtt.publish_state(bike_addr, bike["state"])
        log.debug("[%s] state update: %s", bike["name"], data)

    # ── Device lifecycle ──────────────────────────────────────────────────────

    def _on_services_resolved(self, device_path: str, addr: str) -> None:
        """Called once BlueZ has fully resolved the eBike's GATT services."""
        bike_addr = addr.upper()
        log.info("Services resolved for %s – looking for LDI characteristic", bike_addr)

        char_path = self._find_ldi_char(device_path)
        if not char_path:
            log.error(
                "LDI characteristic %s not found on %s. "
                "Is this a Bosch eBike with firmware ≥ v19?",
                LDI_CHAR_UUID, bike_addr,
            )
            return

        log.info("LDI characteristic found at %s", char_path)

        if bike_addr not in self._bikes:
            name = f"eBike {bike_addr[-5:].replace(':', '')}"
            self._bikes[bike_addr] = {"name": name, "state": {}, "char_path": char_path}
        else:
            self._bikes[bike_addr]["char_path"] = char_path

        self._mqtt.publish_availability(bike_addr, True)
        self._subscribe(char_path, bike_addr)

    def _on_disconnected(self, addr: str) -> None:
        bike_addr = addr.upper()
        log.info("eBike disconnected: %s", bike_addr)
        if bike_addr in self._bikes:
            self._bikes[bike_addr]["state"] = {}
            self._bikes[bike_addr]["char_path"] = None
        self._mqtt.publish_availability(bike_addr, False)

    # ── D-Bus signal monitoring ───────────────────────────────────────────────

    def _monitor(self) -> None:
        """
        Watch for:
          • InterfacesAdded   – device appears (first connect)
          • PropertiesChanged – Connected / ServicesResolved changes on Device1
        """

        def on_interfaces_added(path: str, interfaces: dict) -> None:
            if DEVICE_IFACE not in interfaces:
                return
            props = interfaces[DEVICE_IFACE]
            addr = str(props.get("Address", ""))
            # ServicesResolved may already be True if this is a fast re-bond
            if bool(props.get("ServicesResolved", False)):
                self._on_services_resolved(str(path), addr)

        def on_properties_changed(
            iface: str, changed: dict, invalidated: list, sender_path: str = ""
        ) -> None:
            if iface != DEVICE_IFACE:
                return

            try:
                dev_obj   = self._bus.get_object(BLUEZ_SERVICE, sender_path)
                props_ifc = dbus.Interface(dev_obj, PROPS_IFACE)
                addr      = str(props_ifc.Get(DEVICE_IFACE, "Address"))
            except dbus.DBusException:
                return

            if "ServicesResolved" in changed and bool(changed["ServicesResolved"]):
                self._on_services_resolved(sender_path, addr)

            if "Connected" in changed and not bool(changed["Connected"]):
                self._on_disconnected(addr)

        self._bus.add_signal_receiver(
            on_interfaces_added,
            signal_name="InterfacesAdded",
            dbus_interface=OM_IFACE,
        )
        self._bus.add_signal_receiver(
            on_properties_changed,
            signal_name="PropertiesChanged",
            dbus_interface=PROPS_IFACE,
            path_keyword="sender_path",
        )

        # Handle any bikes that are already connected at startup
        om = dbus.Interface(self._bus.get_object(BLUEZ_SERVICE, "/"), OM_IFACE)
        for path, ifaces in om.GetManagedObjects().items():
            if DEVICE_IFACE not in ifaces:
                continue
            props = ifaces[DEVICE_IFACE]
            addr  = str(props.get("Address", ""))
            if bool(props.get("ServicesResolved", False)):
                log.info("Found already-connected bike at startup: %s", addr)
                self._on_services_resolved(str(path), addr)

    # ── Entry point ───────────────────────────────────────────────────────────

    def run(self) -> None:
        adapter_path = self._get_adapter_path()
        self._setup_adapter(adapter_path)
        self._register_agent()
        self._register_advertisement(adapter_path)
        self._monitor()

        def _shutdown(signum, frame) -> None:
            log.info("Caught signal %d – shutting down", signum)
            for addr in self._bikes:
                self._mqtt.publish_availability(addr, False)
            self._mqtt.stop()
            self._loop.quit()

        signal.signal(signal.SIGINT,  _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)

        log.info(
            "Bridge running – advertising as '%s', waiting for eBike connections",
            self._cfg.get("bridge_name", "HA eBike Bridge"),
        )
        self._loop.run()


# ── CLI entry point ───────────────────────────────────────────────────────────

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    config_path = "/etc/bosch-ebike-bridge/config.yaml"
    if len(sys.argv) > 1:
        config_path = sys.argv[1]

    try:
        with open(config_path) as fh:
            config = yaml.safe_load(fh)
    except FileNotFoundError:
        log.error("Config not found: %s", config_path)
        sys.exit(1)

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    EbikeBridge(config).run()


if __name__ == "__main__":
    main()

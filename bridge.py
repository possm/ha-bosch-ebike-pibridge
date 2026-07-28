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
import subprocess
import sys
import time
import yaml

import dbus
import dbus.service
import dbus.mainloop.glib
from gi.repository import GLib

from livedata import decode_live_data, ChargeEstimator
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

# How long the discoverable pairing window stays open (seconds) after boot or
# when reopened via the HA button. Outside the window the bridge advertises
# privately (no LDI solicitation) so strangers' Flow apps can't see it.
PAIRING_WINDOW_SEC = 5 * 60

# After this many consecutive disconnects that happen before services resolved
# (the stale-bond signature), the bond for that address is dropped so the bike
# can re-pair cleanly. See _on_disconnected() / _remove_bond().
STALE_BOND_THRESHOLD = 3


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

    Name placement note: BlueZ's high-level LEAdvertisement1 D-Bus API always
    puts the LocalName in the SCAN RESPONSE, not the primary advertising PDU —
    and offers no way to control this (verified with btmon). A scanner only
    receives the name if it does an ACTIVE scan (sends a scan request). Phones
    and the Bosch Flow app do active scans, so they show the bridge name fine.
    The eBike's own accessory-discovery scan appears to be passive, so during
    that one-time pairing step it may show only the MAC address. This is
    cosmetic: once bonded, the bike reconnects automatically and the correct
    per-bike names appear in Home Assistant and the dashboard. Forcing the name
    into the primary PDU would require bypassing BlueZ with raw HCI commands,
    which is fragile and conflicts with bluetoothd — deliberately not done.
    """

    def __init__(self, bus: dbus.SystemBus, local_name: str,
                 pairing: bool = True) -> None:
        dbus.service.Object.__init__(self, bus, ADV_PATH)
        self._local_name = local_name
        # pairing=True  → discoverable + LDI solicitation, so a Flow app can add
        #                 the bridge (used during the 5-min pairing window).
        # pairing=False → private reconnect: name only, NO solicitation, not
        #                 discoverable, so other people's Flow apps don't see us.
        #                 A bonded bike still reconnects by bond/address.
        self._pairing = pairing

    def _props(self) -> dict:
        props = {
            "Type":         dbus.String("peripheral"),
            "LocalName":    dbus.String(self._local_name),
            "Discoverable": dbus.Boolean(bool(self._pairing)),
        }
        if self._pairing:
            # Only the pairing window carries the solicitation UUID + Cycling
            # appearance — that's exactly what makes a Flow app recognise us as
            # a pairable Bosch accessory.
            props["SolicitUUIDs"] = dbus.Array([LDI_SERVICE_UUID], signature="s")
            props["Appearance"] = dbus.UInt16(0x0480)
        return props

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

        # BLE advertisement handle + adapter path (set in run()).
        self._adv = None
        self._adapter_path = None

        # Pairing window: monotonic deadline (seconds) until which the bridge
        # advertises discoverable WITH the LDI solicitation, so a Flow app can
        # add it. Outside the window it advertises privately (no solicitation).
        # Opened on boot and by the HA "Start pairing" button. 0 = never opened.
        self._pairing_until = 0.0
        # Whether the pairing window may be opened at all. When a config sets
        # privacy off, the bridge always advertises with the solicitation (old
        # behaviour) so nothing changes for users who want it.
        self._privacy = bool(config.get("private_advertising", True))

        # Consecutive failed-pairing counter per address. A "failed pairing" is
        # a disconnect that happened before services ever resolved — the classic
        # symptom of a stale bond after a reboot (SMP "Authentication Failed").
        # After STALE_BOND_THRESHOLD such failures we drop the bond so the bike
        # can re-pair automatically. Reset to 0 on every successful resolve.
        self._connect_fails: dict[str, int] = {}

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
        # On a cold boot the controller (hci0) may not be enumerated on D-Bus
        # yet when the service starts. Poll briefly instead of failing outright
        # so the bridge survives a reboot on slower boards (Pi 3) as well as
        # faster ones (Pi 4).
        for attempt in range(1, 16):
            om = dbus.Interface(self._bus.get_object(BLUEZ_SERVICE, "/"), OM_IFACE)
            for path, ifaces in om.GetManagedObjects().items():
                if ADAPTER_IFACE in ifaces:
                    return str(path)
            log.warning(
                "No Bluetooth adapter yet (attempt %d/15) — waiting for hci0",
                attempt,
            )
            time.sleep(2)
        raise RuntimeError("No Bluetooth adapter found. Is bluetoothd running?")

    @staticmethod
    def _rfkill_unblock_bluetooth() -> None:
        """Best-effort `rfkill unblock bluetooth`, model/path agnostic.

        On a fresh boot systemd-rfkill may restore the adapter to a SOFT-BLOCKED
        state, so powering it on via BlueZ fails with org.bluez.Error.Failed.
        The systemd unit already calls rfkill via ExecStartPre, but on slower
        boards (e.g. Pi 3) the unblock can land before the controller is fully
        up. Calling it again here, with the binary looked up across the usual
        locations, closes that race on both Pi 3 and Pi 4.
        """
        for binary in ("rfkill", "/usr/sbin/rfkill", "/sbin/rfkill"):
            try:
                subprocess.run(
                    [binary, "unblock", "bluetooth"],
                    check=False,
                    capture_output=True,
                    timeout=5,
                )
                return
            except (FileNotFoundError, subprocess.SubprocessError):
                continue
        log.warning("rfkill not found in PATH or sbin — could not unblock BT")

    def _setup_adapter(self, adapter_path: str) -> None:
        adapter = self._bus.get_object(BLUEZ_SERVICE, adapter_path)
        props = dbus.Interface(adapter, PROPS_IFACE)

        # Powering on can fail right after boot if the adapter is still
        # rfkill-soft-blocked or bluetoothd hasn't finished bringing hci0 up.
        # Retry with an rfkill unblock between attempts instead of crashing —
        # this self-heals across reboots on any board (Pi 3 / Pi 4 / etc.).
        last_exc: Exception | None = None
        for attempt in range(1, 11):
            try:
                props.Set(ADAPTER_IFACE, "Powered", dbus.Boolean(True))
                last_exc = None
                break
            except dbus.DBusException as exc:
                last_exc = exc
                log.warning(
                    "Adapter power-on attempt %d/10 failed (%s) — "
                    "unblocking rfkill and retrying",
                    attempt, exc.get_dbus_name(),
                )
                self._rfkill_unblock_bluetooth()
                time.sleep(2)
        if last_exc is not None:
            raise last_exc

        # These are non-fatal niceties — log but don't crash if they fail.
        for prop in ("Discoverable", "Pairable"):
            try:
                props.Set(ADAPTER_IFACE, prop, dbus.Boolean(True))
            except dbus.DBusException as exc:
                log.warning("Could not set adapter %s: %s", prop, exc)

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

    def _pairing_open(self) -> bool:
        """True while the discoverable pairing window is open.

        When privacy is disabled this is always True, so the bridge always
        advertises with the solicitation (the pre-privacy behaviour).
        """
        if not self._privacy:
            return True
        return self._pairing_until > time.monotonic()

    def _register_advertisement(self, adapter_path: str) -> LDIAdvertisement:
        local_name = self._cfg.get("bridge_name", "HA eBike Bridge")
        pairing = self._pairing_open()
        adv = LDIAdvertisement(self._bus, local_name, pairing=pairing)
        mgr = dbus.Interface(
            self._bus.get_object(BLUEZ_SERVICE, adapter_path), LE_ADV_MGR_IFACE
        )
        mode = ("PAIRING: discoverable + solicitation" if pairing
                else "private reconnect: no solicitation, not discoverable")
        mgr.RegisterAdvertisement(
            ADV_PATH, {},
            reply_handler=lambda: log.info("Advertising '%s' (%s)", local_name, mode),
            error_handler=lambda e: log.error("RegisterAdvertisement failed: %s", e),
        )
        return adv

    def _start_pairing_idle(self) -> bool:
        """GLib idle wrapper so an MQTT-thread button press runs on the loop."""
        self.start_pairing()
        return False  # run once

    def start_pairing(self) -> None:
        """Open the discoverable pairing window (HA button / boot)."""
        self._pairing_until = time.monotonic() + PAIRING_WINDOW_SEC
        log.info("Pairing window open for %d min — bridge is discoverable",
                 PAIRING_WINDOW_SEC // 60)
        self._publish_pairing_state()
        self._readvertise()
        # Re-advertise privately again once the window elapses, so we don't stay
        # discoverable forever. GLib timeout fires on the main loop thread.
        GLib.timeout_add_seconds(PAIRING_WINDOW_SEC + 1, self._pairing_window_elapsed)

    def _pairing_window_elapsed(self) -> bool:
        if not self._pairing_open():
            log.info("Pairing window closed — switching to private advertising")
            self._publish_pairing_state()
            self._readvertise()
        return False  # one-shot timer

    def _publish_pairing_state(self) -> None:
        if self._mqtt is not None:
            self._mqtt.publish_pairing_active(self._pairing_open())

    def _all_known_bikes_connected(self) -> bool:
        """True if every bike listed in config is currently connected.

        A bike counts as connected once its char_path is set. If no bikes are
        configured (auto-discover mode) this is always False, so the bridge
        keeps advertising to pick up any bike.
        """
        configured = [b for b in self._cfg.get("bikes", [])]
        if not configured:
            return False
        for b in configured:
            addr = b["address"].upper()
            state = self._bikes.get(addr)
            if not state or not state.get("char_path"):
                return False
        return True

    def _unregister_advertisement(self) -> None:
        """Tear down the current advert object, ignoring if already gone."""
        try:
            mgr = dbus.Interface(
                self._bus.get_object(BLUEZ_SERVICE, self._adapter_path),
                LE_ADV_MGR_IFACE,
            )
            try:
                mgr.UnregisterAdvertisement(ADV_PATH)
            except dbus.DBusException:
                pass
            try:
                if self._adv is not None:
                    self._adv.remove_from_connection()
            except Exception:
                pass
            self._adv = None
        except dbus.DBusException as exc:
            log.warning("Unregister advertisement failed: %s", exc)

    def _readvertise(self) -> None:
        """Keep the named LE advertisement in the right state.

        BlueZ stops (releases) a connectable advertisement as soon as a peer
        connects. With only one advert object the bridge then falls back to the
        adapter's generic discoverable mode, which exposes the raw MAC but NOT
        our bridge name or the LDI Service Solicitation UUID — so a SECOND eBike
        only sees a nameless MAC.

        Policy:
          • If every configured bike is connected, stop advertising entirely —
            there is no free slot, so broadcasting just wastes airtime and
            invites unknown devices.
          • Otherwise, (re-)register the named, solicitation-carrying advert so
            the next bike can still discover the bridge by name.
        """
        if self._all_known_bikes_connected():
            log.info("All configured bikes connected — stopping advertising")
            self._unregister_advertisement()
            return

        try:
            # Drop any stale advert object before re-registering.
            self._unregister_advertisement()
            self._adv = self._register_advertisement(self._adapter_path)
        except dbus.DBusException as exc:
            log.warning("Re-advertise failed: %s", exc)

    def _device_path(self, addr: str) -> dbus.ObjectPath:
        """BlueZ object path for a peer, e.g. /org/bluez/hci0/dev_A4_0D_BC_..."""
        return dbus.ObjectPath(
            f"{self._adapter_path}/dev_{addr.upper().replace(':', '_')}"
        )

    def _remove_bond(self, addr: str) -> None:
        """Delete a stale bond so the bike can re-pair cleanly.

        After a reboot or a controller change, the bond keys stored on the Pi
        can disagree with the bike's, causing the SMP handshake to fail with
        "Authentication Failed" and a connect/disconnect loop. BlueZ's
        Adapter1.RemoveDevice() drops the device and its stored keys; the bike
        then re-pairs automatically on its next connection attempt (our
        JustWorksAgent auto-accepts), with no manual intervention.
        """
        try:
            adapter = dbus.Interface(
                self._bus.get_object(BLUEZ_SERVICE, self._adapter_path),
                ADAPTER_IFACE,
            )
            adapter.RemoveDevice(self._device_path(addr))
            log.warning("Removed stale bond for %s — will re-pair on next connect", addr)
        except dbus.DBusException as exc:
            log.warning("RemoveDevice failed for %s: %s", addr, exc)
        finally:
            self._connect_fails[addr] = 0

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

        # ── Charge-time estimation ─────────────────────────────────────────
        est = bike.setdefault("charge_est", ChargeEstimator())
        # Reset the estimate whenever the charging flag flips (plug/unplug),
        # so a fresh session re-seeds the reference point.
        if "charger_connected" in data:
            prev = bike.get("_was_charging")
            if prev is not None and data["charger_connected"] != prev:
                est.reset()
            bike["_was_charging"] = data["charger_connected"]
        charging = bool(bike["state"].get("charger_connected"))
        soc = bike["state"].get("battery_soc")
        soc = float(soc) if soc is not None else None
        est.update(soc, charging)
        # Always (re)publish the two ETA fields so they clear to null when not
        # charging. JSON null → HA template '' default → entity shows unknown.
        bike["state"]["charge_eta_80_min"] = est.eta_to(soc, charging, 80.0)
        bike["state"]["charge_eta_100_min"] = est.eta_to(soc, charging, 100.0)

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

        # Successful resolve → connection is healthy, clear any failure count.
        self._connect_fails[bike_addr] = 0

        if bike_addr not in self._bikes:
            name = f"eBike {bike_addr[-5:].replace(':', '')}"
            self._bikes[bike_addr] = {"name": name, "state": {}, "char_path": char_path}
        else:
            self._bikes[bike_addr]["char_path"] = char_path

        self._mqtt.publish_availability(bike_addr, True)
        self._subscribe(char_path, bike_addr)

        # BlueZ released our advert when this bike connected. Re-register it so
        # a second eBike can still discover the bridge by name (not just MAC).
        self._readvertise()

    def _on_disconnected(self, addr: str) -> None:
        bike_addr = addr.upper()
        log.info("eBike disconnected: %s", bike_addr)

        # Was this a healthy session (services resolved) or a pre-resolve drop?
        # A drop before services ever resolved is the stale-bond signature.
        was_resolved = bool(self._bikes.get(bike_addr, {}).get("char_path"))

        if bike_addr in self._bikes:
            self._bikes[bike_addr]["state"] = {}
            self._bikes[bike_addr]["char_path"] = None
        self._mqtt.publish_availability(bike_addr, False)

        if was_resolved:
            # Clean disconnect of a working connection — not a failure.
            self._connect_fails[bike_addr] = 0
        else:
            self._connect_fails[bike_addr] = self._connect_fails.get(bike_addr, 0) + 1
            n = self._connect_fails[bike_addr]
            log.warning(
                "Pre-resolve disconnect for %s (%d/%d) — possible stale bond",
                bike_addr, n, STALE_BOND_THRESHOLD,
            )
            if n >= STALE_BOND_THRESHOLD:
                self._remove_bond(bike_addr)

        # Ensure the named advert is live again after a disconnect.
        self._readvertise()

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
        self._adapter_path = adapter_path
        self._setup_adapter(adapter_path)
        self._register_agent()

        # Open a pairing window on boot so a Flow app can add the bridge in the
        # first 5 minutes; afterwards it advertises privately. When privacy is
        # off, _pairing_open() is always True and this is a no-op distinction.
        self._pairing_until = time.monotonic() + PAIRING_WINDOW_SEC
        self._adv = self._register_advertisement(adapter_path)
        GLib.timeout_add_seconds(PAIRING_WINDOW_SEC + 1, self._pairing_window_elapsed)

        # Publish discovery for every configured bike up front, so their HA
        # entities (incl. the corrected 'connected' sensor) register even while
        # the bike is offline. Mark them offline until they actually connect.
        for addr, bike in self._bikes.items():
            self._mqtt.publish_discovery(addr, bike["name"])
            self._mqtt.publish_availability(addr, False)

        # Pairing controls (button + status) for Home Assistant. The button
        # press arrives on the MQTT thread, so marshal start_pairing() onto the
        # GLib main loop (all D-Bus/adv work must happen on that thread).
        self._mqtt.publish_pairing_discovery(
            lambda: GLib.idle_add(self._start_pairing_idle)
        )
        self._publish_pairing_state()

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

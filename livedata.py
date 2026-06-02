"""
Bosch eBike Live Data Interface – protobuf decoder.

Ported from esphome/components/bosch_ebike_ldi/livedata_decoder.cpp.
The on-wire format is proto3 with all payload fields as varints (wire type 0).
Unknown fields are silently skipped per proto3 forward-compatibility rules.
"""
from __future__ import annotations
import struct
import time


class ChargeEstimator:
    """Estimates remaining charge time to 80% and 100% from rising SoC.

    Ported from the upstream ESPHome bridge (feat: Restladezeit-Schätzung).
    While charging, it measures the charge rate (%/min) from the rising SoC,
    smoothed with an EMA, and extrapolates:
      • to 80%: linear (the whole CC region is roughly linear → accurate)
      • to 100%: CC up to 85%, then the slower CV tail at rate/2.5

    One instance per bike. Feed it every SoC sample via update(); call reset()
    on each charger plug/unplug transition so a new session re-seeds cleanly.
    """

    EMA_NEW = 0.3          # weight of the newest rate sample
    CV_START = 85.0        # %, where the slower constant-voltage phase begins
    CV_FACTOR = 2.5        # CV charges ~this many× slower than CC

    def __init__(self) -> None:
        self._rate: float | None = None   # %/min, None = unknown
        self._last_soc: float | None = None
        self._last_ms: float | None = None

    def reset(self) -> None:
        """Clear the estimate — call on every charger plug/unplug."""
        self._rate = None
        self._last_soc = None
        self._last_ms = None

    def update(self, soc: float | None, charging: bool) -> None:
        """Feed one SoC sample. Updates the smoothed rate once per +1% gained."""
        if not charging or soc is None:
            return
        now = time.monotonic() * 1000.0  # ms, monotonic (immune to clock changes)
        if self._last_soc is None:
            self._last_soc = soc
            self._last_ms = now
            return
        dsoc = soc - self._last_soc
        if dsoc >= 1.0:
            dmin = (now - self._last_ms) / 60000.0
            if dmin > 0.01:
                r = dsoc / dmin
                if r > 0:
                    self._rate = r if self._rate is None else (
                        self.EMA_NEW * r + (1 - self.EMA_NEW) * self._rate
                    )
            self._last_soc = soc
            self._last_ms = now

    def eta_to(self, soc: float | None, charging: bool, target: float) -> int | None:
        """Minutes remaining until `target`%, or None if not yet estimable."""
        if not charging or soc is None or self._rate is None or self._rate <= 0:
            return None
        if soc >= target:
            return None
        rate = self._rate
        if target <= self.CV_START:
            return round((target - soc) / rate)
        # Target above the CV knee: linear CC up to CV_START, slower CV after.
        mins = 0.0
        s = soc
        if s < self.CV_START:
            mins += (self.CV_START - s) / rate
            s = self.CV_START
        mins += (target - s) / (rate / self.CV_FACTOR)
        return round(mins)


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    """Read a base-128 varint at pos. Returns (value, new_pos)."""
    result = 0
    shift = 0
    while pos < len(data):
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if (b & 0x80) == 0:
            return result, pos
        shift += 7
        if shift >= 64:
            raise ValueError("varint overflow")
    raise ValueError("truncated varint")


def _to_int32(v: int) -> int:
    """Reinterpret uint64 as signed int32 (two's-complement truncation)."""
    v32 = v & 0xFFFFFFFF
    return struct.unpack("<i", struct.pack("<I", v32))[0]


def decode_live_data(data: bytes) -> dict:
    """
    Decode one Bosch LDI LiveData protobuf notification.

    Returns a dict containing whichever fields were present in this packet.
    Notifications are sparse – only changed fields are included per spec §2.2.4.3.
    Callers should merge returned dicts into a running state dict.

    Possible keys and their HA-friendly units:
      speed_kmh        float   km/h
      cadence          int     rpm  (signed – negative = backwards)
      rider_power      int     W
      ambient_lux      float   lx
      battery_soc      int     %
      timestamp        int     seconds since Unix epoch
      odometer_km      float   km
      bike_light       bool    True = light on
      system_locked    bool    True = locked
      charger_connected bool
      light_reserve    bool
      diagnosis_active bool
      in_motion        bool    True = bicycle is moving
                               (inverted from wire field "bike_not_driving")
    """
    out: dict = {}
    pos = 0

    while pos < len(data):
        try:
            tag, pos = _read_varint(data, pos)
        except ValueError:
            break

        field_no = tag >> 3
        wire_type = tag & 0x07

        # Skip any non-varint field (forward-compat)
        if wire_type != 0:
            try:
                if wire_type == 1:    # 64-bit fixed
                    pos += 8
                elif wire_type == 2:  # length-delimited
                    length, pos = _read_varint(data, pos)
                    pos += length
                elif wire_type == 5:  # 32-bit fixed
                    pos += 4
                else:
                    break             # unknown wire type – bail
            except (ValueError, IndexError):
                break
            continue

        try:
            v, pos = _read_varint(data, pos)
        except ValueError:
            break

        # Field numbers mirror livedata_decoder.cpp switch statement.
        if field_no == 1:
            out["speed_kmh"] = v / 100.0            # raw is 1/100 km/h
        elif field_no == 2:
            out["cadence"] = _to_int32(v)           # rpm, signed
        elif field_no == 5:
            out["rider_power"] = int(v)             # W
        elif field_no == 9:
            out["ambient_lux"] = v / 1000.0         # raw is 1/1000 lux
        elif field_no == 10:
            out["battery_soc"] = int(v)             # %
        elif field_no == 11:
            out["timestamp"] = int(v)               # Unix epoch seconds
        elif field_no == 12:
            out["odometer_km"] = v / 1000.0         # raw is metres
        elif field_no == 17:
            out["bike_light"] = (v == 2)            # 2=on, 1=off, 0=invalid
        elif field_no == 21:
            out["system_locked"] = bool(v)
        elif field_no == 22:
            out["charger_connected"] = bool(v)
        elif field_no == 23:
            out["light_reserve"] = bool(v)
        elif field_no == 24:
            out["diagnosis_active"] = bool(v)
        elif field_no == 25:
            out["in_motion"] = not bool(v)          # wire = "bike_not_driving"

    return out

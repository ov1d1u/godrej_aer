import logging
import struct
from datetime import datetime, timedelta

from .const import SPRAY_CAPACITY

_LOGGER = logging.getLogger(__name__)

DEVICE_STATUS_VALIDITY_TIME = timedelta(minutes=60)

STATUS_LEN = 99

# currentStatus (byte 41) -> auto-spray interval in minutes. The tens digit
# encodes the trigger (spray-now / schedule / button), the units digit the
# interval. 111 / 0 means idle. Mirrors AppUtility.getCurrentDeviceInterval().
_INTERVAL_BY_STATUS = {
    112: 10, 121: 10, 131: 10,
    113: 20, 122: 20, 132: 20,
    114: 40, 123: 40, 133: 40,
}


def _u16(data: bytes, offset: int) -> int:
    return struct.unpack_from(">H", data, offset)[0]


def _ascii(data: bytes, start: int, end: int) -> str:
    return data[start:end].decode("ascii", "replace").strip("\x00 ").strip()


class DeviceStatus:
    battery_mv: int | None = None
    current_status: int | None = None
    firmware_version: str | None = None
    structure_version: str | None = None
    total_spray_count: int | None = None

    def __init__(self, status_bytes: bytes):
        self.date = datetime.now()

        if len(status_bytes) != STATUS_LEN:
            _LOGGER.debug(
                "Unexpected status length %d, expected %d",
                len(status_bytes), STATUS_LEN
            )
            return

        self.message_type = status_bytes[5]
        self.message_id = status_bytes[10]
        self.acknowledgement = status_bytes[16]

        self.battery_mv = struct.unpack_from(">I", status_bytes, 21)[0]
        self.firmware_version = _ascii(status_bytes, 29, 37)
        self.current_status = status_bytes[41]
        self.structure_version = _ascii(status_bytes, 92, 98)

        # Per-trigger, per-interval spray counters (big-endian uint16).
        try:
            counts = {
                "spray_now_0": _u16(status_bytes, 51),
                "spray_now_10": _u16(status_bytes, 54),
                "spray_now_20": _u16(status_bytes, 57),
                "spray_now_40": _u16(status_bytes, 60),
                "schedule_10": _u16(status_bytes, 67),
                "schedule_20": _u16(status_bytes, 70),
                "schedule_40": _u16(status_bytes, 73),
                "button_10": _u16(status_bytes, 80),
                "button_20": _u16(status_bytes, 83),
                "button_40": _u16(status_bytes, 86),
            }
            self.spray_counts = counts
            self.total_spray_count = sum(counts.values())
        except struct.error:
            self.spray_counts = {}

    @property
    def battery_volts(self) -> float | None:
        if self.battery_mv is None:
            return None
        return round(self.battery_mv / 1000, 3)

    @property
    def spray_interval(self) -> int:
        """Auto-spray interval in minutes (0 when idle)."""
        return _INTERVAL_BY_STATUS.get(self.current_status, 0)

    @property
    def is_spraying(self) -> bool:
        """True when the device is in an auto-spray mode."""
        return self.spray_interval > 0

    @property
    def refill_percent(self) -> int | None:
        """Estimated cartridge level, 0-100, from the lifetime spray count."""
        if self.total_spray_count is None:
            return None
        used = min(self.total_spray_count, SPRAY_CAPACITY)
        return round((SPRAY_CAPACITY - used) / SPRAY_CAPACITY * 100)

    @property
    def is_valid(self):
        return self.battery_mv is not None and \
            datetime.now() < self.date + DEVICE_STATUS_VALIDITY_TIME

import struct
from datetime import datetime, timedelta

DEVICE_STATUS_VALIDITY_TIME = timedelta(minutes=60)

class DeviceStatus:
    battery_mv: int | None = None

    def __init__(self, status_bytes: bytes):
        self.date = datetime.now()
        if len(status_bytes) == 99:
            self.battery_mv = struct.unpack(
                ">I",
                status_bytes[21:25]
            )[0]

    @property
    def is_valid(self):
        return self.battery_mv is not None and \
            datetime.now() < self.date + DEVICE_STATUS_VALIDITY_TIME

"""Device- and BLE-connection constants for the Godrej Aer Smart Matic."""

# Nordic UART-style GATT service and characteristics exposed by the device.
MAIN_SVC = "6e400000-b5a3-f393-e0a9-e50e24dcca9e"
NOTIFY_CHAR = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
WRITE_CHAR = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"

# Seconds to stay connected after a successful exchange before dropping the
# link (keeping it open drains the device battery).
DISCONNECT_DELAY = 15
# Seconds to wait for the 99-byte status notification after asking for it.
STATUS_TIMEOUT = 30
# Seconds to back off polling after a failed connect attempt.
CONNECT_RETRY_COOLDOWN = 1800

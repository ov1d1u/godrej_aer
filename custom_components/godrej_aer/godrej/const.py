"""Device- and BLE-connection constants for the Godrej Aer Smart Matic."""

# Nordic UART-style GATT service and characteristics exposed by the device.
# The device's firmware is inconsistent about which characteristic actually
# carries the status notification (and whether it exposes a CCC descriptor),
# so the code subscribes to *every* notify characteristic in MAIN_SVC and
# tries NOTIFY_CHAR first. See SmartMatic._subscribe_notifications.
# Replies arrive on NOTIFY_CHAR (no CCCD) on all firmware seen so far.
MAIN_SVC = "6e400000-b5a3-f393-e0a9-e50e24dcca9e"
NOTIFY_CHAR = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"

# Command characteristic per firmware: 6e400002 (original),
# 6e400003 (3.0.6-T1), 6e400004 (3.0.8-IN). Detection probes every writable
# characteristic in MAIN_SVC, these first and in this order.
COMMAND_CHAR_CANDIDATES = (
    "6e400002-b5a3-f393-e0a9-e50e24dcca9e",
    "6e400003-b5a3-f393-e0a9-e50e24dcca9e",
    "6e400004-b5a3-f393-e0a9-e50e24dcca9e",
)
# Used while no channel is known (the historical default).
WRITE_CHAR = COMMAND_CHAR_CANDIDATES[0]

# Seconds to stay connected after a successful exchange before dropping the
# link (keeping it open drains the device battery).
DISCONNECT_DELAY = 15
# Seconds to wait for the status notification after asking for it.
STATUS_TIMEOUT = 30
# Seconds to wait for a reply from each candidate while probing.
PROBE_TIMEOUT = 5
# Seconds to wait after connecting before sending a button command.
# Polls wait 2 s, like the official app.
ACTION_SETTLE = 0.5
# Seconds to back off polling after a failed connect attempt.
CONNECT_RETRY_COOLDOWN = 300

# Auto-spray intervals the device (and the official app) support, in minutes.
SPRAY_INTERVALS = (10, 20, 40)
# Interval used when auto-spray is switched on without an explicit choice
# (matches the official app's on/off toggle).
DEFAULT_SPRAY_INTERVAL = 10
# Sprays a full cartridge is rated for; used to derive a refill percentage.
SPRAY_CAPACITY = 2200

# ATT MTU to request after connecting, matching the official app. The
# status frame (99-105 bytes) needs an MTU of ~108 to fit one notification;
# otherwise SmartMatic reassembles the fragments.
DESIRED_MTU = 185

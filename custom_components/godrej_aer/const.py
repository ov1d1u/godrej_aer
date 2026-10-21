"""Constants for the Godrej Aer Smart Matic integration."""

from .godrej.const import COMMAND_CHAR_CANDIDATES

DOMAIN = "godrej_aer"

CONNECTION_TIMEOUT = 120

CONF_COMMAND_CHAR = "command_char"
CONF_REPLY_CHAR = "reply_char"
CHANNEL_DETECT = "detect"

_CHANNEL_HINTS = {
    "6e400002-b5a3-f393-e0a9-e50e24dcca9e": "original firmware",
    "6e400003-b5a3-f393-e0a9-e50e24dcca9e": "fw 3.0.6-T1",
    "6e400004-b5a3-f393-e0a9-e50e24dcca9e": "fw 3.0.8-IN",
}
CHANNEL_CHOICES = {CHANNEL_DETECT: "Detect automatically on next connection"} | {
    uuid: f"{uuid[:8]} ({_CHANNEL_HINTS.get(uuid, 'unknown firmware')})"
    for uuid in COMMAND_CHAR_CANDIDATES
}

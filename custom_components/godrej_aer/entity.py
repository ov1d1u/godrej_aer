from __future__ import annotations
from homeassistant.core import callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH

from .godrej import SmartMatic

@callback
def async_device_device_info_fn(smartmatic: SmartMatic, name: str) -> DeviceInfo:
    return DeviceInfo(
        connections={(CONNECTION_BLUETOOTH, smartmatic.mac)},
        manufacturer="Godrej Aer",
        model="Smart Matic",
        name=name
    )
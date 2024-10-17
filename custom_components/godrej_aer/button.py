from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.core import HomeAssistant
from homeassistant.const import CONF_NAME
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import async_device_device_info_fn
from .godrej import SmartMatic


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback
):
    instance: SmartMatic = config_entry.runtime_data
    async_add_entities([
        SmartMaticButton(instance, config_entry)
    ])


class SmartMaticButton(ButtonEntity):
    """Representation of a Smart Matic button."""

    def __init__(self, instance: SmartMatic, config_entry: ConfigEntry):
        self._instance: SmartMatic = instance
        self._config_entry: ConfigEntry = config_entry
        self._attr_name = config_entry.data[CONF_NAME]
        self._attr_unique_id = f"{config_entry.data[CONF_NAME]}_trigger"
        self._attr_icon = "mdi:spray"

    @property
    def device_info(self) -> DeviceInfo:
        return async_device_device_info_fn(self._instance, self._config_entry.data[CONF_NAME])

    async def async_press(self) -> None:
        await self._instance.trigger()

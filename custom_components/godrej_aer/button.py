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
        SmartMaticSprayNowButton(instance, config_entry),
        SmartMaticResetRefillButton(instance, config_entry),
    ])


class SmartMaticButton(ButtonEntity):
    """Base for Smart Matic buttons."""

    _key: str
    _name_suffix: str = ""

    def __init__(self, instance: SmartMatic, config_entry: ConfigEntry):
        self._instance: SmartMatic = instance
        self._config_entry: ConfigEntry = config_entry
        name = config_entry.data[CONF_NAME]
        self._attr_name = f"{name} {self._name_suffix}".strip() if self._name_suffix else name
        self._attr_unique_id = f"{config_entry.entry_id}_{self._key}"

    @property
    def device_info(self) -> DeviceInfo:
        return async_device_device_info_fn(self._instance, self._config_entry.data[CONF_NAME])


class SmartMaticSprayNowButton(SmartMaticButton):
    """Spray once, now."""

    _key = "trigger"
    _attr_icon = "mdi:spray"

    async def async_press(self) -> None:
        await self._instance.spray_now()


class SmartMaticResetRefillButton(SmartMaticButton):
    """Tell the device its cartridge has been refilled."""

    _key = "reset_refill"
    _name_suffix = "Reset Refill Level"
    _attr_icon = "mdi:cup-water"

    async def async_press(self) -> None:
        await self._instance.reset_refill()

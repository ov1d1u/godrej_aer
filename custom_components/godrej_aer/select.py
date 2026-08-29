from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import async_device_device_info_fn
from .godrej import SmartMatic, DeviceStatus
from .godrej.const import SPRAY_INTERVALS
from .godrej.events import DEVICE_STATUS_UPDATE

_OPTIONS = {f"{minutes} minutes": minutes for minutes in SPRAY_INTERVALS}
_LABEL_BY_INTERVAL = {minutes: label for label, minutes in _OPTIONS.items()}


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
):
    instance: SmartMatic = config_entry.runtime_data
    async_add_entities([SmartMaticSprayIntervalSelect(instance, config_entry)])


class SmartMaticSprayIntervalSelect(SelectEntity):
    """Auto-spray interval for a Smart Matic.

    Selecting an interval also starts auto-spraying at it, mirroring the
    official app. While the device is idle there is no current option.
    """

    _attr_icon = "mdi:timer-cog"
    _attr_options = list(_OPTIONS)

    def __init__(self, instance: SmartMatic, config_entry: ConfigEntry):
        self._instance = instance
        self._config_entry = config_entry
        self._attr_name = f"{config_entry.data[CONF_NAME]} Spray Interval"
        self._attr_unique_id = f"{config_entry.entry_id}_spray_interval"
        self._attr_current_option = self._option_for(instance.device_status)

    @staticmethod
    def _option_for(status: DeviceStatus | None) -> str | None:
        if status is None or not status.is_spraying:
            return None
        return _LABEL_BY_INTERVAL.get(status.spray_interval)

    async def async_added_to_hass(self) -> None:
        self._instance.eventbus.add_listener(
            DEVICE_STATUS_UPDATE, self.on_status_update
        )

    async def async_will_remove_from_hass(self) -> None:
        self._instance.eventbus.remove_listener(
            DEVICE_STATUS_UPDATE, self.on_status_update
        )

    @property
    def device_info(self) -> DeviceInfo:
        return async_device_device_info_fn(
            self._instance, self._config_entry.data[CONF_NAME]
        )

    async def async_select_option(self, option: str) -> None:
        await self._instance.set_spray_interval(_OPTIONS[option])

    async def on_status_update(self, device_status: DeviceStatus):
        self._attr_current_option = self._option_for(device_status)
        self.async_write_ha_state()

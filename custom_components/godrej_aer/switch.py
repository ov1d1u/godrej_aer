from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import AcceptedButFlat, async_device_device_info_fn, async_send_command
from .godrej import SmartMatic, DeviceStatus
from .godrej.const import DEFAULT_SPRAY_INTERVAL
from .godrej.events import DEVICE_STATUS_UPDATE


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
):
    instance: SmartMatic = config_entry.runtime_data
    async_add_entities([SmartMaticAutoSpraySwitch(instance, config_entry)])


class SmartMaticAutoSpraySwitch(SwitchEntity):
    """Auto-spray on/off for a Smart Matic."""

    _attr_icon = "mdi:spray"

    def __init__(self, instance: SmartMatic, config_entry: ConfigEntry):
        self._instance = instance
        self._config_entry = config_entry
        self._attr_has_entity_name = True
        self._attr_name = "Spray: auto"
        self._attr_unique_id = f"{config_entry.entry_id}_auto_spray"
        # Interval to resume at when toggled on; tracks the last active one.
        self._last_interval = DEFAULT_SPRAY_INTERVAL
        status = instance.device_status
        self._attr_is_on = bool(status and status.is_spraying)

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

    async def async_turn_on(self, **kwargs) -> None:
        await self._command(self._instance.set_spray_interval(self._last_interval), True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._command(self._instance.stop_spray(), False)

    async def _command(self, command, is_on: bool) -> None:
        """Show the new state once the device acknowledges the command.

        Otherwise re-write the current state, so the toggle flips back,
        and raise; the status read-back that follows shows what the device
        is really doing.
        """
        try:
            await async_send_command(command)
        except AcceptedButFlat:
            # The device did switch modes; it just can't spray.
            self._attr_is_on = is_on
            self.async_write_ha_state()
            raise
        except Exception:
            self.async_write_ha_state()
            raise
        self._attr_is_on = is_on
        self.async_write_ha_state()

    async def on_status_update(self, device_status: DeviceStatus):
        if device_status.is_spraying:
            self._last_interval = device_status.spray_interval
        self._attr_is_on = device_status.is_spraying
        self.async_write_ha_state()

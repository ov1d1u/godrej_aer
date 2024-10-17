from __future__ import annotations

from homeassistant.const import UnitOfElectricPotential, CONF_NAME
from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity import DeviceInfo

from .entity import async_device_device_info_fn
from .godrej import SmartMatic, DeviceStatus
from .godrej.events import DEVICE_STATUS_UPDATE

async def async_setup_entry(hass, config_entry, async_add_entities):
    instance: SmartMatic = config_entry.runtime_data
    async_add_entities([
        SmartMaticBatteryVoltageSensor(instance, config_entry)
    ])


class SmartMaticBatteryVoltageSensor(SensorEntity):
    _attr_name = "SmartMatic Battery Voltage"
    _attr_native_unit_of_measurement = UnitOfElectricPotential.MILLIVOLT
    _attr_icon = "mdi:battery"

    def __init__(self, instance: SmartMatic, config_entry: ConfigEntry):
        self._instance: SmartMatic = instance
        self._config_entry = config_entry
        self._attr_unique_id = f"{config_entry.entry_id}_battery_voltage"

        instance.eventbus.add_listener(
            DEVICE_STATUS_UPDATE,
            self.on_status_update
        )

    @property
    def device_info(self) -> DeviceInfo:
        return async_device_device_info_fn(self._instance, self._config_entry.data[CONF_NAME])

    async def on_status_update(self, device_status: DeviceStatus):
        self._attr_native_value = device_status.battery_mv
        self.schedule_update_ha_state()

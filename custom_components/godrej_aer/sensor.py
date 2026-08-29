from __future__ import annotations

from collections.abc import Callable

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfElectricPotential, CONF_NAME
from homeassistant.helpers.entity import DeviceInfo

from .entity import async_device_device_info_fn
from .godrej import SmartMatic, DeviceStatus
from .godrej.events import DEVICE_STATUS_UPDATE


async def async_setup_entry(hass, config_entry, async_add_entities):
    instance: SmartMatic = config_entry.runtime_data
    async_add_entities([
        SmartMaticBatteryVoltageSensor(instance, config_entry),
        SmartMaticRefillLevelSensor(instance, config_entry),
        SmartMaticSprayCountSensor(instance, config_entry),
    ])


class SmartMaticSensor(SensorEntity):
    """Base for sensors driven by device status updates."""

    _key: str
    _value_fn: Callable[[DeviceStatus], float | int | None]

    def __init__(self, instance: SmartMatic, config_entry: ConfigEntry):
        self._instance = instance
        self._config_entry = config_entry
        self._attr_unique_id = f"{config_entry.entry_id}_{self._key}"
        if instance.device_status is not None:
            self._attr_native_value = self._value_fn(instance.device_status)

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

    async def on_status_update(self, device_status: DeviceStatus):
        self._attr_native_value = self._value_fn(device_status)
        self.async_write_ha_state()


class SmartMaticBatteryVoltageSensor(SmartMaticSensor):
    _key = "battery_voltage"
    _attr_name = "SmartMatic Battery Voltage"
    _attr_native_unit_of_measurement = UnitOfElectricPotential.MILLIVOLT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:battery"
    _value_fn = staticmethod(lambda status: status.battery_mv)


class SmartMaticRefillLevelSensor(SmartMaticSensor):
    _key = "refill_level"
    _attr_name = "SmartMatic Refill Level"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:cup-water"
    _value_fn = staticmethod(lambda status: status.refill_percent)


class SmartMaticSprayCountSensor(SmartMaticSensor):
    _key = "spray_count"
    _attr_name = "SmartMatic Spray Count"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_icon = "mdi:counter"
    _value_fn = staticmethod(lambda status: status.total_spray_count)

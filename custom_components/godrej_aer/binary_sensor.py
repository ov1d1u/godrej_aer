from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.helpers.entity import EntityCategory, DeviceInfo

from .entity import async_device_device_info_fn
from .godrej import SmartMatic
from .godrej.events import DEVICE_DISCONNECT, DEVICE_CONNECT

async def async_setup_entry(hass, config_entry, async_add_entities):
    instance: SmartMatic = config_entry.runtime_data
    async_add_entities([
        SmartMaticConnectedBinarySensor(instance, config_entry)
    ])


class SmartMaticConnectedBinarySensor(BinarySensorEntity):
    """Representation of a Smart Matic connected binary sensor."""

    def __init__(self, instance: SmartMatic, config_entry: ConfigEntry):
        self._instance: SmartMatic = instance
        self._config_entry = config_entry
        self._attr_name = f"{config_entry.data[CONF_NAME]} Connected"
        self._attr_unique_id = f"{config_entry.entry_id}_is_connected"
        self._attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_is_on = False
        self._attr_icon = "mdi:bluetooth-off"

    async def async_added_to_hass(self) -> None:
        self._instance.eventbus.add_listener(DEVICE_CONNECT, self.on_connect)
        self._instance.eventbus.add_listener(DEVICE_DISCONNECT, self.on_disconnect)

    async def async_will_remove_from_hass(self) -> None:
        self._instance.eventbus.remove_listener(DEVICE_CONNECT, self.on_connect)
        self._instance.eventbus.remove_listener(DEVICE_DISCONNECT, self.on_disconnect)

    @property
    def device_info(self) -> DeviceInfo:
        return async_device_device_info_fn(self._instance, self._config_entry.data[CONF_NAME])

    async def on_connect(self, instance: SmartMatic):
        self._attr_is_on = True
        self._attr_icon = "mdi:bluetooth-connect"
        self.async_write_ha_state()

    async def on_disconnect(self, instance: SmartMatic):
        self._attr_is_on = False
        self._attr_icon = "mdi:bluetooth-off"
        self.async_write_ha_state()

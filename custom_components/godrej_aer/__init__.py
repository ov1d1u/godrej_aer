"""The Godrej Aer Smart Matic integration."""
from __future__ import annotations
from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform, CONF_MAC
from homeassistant.core import HomeAssistant, callback
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth.match import ADDRESS, BluetoothCallbackMatcher
from homeassistant.helpers.event import async_track_time_interval

from .godrej import SmartMatic
from .services import async_setup_services, async_unload_services

_LOGGER = logging.getLogger(__name__)
PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BUTTON,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.SELECT,
]

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry
) -> bool:
    """Set up Godrej Aer Smart Matic from a config entry."""

    mac = entry.options.get(CONF_MAC, None) or entry.data.get(CONF_MAC, None)

    instance = SmartMatic(hass, mac)
    entry.runtime_data = instance

    async_setup_services(hass)

    async def _connect_if_needed():
        if bluetooth.async_address_present(hass, mac, connectable=True):
            try:
                await instance.connect_if_needed()
            except Exception:
                _LOGGER.debug("Background connect attempt failed for %s", mac, exc_info=True)

    @callback
    def _async_discovered_device(
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange
    ):
        """Subscribe to bluetooth changes."""
        _LOGGER.debug("New service_info: %s", service_info)
        hass.async_create_task(_connect_if_needed())

    entry.async_on_unload(
        bluetooth.async_register_callback(
            hass,
            _async_discovered_device,
            BluetoothCallbackMatcher({ADDRESS: mac}),
            bluetooth.BluetoothScanningMode.PASSIVE
        )
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    @callback
    def _async_periodic_connect(_now):
        hass.async_create_task(_connect_if_needed())

    entry.async_on_unload(
        async_track_time_interval(hass, _async_periodic_connect, timedelta(seconds=60))
    )

    await _connect_if_needed()

    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        instance: SmartMatic = entry.runtime_data
        await instance.disconnect()
        async_unload_services(hass, entry)
    return unload_ok

async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    _LOGGER.debug("Config entry %s updated", entry.entry_id)
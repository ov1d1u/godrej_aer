"""Config flow for Godrej Aer Smart Matic integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_MAC, CONF_NAME
from homeassistant.helpers.device_registry import format_mac
from homeassistant.components.bluetooth import (
    async_discovered_service_info
)

from .const import DOMAIN
from .godrej import SmartMatic

_LOGGER = logging.getLogger(__name__)

MANUAL_MAC = "manual_mac"


class GodrejAerConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self):
        self.name = "Godrej Aer Smart Matic"
        self.mac = None

    def _is_device_supported(self, device_info):
        return "smart matic" in device_info.name.lower()

    async def _validate_device(self, smartmatic):
        assert await smartmatic.connect()
        await smartmatic.disconnect()

        return None

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
        errors: dict[str, str] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        if user_input is not None:
            if user_input[CONF_MAC] == MANUAL_MAC:
                return await self.async_step_manual_mac()

            self.mac = user_input[CONF_MAC]
            await self.async_set_unique_id(format_mac(self.mac), raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return await self.async_step_validate()

        discovered_devices = []
        discovered_infos = async_discovered_service_info(
            self.hass,
            connectable=True
        )
        for device_info in discovered_infos:
            if self._is_device_supported(device_info):
                discovered_devices.append(device_info)

        device_options = {dev.address: f"{dev.name} ({dev.address})" for dev in discovered_devices}
        device_options[MANUAL_MAC] = "Enter MAC address manually"

        return self.async_show_form(
            step_id='user',
            data_schema=vol.Schema({
                vol.Required(CONF_MAC): vol.In(device_options)
            }),
            description_placeholders={
                "description": "Please select a device to configure"
            },
            errors=errors
        )

    async def async_step_manual_mac(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle manual mac step."""
        if user_input is not None:
            self.mac = user_input[CONF_MAC]
            await self.async_set_unique_id(format_mac(self.mac), raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return await self.async_step_validate()

        return self.async_show_form(
            step_id="manual_mac",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_MAC): str
                }
            ),
            errors={})

    async def async_step_validate(
        self, user_input: "dict[str, Any] | None" = None
    ) -> ConfigFlowResult:
        """Handle validate step."""
        error = None
        smartmatic = SmartMatic(self.hass, self.mac)
        try:
            error = await self._validate_device(smartmatic)
        except Exception as e:
            error = str(e)
        finally:
            await smartmatic.disconnect()

        if error:
            return await self.async_step_user(errors={"base": error})

        return await self.async_step_name()

    async def async_step_name(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle name step."""
        if user_input is not None:
            self.name = user_input[CONF_NAME]

            return self.async_create_entry(
                title=self.name,
                data = {
                    CONF_MAC: self.mac,
                    CONF_NAME: self.name
                }
            )

        return self.async_show_form(
            step_id="name",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME, default=self.name): str
                }
            ),
            errors={},
        )
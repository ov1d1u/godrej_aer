"""Config flow for Godrej Aer Smart Matic integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_MAC, CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers.device_registry import format_mac
from homeassistant.components.bluetooth import (
    async_discovered_service_info
)

from .const import (
    CHANNEL_CHOICES,
    CHANNEL_DETECT,
    CONF_COMMAND_CHAR,
    CONF_REPLY_CHAR,
    DOMAIN,
)
from .godrej import SmartMatic
from .godrej.exception import InvalidDeviceError, ConnectionError

_LOGGER = logging.getLogger(__name__)

MANUAL_MAC = "manual_mac"


class GodrejAerConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self):
        self.name = "Godrej Aer Smart Matic"
        self.mac = None
        self.command_char = None
        self.reply_char = None
        self._manual_entry = False

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return GodrejAerOptionsFlow(config_entry)

    def _is_device_supported(self, device_info):
        name = device_info.name or ""
        return "smart matic" in name.lower()

    async def _validate_device(self, smartmatic) -> str | None:
        return await smartmatic.detect_command_channel()

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
        errors: dict[str, str] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        if user_input is not None:
            if user_input[CONF_MAC] == MANUAL_MAC:
                self._manual_entry = True
                return await self.async_step_manual_mac()

            self._manual_entry = False
            self.mac = user_input[CONF_MAC]
            await self.async_set_unique_id(format_mac(self.mac), raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return await self.async_step_validate()

        configured_ids = self._async_current_ids()
        discovered_devices = []
        discovered_infos = async_discovered_service_info(
            self.hass,
            connectable=True
        )
        for device_info in discovered_infos:
            if not self._is_device_supported(device_info):
                continue
            if format_mac(device_info.address) in configured_ids:
                continue
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
        self,
        user_input: dict[str, Any] | None = None,
        errors: dict[str, str] | None = None,
    ) -> ConfigFlowResult:
        """Handle manual mac step."""
        if user_input is not None:
            self.mac = user_input[CONF_MAC]

            try:
                normalized_mac = format_mac(self.mac)
            except ValueError:
                return self.async_show_form(
                    step_id="manual_mac",
                    data_schema=vol.Schema({vol.Required(CONF_MAC): str}),
                    errors={"base": "invalid_mac"},
                )

            await self.async_set_unique_id(normalized_mac, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return await self.async_step_validate()

        return self.async_show_form(
            step_id="manual_mac",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_MAC): str
                }
            ),
            errors=errors or {},
        )

    async def async_step_validate(
        self, user_input: "dict[str, Any] | None" = None
    ) -> ConfigFlowResult:
        """Handle validate step."""
        error = None
        smartmatic = SmartMatic(self.hass, self.mac)
        try:
            self.command_char = await self._validate_device(smartmatic)
            self.reply_char = smartmatic.reply_char
        except InvalidDeviceError:
            error = "invalid_device"
        except ConnectionError:
            error = "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected error while validating %s", self.mac)
            error = "unknown"
        finally:
            await smartmatic.disconnect()

        if error:
            if self._manual_entry:
                return await self.async_step_manual_mac(errors={"base": error})
            return await self.async_step_user(errors={"base": error})

        if self.command_char is None:
            return await self.async_step_command_channel()
        return await self.async_step_name()

    async def async_step_command_channel(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Reachable but no reply: let the user choose."""
        if user_input is not None:
            choice = user_input[CONF_COMMAND_CHAR]
            self.command_char = None if choice == CHANNEL_DETECT else choice
            return await self.async_step_name()

        return self.async_show_form(
            step_id="command_channel",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_COMMAND_CHAR, default=CHANNEL_DETECT):
                        vol.In(CHANNEL_CHOICES)
                }
            ),
        )

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
                    CONF_NAME: self.name,
                    CONF_COMMAND_CHAR: self.command_char,
                    CONF_REPLY_CHAR: self.reply_char,
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

class GodrejAerOptionsFlow(OptionsFlow):
    """View, override or re-detect the stored command channel."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            choice = user_input[CONF_COMMAND_CHAR]
            command_char = None if choice == CHANNEL_DETECT else choice
            data = {**self._entry.data, CONF_COMMAND_CHAR: command_char}
            if command_char is None:
                # Detecting again: new firmware may reply elsewhere too.
                data[CONF_REPLY_CHAR] = None
            if data != dict(self._entry.data):
                self.hass.config_entries.async_update_entry(self._entry, data=data)
                self.hass.config_entries.async_schedule_reload(self._entry.entry_id)
            return self.async_create_entry(data=dict(self._entry.options))

        current = self._entry.data.get(CONF_COMMAND_CHAR)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_COMMAND_CHAR,
                        default=current if current in CHANNEL_CHOICES else CHANNEL_DETECT,
                    ): vol.In(CHANNEL_CHOICES)
                }
            ),
            description_placeholders={
                "current": current[:8] if current else "not detected yet"
            },
        )

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

from .const import CONF_COMMAND_CHAR, CONF_REPLY_CHAR, DOMAIN
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

    def _is_device_supported(self, device_info):
        name = device_info.name or ""
        return "smart matic" in name.lower()

    async def _detect(self, smartmatic: SmartMatic) -> str | None:
        """Detect the channels; return an error key, or None on success."""
        try:
            channels = await smartmatic.detect_channels()
        except InvalidDeviceError:
            return "invalid_device"
        except ConnectionError:
            return "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected error while validating %s", self.mac)
            return "unknown"
        if channels is None:
            return "no_reply"
        self.command_char, self.reply_char = channels
        return None

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
        smartmatic = SmartMatic(self.hass, self.mac)
        try:
            error = await self._detect(smartmatic)
        finally:
            await smartmatic.disconnect()

        if error:
            if self._manual_entry:
                return await self.async_step_manual_mac(errors={"base": error})
            return await self.async_step_user(errors={"base": error})

        return await self.async_step_name()

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Detect the command and reply channels again."""
        entry = self._get_reconfigure_entry()
        errors = {}
        if user_input is not None:
            self.mac = entry.options.get(CONF_MAC) or entry.data[CONF_MAC]
            # The device takes one connection: use the loaded instance.
            instance = getattr(entry, "runtime_data", None)
            if not isinstance(instance, SmartMatic):
                instance = SmartMatic(self.hass, self.mac)
            error = await self._detect(instance)
            if error is None:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_COMMAND_CHAR: self.command_char,
                        CONF_REPLY_CHAR: self.reply_char,
                    },
                )
            errors["base"] = error

        return self.async_show_form(
            step_id="reconfigure",
            errors=errors,
            description_placeholders={
                "command": _short(entry.data.get(CONF_COMMAND_CHAR)),
                "reply": _short(entry.data.get(CONF_REPLY_CHAR)),
            },
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


def _short(uuid: str | None) -> str:
    return uuid[:8] if uuid else "not detected"

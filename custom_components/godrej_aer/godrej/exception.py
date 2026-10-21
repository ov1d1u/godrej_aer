from homeassistant.exceptions import HomeAssistantError


class InvalidDeviceError(HomeAssistantError):
    pass

class NotConnectedError(HomeAssistantError):
    pass

class ConnectionError(HomeAssistantError):
    pass

class CommandNotConfirmed(HomeAssistantError):
    """The device did not acknowledge a command (or rejected it)."""

class BatteryTooLow(HomeAssistantError):
    """A spray command was acknowledged, but the batteries are below the
    device's cut-off, so the motor won't run."""

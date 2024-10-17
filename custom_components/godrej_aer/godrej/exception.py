from homeassistant.exceptions import HomeAssistantError


class InvalidDeviceError(HomeAssistantError):
    pass

class NotConnectedError(HomeAssistantError):
    pass

class ConnectionError(HomeAssistantError):
    pass
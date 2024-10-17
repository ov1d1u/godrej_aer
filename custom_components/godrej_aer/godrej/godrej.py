import asyncio
import logging
from bleak import BleakClient, BleakError

from homeassistant.components.bluetooth import (
    async_ble_device_from_address
)

from ..const import (
    DISCONNECT_DELAY,
    CONNECTION_TIMEOUT
)
from .eventbus import EventBus
from .devicestatus import DeviceStatus
from .exception import (
    InvalidDeviceError,
    NotConnectedError,
    ConnectionError
)
from .events import (
    DEVICE_CONNECT,
    DEVICE_DISCONNECT,
    DEVICE_STATUS_UPDATE
)

_LOGGER = logging.getLogger(__name__)

MAIN_SVC    = "6e400000-b5a3-f393-e0a9-e50e24dcca9e"
NOTIFY_CHAR = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
WRITE_CHAR  = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"


class SmartMatic:
    client: BleakClient | None = None
    device_status: DeviceStatus | None = None
    eventbus: EventBus = EventBus()

    _connect_lock = asyncio.Lock()
    _device_status_event = asyncio.Event()
    _disconnect_task: asyncio.Task = None

    def __init__(self, hass, mac):
        self.hass = hass
        self.mac = mac

    async def connect(self) -> bool:
        async with self._connect_lock:
            if self.client and self.client.is_connected:
                return True

            device = async_ble_device_from_address(
                self.hass, self.mac
            )
            self.client = BleakClient(
                device,
                disconnected_callback=self._on_disconnect
            )

            _LOGGER.debug(f"Connecting to {self.mac}...")
            try:
                await self.client.connect()
            except Exception as e:
                _LOGGER.debug(f"Failed to connect to {self.mac}: {e}")
                raise ConnectionError(f"Failed to connect to device: {e}")

            await asyncio.sleep(2.0)  # give some time for service discovery

            _LOGGER.debug(f"Connected to {self.mac}")

            services = self.client.services
            if not MAIN_SVC in [service.uuid for service in services]:
                self.client.disconnect()
                raise InvalidDeviceError("Device does not look right")

            self.eventbus.send(DEVICE_CONNECT, self)

            try:
                await self.client.start_notify(
                    NOTIFY_CHAR,
                    self._notification_handler
                )
            except Exception as e:
                # This always triggers an error for some reason
                # (Characteristic 6e400001-b5a3-f393-e0a9-e50e24dcca9e
                # does not have a characteristic client config descriptor),
                # but notifications are enabled anyway.
                pass

            await self.get_device_status()

            return True

    async def connect_if_needed(self) -> bool:
        if not self.device_status or not self.device_status.is_valid:
            await self.connect()
            return True

        return False

    async def disconnect(self):
        if self.client and self.client.is_connected:
            _LOGGER.debug(f"Disconnecting from {self.mac}...")
            await self.client.disconnect()
            return True

    async def delayed_disconnect(self):
        async def _delayed_disconnect():
            if not self.client.is_connected:
                return

            try:
                await asyncio.sleep(DISCONNECT_DELAY)
                await self.disconnect()
            except Exception as e:
                _LOGGER.debug(f"Failed to disconnect. Error: {e}")

        loop = asyncio.get_running_loop()
        if self._disconnect_task is not None:
            self._disconnect_task.cancel()
        self._disconnect_task = loop.create_task(_delayed_disconnect())

    async def get_device_status(self):
        await self._ensure_connected()

        await self.client.write_gatt_char(
            WRITE_CHAR,
            bytes.fromhex("bf626d54186b626d4e189dff")
        )
        await self._device_status_event.wait()

        await self.delayed_disconnect()

    async def trigger(self):
        await self._ensure_connected()

        await self.client.write_gatt_char(
            WRITE_CHAR,
            bytes.fromhex("bf626d541868626d4e189a62724900ff")
        )

        await self.delayed_disconnect()

    async def _ensure_connected(self):
        async def wait_for_connected():
            while not self.client or not self.client.is_connected:
                await self.connect()

        try:
            await asyncio.wait_for(wait_for_connected(), CONNECTION_TIMEOUT)
        except asyncio.TimeoutError:
            raise NotConnectedError("Connection timeout")

    async def _notification_handler(self, sender, data):
        if sender.uuid.lower() == NOTIFY_CHAR.lower():
            _LOGGER.debug(f"<< {sender.uuid}: {data.hex()}")
            if len(data) == 99:
                self.device_status = DeviceStatus(data)
                self._device_status_event.set()
                self.eventbus.send(DEVICE_STATUS_UPDATE, self.device_status)

    def _on_disconnect(self, client: BleakClient):
        if self._disconnect_task is not None:
            self._disconnect_task.cancel()
            self._disconnect_task = None

        self.client = None
        self.eventbus.send(DEVICE_DISCONNECT, self)

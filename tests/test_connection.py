"""Connection regressions, runnable without Home Assistant or BLE hardware."""
import asyncio
import importlib
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch


def load_driver():
    package = ModuleType("smartmatic_test_driver")
    package.__path__ = [str(Path(__file__).resolve().parents[1] /
                            "custom_components/godrej_aer/godrej")]
    stubs = {"smartmatic_test_driver": package}
    for name in ("bleak", "bleak_retry_connector", "homeassistant",
                 "homeassistant.components", "homeassistant.components.bluetooth",
                 "homeassistant.util", "homeassistant.exceptions"):
        stubs[name] = ModuleType(name)
    stubs["bleak"].BleakClient = Mock
    stubs["bleak_retry_connector"].establish_connection = AsyncMock()
    stubs["homeassistant.components.bluetooth"].async_ble_device_from_address = Mock()
    stubs["homeassistant.util"].dt = SimpleNamespace()
    stubs["homeassistant.exceptions"].HomeAssistantError = type(
        "HomeAssistantError", (Exception,), {})
    with patch.dict(sys.modules, stubs):
        return importlib.import_module("smartmatic_test_driver.godrej")


driver = load_driver()


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.device = driver.SmartMatic(None, "AA:BB:CC:DD:EE:FF")
        self.client = SimpleNamespace(is_connected=True, write_gatt_char=AsyncMock(),
                                      disconnect=AsyncMock())
        self.device.client = self.client

    async def asyncTearDown(self):
        task = self.device._disconnect_task
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_missing_client_is_connection_error(self):
        self.device.client = None
        with self.assertRaises(driver.ConnectionError):
            await self.device._write_message(driver.SPRAY_NOW_MSG)

    async def test_disconnect_during_notification_setup(self):
        self.device.client = None
        self.client.services = SimpleNamespace(get_service=Mock(return_value=object()))

        async def subscribe(*args):
            self.client.is_connected = False
            self.device._on_disconnect(self.client)

        with patch.object(driver, "establish_connection", AsyncMock(return_value=self.client)), \
             patch.object(driver.asyncio, "sleep", AsyncMock()), \
             patch.object(self.device, "_subscribe_notifications", subscribe):
            with self.assertRaises(driver.ConnectionError):
                await self.device.spray_now()
        self.client.write_gatt_char.assert_not_awaited()

    async def test_old_disconnect_does_not_clear_new_client(self):
        self.device._on_disconnect(SimpleNamespace(is_connected=False))
        self.assertIs(self.device.client, self.client)
        await self.device.spray_now()
        self.client.write_gatt_char.assert_awaited_once_with(
            driver.WRITE_CHAR, driver.SPRAY_NOW_MSG)

    async def test_action_waits_for_poll_and_reconnects_after_cleanup(self):
        entered = asyncio.Event()
        finish = asyncio.Event()

        async def status(**kwargs):
            entered.set()
            await finish.wait()
            raise driver.ConnectionError("No status")

        async def open_link():
            if self.device.client is None:
                self.device.client = self.client

        with patch.object(self.device, "_open_connection", open_link), \
             patch.object(self.device, "_set_time", AsyncMock()), \
             patch.object(self.device, "get_device_status", status):
            poll = asyncio.create_task(self.device.connect())
            await entered.wait()
            action = asyncio.create_task(self.device.spray_now())
            await asyncio.sleep(0)
            self.client.write_gatt_char.assert_not_awaited()
            finish.set()
            with self.assertRaises(driver.ConnectionError):
                await poll
            await action
        self.client.write_gatt_char.assert_awaited_once_with(
            driver.WRITE_CHAR, driver.SPRAY_NOW_MSG)

    async def test_pending_disconnect_is_cancelled_before_action(self):
        await self.device.delayed_disconnect()
        old_task = self.device._disconnect_task
        await self.device.spray_now()
        await asyncio.sleep(0)
        self.assertTrue(old_task.cancelled())
        self.client.disconnect.assert_not_awaited()
        self.client.write_gatt_char.assert_awaited_once()

    async def test_disconnect_waits_for_in_flight_action(self):
        entered = asyncio.Event()
        finish = asyncio.Event()

        async def write(*args):
            entered.set()
            await finish.wait()

        self.client.write_gatt_char.side_effect = write
        action = asyncio.create_task(self.device.spray_now())
        await entered.wait()
        disconnect = asyncio.create_task(self.device.disconnect())
        await asyncio.sleep(0)
        self.client.disconnect.assert_not_awaited()
        finish.set()
        await action
        await disconnect
        self.client.disconnect.assert_awaited_once()

    async def test_failed_write_is_reported_without_retry(self):
        self.client.write_gatt_char.side_effect = RuntimeError("Link lost")
        with self.assertRaisesRegex(driver.ConnectionError, "Link lost"):
            await self.device.spray_now()
        self.client.write_gatt_char.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()

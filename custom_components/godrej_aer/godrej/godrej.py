import asyncio
import logging
from datetime import datetime, timedelta
from bleak import BleakClient
from bleak_retry_connector import establish_connection

from homeassistant.components.bluetooth import (
    async_ble_device_from_address
)
from homeassistant.util import dt as dt_util

from .const import (
    DISCONNECT_DELAY,
    STATUS_TIMEOUT,
    CONNECT_RETRY_COOLDOWN,
    MAIN_SVC,
    NOTIFY_CHAR,
    WRITE_CHAR,
    SPRAY_INTERVALS,
    DEFAULT_SPRAY_INTERVAL,
    DESIRED_MTU,
)
from .eventbus import EventBus
from .devicestatus import DeviceStatus, STATUS_LEN
from .exception import (
    InvalidDeviceError,
    ConnectionError
)
from .events import (
    DEVICE_CONNECT,
    DEVICE_DISCONNECT,
    DEVICE_STATUS_UPDATE
)

_LOGGER = logging.getLogger(__name__)


def _cbor_uint(n: int) -> bytes:
    """Encode a non-negative int as a CBOR major-type-0 value."""
    if n < 24:
        return bytes([n])
    if n < 0x100:
        return bytes([0x18, n])
    if n < 0x10000:
        return bytes([0x19]) + n.to_bytes(2, "big")
    if n < 0x100000000:
        return bytes([0x1A]) + n.to_bytes(4, "big")
    return bytes([0x1B]) + n.to_bytes(8, "big")


def _cbor_int(n: int) -> bytes:
    """Encode a (possibly negative) int as CBOR."""
    if n >= 0:
        return _cbor_uint(n)
    head = _cbor_uint(-1 - n)
    return bytes([head[0] | 0x20]) + head[1:]


def _cbor_key(text: str) -> bytes:
    """Encode a short (<24 char) ASCII map key as a CBOR text string."""
    raw = text.encode("ascii")
    return bytes([0x60 | len(raw)]) + raw


def encode_message(**fields: int | bool) -> bytes:
    """Encode a device message as an indefinite-length CBOR map.

    Mirrors the official app, which builds these with Jackson's
    ``writeStartObject()`` / ``writeEndObject()`` (emitting ``BF … FF``)
    and passes the *response* message type as ``mN``. Values are ints, or
    bools (encoded as CBOR true/false).
    """
    out = bytearray([0xBF])  # indefinite-length map
    for key, value in fields.items():
        out += _cbor_key(key)
        if isinstance(value, bool):  # must precede the int check
            out += bytes([0xF5 if value else 0xF4])
        else:
            out += _cbor_int(value)
    out += bytes([0xFF])  # break
    return bytes(out)


def spray_now_msg(interval: int = 0) -> bytes:
    """Spray-now message. ``interval`` in minutes; 0 means "once, now",
    a positive value starts auto-spraying at that interval."""
    return encode_message(mT=104, mN=154, rI=interval)


# {"mT": 107, "mN": 157} -> bf626d54186b626d4e189dff
FETCH_STATUS_MSG = encode_message(mT=107, mN=157)
# {"mT": 104, "mN": 154, "rI": 0} -> bf626d541868626d4e189a62724900ff
SPRAY_NOW_MSG = spray_now_msg(0)
# {"mT": 106, "mN": 156, "sS": true} -> stop auto-spray
STOP_SPRAY_MSG = encode_message(mT=106, mN=156, sS=True)
# {"mT": 105, "mN": 155, "rR": true} -> mark the cartridge as refilled
RESET_REFILL_MSG = encode_message(mT=105, mN=155, rR=True)


def encode_set_time(now: datetime | None = None) -> bytes:
    """Build the ``mT=102`` set-time message the way the official app does.

    The app sends an indefinite-length CBOR map with the epoch (``eP``,
    always forced to a fixed 8-byte integer), the standard (non-DST) UTC
    offset in minutes negated (``tZ``), and the zone's DST savings in
    milliseconds (``tD``). ``mN`` is the response message type (152), same
    quirk as the fetch-status message.
    """
    now = now or dt_util.now()
    epoch = int(now.timestamp())

    utcoffset = now.utcoffset() or timedelta()
    dst = now.dst() or timedelta()
    raw_offset_minutes = int((utcoffset - dst).total_seconds() // 60)
    tz = raw_offset_minutes * -1

    tzinfo = now.tzinfo
    dst_savings_ms = 0
    if tzinfo is not None:
        year = now.year
        jan = datetime(year, 1, 1, tzinfo=tzinfo).dst() or timedelta()
        jul = datetime(year, 7, 1, tzinfo=tzinfo).dst() or timedelta()
        dst_savings_ms = int(max(jan, jul).total_seconds() * 1000)

    out = bytearray([0xBF])  # indefinite-length map
    out += _cbor_key("mT") + _cbor_uint(102)
    out += _cbor_key("mN") + _cbor_uint(152)
    out += _cbor_key("eP") + bytes([0x1B]) + epoch.to_bytes(8, "big")
    out += _cbor_key("tZ") + _cbor_int(tz)
    out += _cbor_key("tD") + _cbor_int(dst_savings_ms)
    out += bytes([0xFF])
    return bytes(out)


class SmartMatic:
    def __init__(self, hass, mac):
        self.hass = hass
        self.mac = mac
        self.client: BleakClient | None = None
        self.device_status: DeviceStatus | None = None
        self.eventbus = EventBus()

        self._connect_lock = asyncio.Lock()
        self._device_status_event = asyncio.Event()
        self._disconnect_task: asyncio.Task | None = None
        self._retry_after: datetime | None = None

    async def connect(
        self,
        *,
        require_status: bool = True,
        status_timeout: float = STATUS_TIMEOUT,
    ) -> bool:
        """Open the BLE link and fetch a fresh device status.

        Used by the polling path. A status timeout here is treated as a
        connect failure. For just actuating the device (button press) use
        trigger(), which does not depend on the status read.

        With ``require_status=False`` (used by the config flow) a failure to
        read the status back is logged but not raised: getting the BLE link
        up and confirming the device looks right is enough. ``status_timeout``
        caps how long to wait for that status frame.
        """
        async with self._connect_lock:
            await self._open_connection()

            try:
                await self._set_time()
                await self.get_device_status(timeout=status_timeout)
            except (asyncio.CancelledError, InvalidDeviceError):
                await self._cleanup_connection()
                raise
            except BaseException as e:
                if require_status:
                    await self._cleanup_connection()
                    raise
                _LOGGER.warning(
                    "Connected to %s but could not read device status: %s",
                    self.mac, e
                )
                await self.delayed_disconnect()

        return True

    async def _open_connection(self):
        """Establish the BLE link and subscribe to notifications.

        The caller must hold ``self._connect_lock``. On any failure after
        the link is up, the connection is torn down before the exception
        propagates, so a failed attempt never leaves the device connected
        (which would drain its battery).
        """
        _LOGGER.debug("Trying to connect to device %s...", self.mac)

        if self.client and self.client.is_connected:
            _LOGGER.debug("Already connected to %s", self.mac)
            return

        device = async_ble_device_from_address(
            self.hass, self.mac
        )
        if device is None:
            raise ConnectionError(
                f"Device {self.mac} is not currently available over Bluetooth"
            )

        _LOGGER.debug("Connecting to %s...", self.mac)
        try:
            client = await establish_connection(
                BleakClient,
                device,
                self.mac,
                disconnected_callback=self._on_disconnect,
            )
        except Exception as e:
            _LOGGER.debug("Failed to connect to %s: %s", self.mac, e)
            raise ConnectionError(f"Failed to connect to device: {e}") from e

        self.client = client

        # From here on the BLE link is open. Any failure must disconnect
        # it, otherwise we stay connected indefinitely and drain the
        # device battery.
        try:
            await asyncio.sleep(2.0)  # give some time for service discovery

            if not client.is_connected:
                raise ConnectionError(
                    f"Device {self.mac} disconnected during service discovery"
                )

            _LOGGER.debug("Connected to %s, discovering services...", self.mac)

            services = client.services
            if _LOGGER.isEnabledFor(logging.DEBUG):
                for service in services:
                    for char in service.characteristics:
                        _LOGGER.debug(
                            "%s: char %s props=%s",
                            self.mac, char.uuid, ",".join(char.properties)
                        )

            main_svc = services.get_service(MAIN_SVC)
            if main_svc is None:
                raise InvalidDeviceError("Device does not look right")

            self.eventbus.send(DEVICE_CONNECT, self)

            await self._subscribe_notifications(client, main_svc)
            await self._request_mtu()
        except BaseException:
            # Includes asyncio.CancelledError: if the caller's task is
            # cancelled mid-connect we still must not leak the BLE link.
            await self._cleanup_connection()
            raise

    async def _subscribe_notifications(self, client, main_svc):
        """Subscribe to every notify characteristic in the main service.

        The device is Nordic UART-style, but its firmware is inconsistent
        about *which* characteristic carries the 99-byte status frame and
        about whether a given one exposes the Client Characteristic Config
        descriptor (CCCD) that strict BLE stacks require - the ESPHome
        Bluetooth proxy (connection v3) refuses ``start_notify`` on a
        characteristic without one:

            Characteristic 6e400001-... does not have a characteristic
            client config descriptor.

        Rather than hard-code one channel, subscribe to all of them and let
        ``_notification_handler`` pick the status frame out of whichever one
        actually delivers it. As long as a single subscription succeeds we
        can receive status updates.
        """
        notify_chars = [
            char for char in main_svc.characteristics
            if "notify" in char.properties or "indicate" in char.properties
        ]
        # Try the conventional Nordic UART TX characteristic first.
        notify_chars.sort(key=lambda c: c.uuid.lower() != NOTIFY_CHAR.lower())

        subscribed = 0
        for char in notify_chars:
            try:
                _LOGGER.debug("Subscribing to notifications on %s...", char.uuid)
                await client.start_notify(char, self._notification_handler)
                subscribed += 1
            except Exception as e:
                _LOGGER.debug(
                    "Failed start_notify on %s for %s: %s",
                    char.uuid, self.mac, e
                )

        if subscribed:
            _LOGGER.debug(
                "Subscribed to %d/%d notify characteristic(s) on %s",
                subscribed, len(notify_chars), self.mac
            )
        else:
            _LOGGER.warning(
                "Could not subscribe to any notify characteristic on %s; "
                "device status updates will not be received",
                self.mac
            )

    async def _request_mtu(self):
        """Negotiate a larger ATT MTU, mirroring the official app.

        Best effort only. Which of these works depends on the bleak
        backend: BlueZ negotiates automatically and exposes only a way to
        force the exchange early; CoreBluetooth and ESPHome proxies size
        the MTU themselves. A failure here is harmless, so it is only
        logged.
        """
        client = self.client
        if client is None:
            return

        try:
            request_mtu = getattr(client, "request_mtu", None)
            if request_mtu is not None:
                # Not in bleak's public API today, but some wrappers add it.
                await request_mtu(DESIRED_MTU)
            else:
                backend = getattr(client, "_backend", None)
                # BlueZ: force the MTU exchange that would otherwise be lazy.
                negotiate = (
                    getattr(backend, "_acquire_mtu", None)
                    or getattr(backend, "_negotiate_mtu", None)
                )
                if negotiate is not None:
                    await negotiate()
        except Exception as e:
            _LOGGER.debug("MTU negotiation with %s failed: %s", self.mac, e)

        try:
            _LOGGER.debug("MTU for %s is %s", self.mac, client.mtu_size)
        except Exception:
            pass

    async def connect_if_needed(self) -> bool:
        if self.device_status and self.device_status.is_valid:
            return False

        if self._retry_after is not None and datetime.now() < self._retry_after:
            _LOGGER.debug(
                "Skipping connect to %s, in retry cooldown until %s",
                self.mac, self._retry_after
            )
            return False

        if self._connect_lock.locked():
            _LOGGER.debug(
                "Skipping connect to %s, an attempt is already in progress",
                self.mac
            )
            return False

        try:
            await self.connect()
        except Exception:
            self._retry_after = datetime.now() + timedelta(
                seconds=CONNECT_RETRY_COOLDOWN
            )
            _LOGGER.debug(
                "Connect to %s failed, next attempt no sooner than %s",
                self.mac, self._retry_after
            )
            raise

        return True

    async def disconnect(self):
        if self.client and self.client.is_connected:
            _LOGGER.debug("Disconnecting from %s...", self.mac)
            await self.client.disconnect()
            return True
        
        _LOGGER.debug("%s already disconnected.", self.mac)
        return False

    async def _cleanup_connection(self):
        """Tear down a half-established connection after a failed attempt.

        Cancels any pending disconnect task and force-disconnects the BLE
        client so a failed connect() never leaves the device connected.
        """
        client = self.client
        self.client = None

        if self._disconnect_task is not None:
            self._disconnect_task.cancel()
            self._disconnect_task = None

        if client is None:
            return

        _LOGGER.debug("Cleaning up failed connection to %s...", self.mac)
        try:
            await client.disconnect()
        except Exception as e:
            _LOGGER.debug("Error while cleaning up connection to %s: %s", self.mac, e)

    async def delayed_disconnect(self):
        async def _delayed_disconnect():
            if not self.client or not self.client.is_connected:
                _LOGGER.debug("%s already disconnected, skipping delayed disconnect.", self.mac)
                return

            try:
                _LOGGER.debug("Waiting %ss before disconnecting from %s...", DISCONNECT_DELAY, self.mac)
                await asyncio.sleep(DISCONNECT_DELAY)
                await self.disconnect()
            except Exception as e:
                _LOGGER.debug("Failed to disconnect. Error: %s", e)

        _LOGGER.debug("Scheduling delayed disconnect from %s...", self.mac)
        loop = asyncio.get_running_loop()
        if self._disconnect_task is not None:
            self._disconnect_task.cancel()
        self._disconnect_task = loop.create_task(_delayed_disconnect())

    async def _set_time(self):
        """Push the current time to the device before reading status.

        The official app sends this on every connect. Some devices (in
        particular ones that were never set up through the official app)
        do not emit a usable status frame until their clock has been set
        at least once. A failure here is logged but not fatal: the status
        read that follows is the real success criterion.
        """
        try:
            payload = encode_set_time()
        except Exception as e:  # pragma: no cover - defensive
            _LOGGER.warning("Could not build set-time message for %s: %s", self.mac, e)
            return

        _LOGGER.debug(">> %s: %s (set time)", WRITE_CHAR, payload.hex())
        try:
            await self.client.write_gatt_char(WRITE_CHAR, payload)
        except Exception as e:
            _LOGGER.debug("Failed to set time on %s: %s", self.mac, e)
            return

        # Match the app's pacing between messages.
        await asyncio.sleep(1.0)

    async def get_device_status(self, *, timeout: float = STATUS_TIMEOUT):
        _LOGGER.debug("Getting device status from %s...", self.mac)

        # Reset the event before waiting for new status
        self._device_status_event.clear()

        _LOGGER.debug("Writing to %s on %s...", WRITE_CHAR, self.mac)
        await self.client.write_gatt_char(WRITE_CHAR, FETCH_STATUS_MSG)

        try:
            _LOGGER.debug("Waiting for device status from %s...", self.mac)

            await asyncio.wait_for(
                self._device_status_event.wait(),
                timeout
            )
        except asyncio.TimeoutError as exc:
            _LOGGER.warning(
                "Timeout waiting for device status from %s after %ss",
                self.mac,
                timeout
            )
            raise ConnectionError(
                f"Timeout waiting for device status response from {self.mac}"
            ) from exc

        # Got a fresh status, clear any retry cooldown from earlier failures.
        self._retry_after = None

        await self.delayed_disconnect()

    async def _write_action(self, payload: bytes, *, refresh: bool = True):
        """Send a command that changes device state.

        Connects if needed, writes the message, then (by default) reads
        the status back so entity state reflects reality. The command has
        already been delivered by the time a status refresh fails, so that
        failure is logged, not raised.
        """
        await self._ensure_connected()

        _LOGGER.debug(">> %s: %s", WRITE_CHAR, payload.hex())
        await self.client.write_gatt_char(WRITE_CHAR, payload)
        _LOGGER.debug("Write complete.")

        if not refresh:
            await self.delayed_disconnect()
            return

        # Give the device a moment to apply the change before reading back.
        await asyncio.sleep(2.0)
        try:
            await self.get_device_status()
        except Exception as e:
            _LOGGER.warning(
                "Status refresh after action on %s failed: %s", self.mac, e
            )
            await self.delayed_disconnect()

    async def spray_now(self):
        """Spray once, now."""
        _LOGGER.debug("Spray now on %s...", self.mac)
        await self._write_action(SPRAY_NOW_MSG)

    async def set_spray_interval(self, interval: int):
        """Start auto-spraying every ``interval`` minutes (10, 20 or 40)."""
        if interval not in SPRAY_INTERVALS:
            raise ValueError(
                f"Unsupported spray interval {interval}; "
                f"expected one of {SPRAY_INTERVALS}"
            )
        _LOGGER.debug("Set spray interval %s min on %s...", interval, self.mac)
        await self._write_action(spray_now_msg(interval))

    async def stop_spray(self):
        """Stop auto-spraying."""
        _LOGGER.debug("Stop auto-spray on %s...", self.mac)
        await self._write_action(STOP_SPRAY_MSG)

    async def set_auto_spray(self, enabled: bool, interval: int | None = None):
        """Turn auto-spray on (at ``interval`` or the default) or off."""
        if enabled:
            await self.set_spray_interval(interval or DEFAULT_SPRAY_INTERVAL)
        else:
            await self.stop_spray()

    async def reset_refill(self):
        """Tell the device its cartridge has been refilled."""
        _LOGGER.debug("Reset refill on %s...", self.mac)
        await self._write_action(RESET_REFILL_MSG)

    async def _ensure_connected(self):
        """Make sure the BLE link is up, without requiring a status read.

        This is the path used for actuating the device (button press): it
        must succeed whenever the device is reachable, even if status
        notifications are currently not coming back, and it ignores the
        polling retry cooldown.
        """
        if self.client and self.client.is_connected:
            return

        async with self._connect_lock:
            _LOGGER.debug("Not connected, connecting to %s...", self.mac)
            await self._open_connection()

    async def _notification_handler(self, sender, data):
        uuid = getattr(sender, "uuid", sender)
        _LOGGER.debug("<< %s: %s", uuid, data.hex())

        # The status frame is a fixed 99 bytes. We subscribe to every notify
        # characteristic (see _subscribe_notifications), so match on the
        # frame shape rather than the source characteristic.
        if len(data) != STATUS_LEN:
            return

        status = DeviceStatus(bytes(data))
        if not status.is_valid:
            _LOGGER.debug("Ignoring malformed status frame from %s", self.mac)
            return

        self.device_status = status
        self._device_status_event.set()
        self.eventbus.send(DEVICE_STATUS_UPDATE, self.device_status)

    def _on_disconnect(self, _client: BleakClient):
        if self._disconnect_task is not None:
            self._disconnect_task.cancel()
            self._disconnect_task = None

        self.client = None
        self.eventbus.send(DEVICE_DISCONNECT, self)

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from bleak import BleakClient
from bleak_retry_connector import establish_connection

from homeassistant.components.bluetooth import (
    async_ble_device_from_address
)
from homeassistant.util import dt as dt_util

from . import cbor
from .const import (
    DISCONNECT_DELAY,
    STATUS_TIMEOUT,
    PROBE_TIMEOUT,
    DETECT_TIMEOUT,
    ACK_TIMEOUT,
    BATTERY_CUTOFF_MV,
    CONNECT_ATTEMPTS,
    CONNECT_TIMEOUT,
    CONNECT_RETRY_BACKOFF,
    MAIN_SVC,
    NOTIFY_CHAR,
    DEFAULT_COMMAND_CHAR,
    DEFAULT_REPLY_CHAR,
    COMMAND_CHAR_CANDIDATES,
    SPRAY_INTERVALS,
    DEFAULT_SPRAY_INTERVAL,
    DESIRED_MTU,
    ACTION_SETTLE,
)
from .eventbus import EventBus
from .devicestatus import ACK_OK, DeviceStatus
from .exception import (
    BatteryTooLow,
    CommandNotConfirmed,
    InvalidDeviceError,
    ConnectionError
)
from .events import (
    DEVICE_CONNECT,
    DEVICE_DISCONNECT,
    DEVICE_REACHABILITY,
    DEVICE_STATUS_UPDATE
)

_LOGGER = logging.getLogger(__name__)

# Limits for reassembling frames split over several notifications.
_MAX_FRAME_LEN = 512
_FRAGMENT_TIMEOUT = 2.0

# Replies to commands (request mT + 50), for logging.
_ACK_LABELS = {
    152: "Clock set",
    154: "Spray command",
    155: "Refill reset",
    156: "Auto-spray stop",
}
# Detection tags each probe with its own message number (mN), so a late
# reply can be matched to the probe it answers.
_PROBE_MN_BASE = 200


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
    def __init__(
        self,
        hass,
        mac,
        *,
        command_char: str | None = None,
        reply_char: str | None = None,
    ):
        """``command_char``/``reply_char`` are the characteristics stored at
        setup (see detect_channels()); without them the defaults are used."""
        self.hass = hass
        self.mac = mac
        self.client: BleakClient | None = None
        self.device_status: DeviceStatus | None = None
        self.eventbus = EventBus()

        self._connect_lock = asyncio.Lock()
        self._device_status_event = asyncio.Event()
        self._disconnect_task: asyncio.Task | None = None
        self._retry_after: datetime | None = None
        self._poll_failures = 0

        self._command_char: str = command_char or DEFAULT_COMMAND_CHAR
        self._reply_char: str = reply_char or DEFAULT_REPLY_CHAR
        self._failure_logged = False
        self._subscribed = False
        self._subscribe_lock = asyncio.Lock()
        self._last_reply_char: str | None = None
        # Detection only: walk the whole service, subscribe to everything.
        self._detecting = False
        self._subscribe_all = False
        # Reply mT -> future resolved with the ack value. Commands are sent
        # one at a time (``_command_lock``), so at most one waits per type.
        self._ack_waiters: dict[int, asyncio.Future] = {}
        self._command_lock = asyncio.Lock()
        # Loop time of the last failed connect, so queued commands give up
        # at once instead of each waiting out their own attempts.
        self._connect_failed_at: float | None = None
        self._refresh_task: asyncio.Task | None = None
        # Reachability: off only on real trouble, not on the normal
        # disconnect after each exchange.
        self.last_update: datetime | None = None
        self.reachable: bool | None = None
        self._advertising: bool | None = None
        self._last_exchange_ok: bool | None = None
        # uuid -> (partial frame, time of last fragment)
        self._rx_buffers: dict[str, tuple[bytearray, float]] = {}

    @property
    def command_char(self) -> str:
        return self._command_char

    @property
    def reply_char(self) -> str:
        return self._reply_char

    def set_advertising(self, seen: bool) -> None:
        """Report whether Home Assistant currently sees the device advertise."""
        self._advertising = seen
        self._update_reachable()

    def _set_exchange_ok(self, ok: bool) -> None:
        self._last_exchange_ok = ok
        if ok:
            self._advertising = True  # it answered, so it is in range
        self._update_reachable()

    def _update_reachable(self) -> None:
        if self._advertising is None and self._last_exchange_ok is None:
            value = None
        else:
            value = self._advertising is not False and self._last_exchange_ok is not False
        if value != self.reachable:
            self.reachable = value
            self.eventbus.send(DEVICE_REACHABILITY, self)

    @property
    def _write_char(self) -> str:
        return self._command_char

    async def connect(self, *, status_timeout: float = STATUS_TIMEOUT) -> bool:
        """Open the BLE link and fetch a fresh device status."""
        async with self._connect_lock:
            await self._open_connection()

            try:
                await self._set_time()
                await self.get_device_status(timeout=status_timeout)
            except BaseException:
                await self._cleanup_connection()
                raise

        return True

    async def _open_connection(
        self, *, subscribe: bool = True, settle: float = 2.0,
        max_attempts: int | None = None, connect_timeout: float | None = None,
        purpose: str = "status poll",
    ):
        """Establish the BLE link and (by default) subscribe to notifications.

        The caller must hold ``self._connect_lock``. On any failure after
        the link is up, the connection is torn down before the exception
        propagates, so a failed attempt never leaves the device connected
        (which would drain its battery).
        """
        _LOGGER.debug("Trying to connect to device %s (%s)...", self.mac, purpose)

        if self.client and self.client.is_connected:
            _LOGGER.debug("Already connected to %s", self.mac)
            if subscribe:
                await self._ensure_subscribed()
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
            # bleak-retry-connector allows 20 s per attempt; a working
            # device connects in about 1 s, so give up much sooner.
            async with asyncio.timeout(connect_timeout or CONNECT_TIMEOUT):
                client = await establish_connection(
                    BleakClient,
                    device,
                    self.mac,
                    disconnected_callback=self._on_disconnect,
                    max_attempts=max_attempts or CONNECT_ATTEMPTS,
                )
        except Exception as e:
            self._connect_failed_at = asyncio.get_running_loop().time()
            if isinstance(e, TimeoutError):
                reason = f"timed out after {connect_timeout or CONNECT_TIMEOUT} s"
            else:
                reason = str(e) or type(e).__name__
            _LOGGER.debug("Failed to connect to %s (%s): %s", self.mac, purpose, reason)
            raise ConnectionError(f"Failed to connect to device: {reason}") from e

        self.client = client
        self._subscribed = False

        # From here on the BLE link is open. Any failure must disconnect
        # it, otherwise we stay connected indefinitely and drain the
        # device battery.
        try:
            await asyncio.sleep(settle)  # let the device settle after connecting

            if not client.is_connected:
                raise ConnectionError(
                    f"Device {self.mac} disconnected during service discovery"
                )

            _LOGGER.debug("Connected to %s", self.mac)

            services = client.services
            # Detection walks the table anyway: list it, for troubleshooting.
            if self._detecting and _LOGGER.isEnabledFor(logging.DEBUG):
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

            if subscribe:
                await self._ensure_subscribed()
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

        Normally subscribe to just the reply characteristic, looked up
        directly. When detection falls back to probing, subscribe to all of
        them and let ``_notification_handler`` pick the status frame out of
        whichever one actually delivers it.
        """
        if not self._subscribe_all:
            char = main_svc.get_characteristic(self._reply_char)
            notify_chars = [char] if char is not None else []
        else:
            notify_chars = [
                char for char in main_svc.characteristics
                if "notify" in char.properties or "indicate" in char.properties
            ]
            # Try the conventional Nordic UART TX characteristic first.
            notify_chars.sort(key=lambda c: c.uuid.lower() != NOTIFY_CHAR.lower())

        self._rx_buffers.clear()
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
                if await self._raw_proxy_notify(client, char):
                    subscribed += 1

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

    async def _ensure_subscribed(self):
        """Subscribe to notifications once per connection."""
        async with self._subscribe_lock:
            client = self.client
            if self._subscribed or client is None:
                return
            await self._subscribe_notifications(
                client, client.services.get_service(MAIN_SVC)
            )
            await self._request_mtu()
            self._subscribed = True

    async def _raw_proxy_notify(self, client, char) -> bool:
        """Subscribe via an ESPHome proxy without writing a CCCD.

        bleak-esphome's start_notify fails on characteristics without a
        CCCD (6e400001). Registering in ``_notify_cancels`` lets it clean
        up on disconnect. Uses bleak-esphome internals; returns False if
        they don't match.
        """
        backend = getattr(client, "_backend", None)
        api = getattr(backend, "_client", None)
        address = getattr(backend, "_address_as_int", None)
        cancels = getattr(backend, "_notify_cancels", None)
        if (
            api is None
            or address is None
            or not isinstance(cancels, dict)
            or not hasattr(api, "bluetooth_gatt_start_notify")
        ):
            _LOGGER.debug(
                "No ESPHome proxy backend for %s (%s), cannot raw-subscribe %s",
                self.mac, type(backend).__name__, char.uuid
            )
            return False
        if char.handle in cancels:
            return True

        uuid = char.uuid
        try:
            cancels[char.handle] = await api.bluetooth_gatt_start_notify(
                address,
                char.handle,
                lambda _handle, data: self._on_notification(uuid, data),
            )
        except Exception as e:
            _LOGGER.debug("Raw proxy notify on %s for %s failed: %s", uuid, self.mac, e)
            return False

        _LOGGER.debug("Raw proxy notify registered on %s for %s", uuid, self.mac)
        return True

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

    @property
    def retry_delay(self) -> float | None:
        """Seconds until the next background attempt is allowed, if backing off."""
        if self._retry_after is None:
            return None
        return max(0.0, (self._retry_after - datetime.now()).total_seconds())

    async def connect_if_needed(self) -> bool:
        # A recent status is enough, unless the last exchange (e.g. a press)
        # failed: then check again so Connection status can recover.
        if (
            self.device_status
            and self.device_status.is_valid
            and self._last_exchange_ok is not False
        ):
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
        except Exception as e:
            self._set_exchange_ok(False)
            backoff = CONNECT_RETRY_BACKOFF[
                min(self._poll_failures, len(CONNECT_RETRY_BACKOFF) - 1)
            ]
            self._poll_failures += 1
            self._retry_after = datetime.now() + timedelta(seconds=backoff)
            # Warn only on the first failure in a row.
            log = _LOGGER.debug if self._failure_logged else _LOGGER.warning
            log(
                "Could not get status from %s (%s); retrying no sooner than %s",
                self.mac, e, self._retry_after.strftime("%H:%M:%S")
            )
            self._failure_logged = True
            raise

        if self._failure_logged:
            _LOGGER.info("Status from %s is available again", self.mac)
            self._failure_logged = False
        return True

    async def detect_channels(
        self,
        *,
        timeout: float = DETECT_TIMEOUT,
        progress: Callable[[str, dict, float], None] | None = None,
    ) -> tuple[str, str] | None:
        """Find the command and reply characteristics on a fresh link.

        Checks the defaults first, and only if they don't answer probes
        every writable characteristic with every notify one subscribed.
        Returns None if the device is reachable but never answers. The
        channels this instance uses are left unchanged. ``progress`` is
        called with (stage, details, fraction done) as detection proceeds.
        """
        report = progress or (lambda stage, details, fraction: None)
        report("connecting", {}, 0.0)
        async with self._connect_lock:
            if self._refresh_task is not None:
                self._refresh_task.cancel()
                self._refresh_task = None
            saved = self._command_char, self._reply_char
            await self._cleanup_connection()
            self._command_char, self._reply_char = DEFAULT_COMMAND_CHAR, DEFAULT_REPLY_CHAR
            self._last_reply_char = None
            self._detecting = True
            sent: dict[int, str] = {}
            try:
                await self._open_connection(purpose="setup check")
                loop = asyncio.get_running_loop()
                deadline = loop.time() + timeout

                report("checking_defaults", {}, 0.0)
                # Exactly the official app's request.
                command = await self._probe(
                    DEFAULT_COMMAND_CHAR, PROBE_TIMEOUT, sent, numbered=False
                )
                if command is None:
                    _LOGGER.info(
                        "Default channels of %s did not answer; probing", self.mac
                    )
                    self._subscribe_all = True
                    self._subscribed = False
                    await self._ensure_subscribed()
                    try:
                        command = await self._detect_command_char(
                            timeout=max(0.0, deadline - loop.time()),
                            progress=report,
                            sent=sent,
                        )
                    except ConnectionError as e:
                        _LOGGER.warning("Could not detect the command channel: %s", e)
                        return None

                _LOGGER.info(
                    "Command channel for %s is %s, replies on %s (firmware %s)",
                    self.mac, command, self._last_reply_char,
                    self.device_status.firmware_version if self.device_status else "?"
                )
                await self._set_time(command)
                return command, self._last_reply_char
            finally:
                self._command_char, self._reply_char = saved
                self._detecting = self._subscribe_all = False
                await self._cleanup_connection()

    async def disconnect(self):
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            self._refresh_task = None
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

        _LOGGER.debug("Closing connection to %s...", self.mac)
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

    async def _set_time(self, char: str | None = None):
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

        char = char or self._write_char
        _LOGGER.debug(">> %s: %s (set time)", char, payload.hex())
        try:
            await self.client.write_gatt_char(char, payload)
        except Exception as e:
            _LOGGER.debug("Failed to set time on %s: %s", self.mac, e)
            return

        # Match the app's pacing between messages.
        await asyncio.sleep(1.0)

    async def get_device_status(self, *, timeout: float = STATUS_TIMEOUT):
        _LOGGER.debug("Getting device status from %s...", self.mac)

        if not await self._request_status(self._write_char, timeout):
            raise ConnectionError(
                f"Timeout waiting for device status response from {self.mac}"
            )

        # Got a fresh status, clear any retry cooldown from earlier failures.
        self._retry_after = None
        self._poll_failures = 0

        await self.delayed_disconnect()

    async def _request_status(
        self, char: str, timeout: float, message: bytes = FETCH_STATUS_MSG
    ) -> bool:
        """Send fetch-status to ``char``; True if a status reply arrived."""
        self._device_status_event.clear()

        _LOGGER.debug(">> %s: %s (fetch status)", char, message.hex())
        try:
            await self.client.write_gatt_char(char, message)
        except Exception as e:
            _LOGGER.debug("Write to %s on %s failed: %s", char, self.mac, e)
            return False

        _LOGGER.debug("Waiting for device status from %s...", self.mac)
        try:
            await asyncio.wait_for(self._device_status_event.wait(), timeout)
        except asyncio.TimeoutError:
            return False
        return True

    async def _probe(
        self, char: str, timeout: float, sent: dict[int, str], *, numbered: bool = True
    ) -> str | None:
        """Probe ``char`` with a status request.

        Returns the characteristic the reply belongs to: usually ``char``,
        but an earlier probe's if a late reply carries its number (mN) back.
        If the device doesn't echo numbers, the reply is credited to
        ``char``. ``numbered=False`` sends the app's own request (mN 157).
        """
        # Only numbered probes are recorded: 157 is also what a device that
        # doesn't echo mN puts in every reply.
        if numbered:
            mn = _PROBE_MN_BASE + len(sent)
            sent[mn] = char
            message = encode_message(mT=107, mN=mn)
        else:
            mn, message = 157, FETCH_STATUS_MSG
        if not await self._request_status(char, timeout, message):
            return None
        reply_mn = self.device_status.message_id if self.device_status else None
        _LOGGER.debug(
            "Status reply to probe of %s: sent mN=%s, reply mN=%s", char, mn, reply_mn
        )
        if reply_mn in sent and sent[reply_mn] != char:
            _LOGGER.debug("Late reply: it answers the probe of %s", sent[reply_mn])
            return sent[reply_mn]
        return char

    def _command_char_candidates(self) -> list[str]:
        """Every writable characteristic in the main service, known ones first."""
        main_svc = self.client.services.get_service(MAIN_SVC) if self.client else None
        if main_svc is None:
            return list(COMMAND_CHAR_CANDIDATES)

        writable = [
            char.uuid.lower() for char in main_svc.characteristics
            if "write" in char.properties or "write-without-response" in char.properties
        ]
        known = [uuid for uuid in COMMAND_CHAR_CANDIDATES if uuid in writable]
        return known + [uuid for uuid in writable if uuid not in known]

    async def _detect_command_char(
        self,
        *,
        timeout: float = DETECT_TIMEOUT,
        set_time_passes: tuple[bool, ...] = (False, True),
        progress: Callable[[str, dict, float], None] = lambda *_: None,
        sent: dict[int, str] | None = None,
    ):
        """Send fetch-status to each candidate; keep the one that answers.

        A second pass sets the clock first, for units that stay silent
        until it is set.
        """
        candidates = self._command_char_candidates()
        sent = {} if sent is None else sent
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout

        attempts = len(candidates) * len(set_time_passes)
        attempt = 0
        for set_time_first in set_time_passes:
            for index, char in enumerate(candidates, start=1):
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                progress(
                    "probing_again" if set_time_first else "probing",
                    {"index": str(index), "total": str(len(candidates)), "channel": char[:8]},
                    attempt / attempts,
                )
                attempt += 1
                _LOGGER.debug(
                    "Probing %s as command channel for %s%s...",
                    char, self.mac, " (after set time)" if set_time_first else ""
                )
                if set_time_first:
                    await self._set_time(char)
                answered = await self._probe(char, min(PROBE_TIMEOUT, remaining), sent)
                if answered is not None:
                    return answered

        raise ConnectionError(
            f"No status reply from {self.mac} on any of {', '.join(candidates)}"
        )

    async def _write_action(self, payload: bytes, reply_mt: int):
        """Send a command and wait for the device to acknowledge it.

        Subscribes first (one quick call) so the acknowledgement, a reply of
        type ``reply_mt``, is caught. Raises CommandNotConfirmed if it
        doesn't arrive in time or isn't ack 200. Either way the status is
        read back afterwards, so the entities show what actually happened.

        Commands are sent one at a time: acks only carry the reply type,
        and Spray and Auto Spray share one, so overlapping commands could
        not be told apart. Each waits for its ack from its own write.
        """
        queued_at = asyncio.get_running_loop().time()
        async with self._command_lock:
            failed_at = self._connect_failed_at
            if failed_at is not None and failed_at >= queued_at:
                raise ConnectionError(
                    f"{self.mac} could not be reached for an earlier command"
                )
            await self._send_command(payload, reply_mt)

    async def _send_command(self, payload: bytes, reply_mt: int):
        try:
            await self._ensure_connected()
            await self._ensure_subscribed()
        except Exception:
            self._set_exchange_ok(False)
            raise

        # A read-back from an earlier press is superseded by this one's.
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            self._refresh_task = None

        label = _ACK_LABELS.get(reply_mt, f"Command {reply_mt - 50}")
        ack = None
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._ack_waiters[reply_mt] = future
        try:
            char = self._write_char
            _LOGGER.debug(">> %s: %s", char, payload.hex())
            await self.client.write_gatt_char(char, payload)
            sent_at = loop.time()
            ack = await asyncio.wait_for(future, ACK_TIMEOUT)
            _LOGGER.debug("%s answered in %.2f s", label, loop.time() - sent_at)
        except asyncio.TimeoutError:
            _LOGGER.warning(
                "%s was not acknowledged by %s within %s s",
                label, self.mac, ACK_TIMEOUT,
            )
        except Exception as e:
            _LOGGER.warning("%s could not be sent to %s: %s", label, self.mac, e)
        finally:
            if self._ack_waiters.get(reply_mt) is future:
                del self._ack_waiters[reply_mt]

        self._schedule_refresh()
        if ack != ACK_OK:
            raise CommandNotConfirmed(f"{label} not confirmed by {self.mac} (ack={ack})")

    def _schedule_refresh(self):
        """Read the status back in the background after an action.

        The press returns once the command is acknowledged (or not);
        presses in quick succession share one read-back.
        """
        if self._refresh_task is not None:
            self._refresh_task.cancel()

        async def _refresh():
            try:
                await self._ensure_connected()
                await self._ensure_subscribed()
                await self.get_device_status()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._set_exchange_ok(False)
                _LOGGER.warning(
                    "Status refresh after action on %s failed: %s", self.mac, e
                )
                await self.delayed_disconnect()

        self._refresh_task = asyncio.get_running_loop().create_task(_refresh())

    async def spray_now(self):
        """Spray once, now."""
        _LOGGER.debug("Spray now on %s...", self.mac)
        await self._write_action(SPRAY_NOW_MSG, 154)
        self._raise_if_battery_too_low()

    async def set_spray_interval(self, interval: int):
        """Start auto-spraying every ``interval`` minutes (10, 20 or 40)."""
        if interval not in SPRAY_INTERVALS:
            raise ValueError(
                f"Unsupported spray interval {interval}; "
                f"expected one of {SPRAY_INTERVALS}"
            )
        _LOGGER.debug("Set spray interval %s min on %s...", interval, self.mac)
        await self._write_action(spray_now_msg(interval), 154)
        self._raise_if_battery_too_low()

    def _raise_if_battery_too_low(self):
        """Below the cut-off the device acknowledges sprays but doesn't
        spray; judged from the latest status read."""
        status = self.device_status
        mv = status.battery_mv if status else None
        if mv is not None and mv < BATTERY_CUTOFF_MV:
            _LOGGER.warning(
                "Batteries of %s are at %s mV, below the %s mV the motor needs",
                self.mac, mv, BATTERY_CUTOFF_MV,
            )
            raise BatteryTooLow(f"Batteries of {self.mac} at {mv} mV")

    async def stop_spray(self):
        """Stop auto-spraying."""
        _LOGGER.debug("Stop auto-spray on %s...", self.mac)
        await self._write_action(STOP_SPRAY_MSG, 156)

    async def set_auto_spray(self, enabled: bool, interval: int | None = None):
        """Turn auto-spray on (at ``interval`` or the default) or off."""
        if enabled:
            await self.set_spray_interval(interval or DEFAULT_SPRAY_INTERVAL)
        else:
            await self.stop_spray()

    async def reset_refill(self):
        """Tell the device its cartridge has been refilled."""
        _LOGGER.debug("Reset refill on %s...", self.mac)
        await self._write_action(RESET_REFILL_MSG, 155)

    async def _ensure_connected(self):
        """Make sure the BLE link is up, without requiring a status read.

        This is the path used for actuating the device (button press): it
        must succeed whenever the device is reachable, even if status
        notifications are currently not coming back, and it ignores the
        polling retry cooldown.
        """
        # Don't let a disconnect timer from the last exchange drop the link
        # mid-command; the action schedules a new one.
        if self._disconnect_task is not None:
            self._disconnect_task.cancel()
            self._disconnect_task = None

        if self.client and self.client.is_connected:
            return

        waiting_since = asyncio.get_running_loop().time()
        async with self._connect_lock:
            # A poll may have been connecting while this waited: if it
            # failed, so would this, so don't make the user wait twice.
            failed_at = self._connect_failed_at
            if failed_at is not None and failed_at >= waiting_since:
                raise ConnectionError(f"{self.mac} could not be reached just now")
            _LOGGER.debug("Not connected, connecting to %s...", self.mac)
            # Subscribing first (one quick call) lets the action catch the
            # device's acknowledgement.
            await self._open_connection(settle=ACTION_SETTLE, purpose="command")

    async def _notification_handler(self, sender, data):
        self._on_notification(getattr(sender, "uuid", str(sender)), data)

    def _on_notification(self, uuid: str, data: bytes):
        """Buffer fragments per characteristic until a CBOR item is complete."""
        _LOGGER.debug("<< %s (%d): %s", uuid, len(data), bytes(data).hex())

        now = time.monotonic()
        buf, last = self._rx_buffers.get(uuid, (bytearray(), now))
        if buf and now - last > _FRAGMENT_TIMEOUT:
            _LOGGER.debug("Dropping stale partial frame on %s: %s", uuid, buf.hex())
            buf = bytearray()
        buf.extend(data)

        try:
            _, end = cbor.decode(bytes(buf))
        except cbor.CBORIncompleteError:
            if len(buf) > _MAX_FRAME_LEN:
                _LOGGER.debug("Dropping oversized partial frame on %s", uuid)
                self._rx_buffers.pop(uuid, None)
            else:
                self._rx_buffers[uuid] = (buf, now)
            return
        except cbor.CBORError:
            # Not CBOR; DeviceStatus still tries the legacy layout.
            end = len(buf)

        self._rx_buffers.pop(uuid, None)
        self._handle_message(bytes(buf[:end]), uuid)

    def _handle_message(self, frame: bytes, uuid: str | None = None):
        status = DeviceStatus(frame)
        if not status.is_valid:
            self._handle_ack(status)
            return

        if uuid is not None:
            self._last_reply_char = uuid

        self.device_status = status
        self.last_update = dt_util.utcnow()
        self._set_exchange_ok(True)
        self._device_status_event.set()
        self.eventbus.send(DEVICE_STATUS_UPDATE, self.device_status)

    def _handle_ack(self, message: DeviceStatus) -> None:
        """Log a command acknowledgement and hand it to a waiting action."""
        reply_mt, ack = message.message_type, message.acknowledgement
        label = _ACK_LABELS.get(reply_mt)
        if label is None:
            _LOGGER.debug("Ignoring message type %s from %s", reply_mt, self.mac)
            return
        if ack == ACK_OK:
            _LOGGER.debug("%s acknowledged by %s", label, self.mac)
        else:
            _LOGGER.warning("%s rejected by %s (ack=%s)", label, self.mac, ack)
        future = self._ack_waiters.get(reply_mt)
        if future is not None and not future.done():
            future.set_result(ack)
        else:
            _LOGGER.debug("%s: nothing was waiting (it came late)", label)

    def _on_disconnect(self, _client: BleakClient):
        if self._disconnect_task is not None:
            self._disconnect_task.cancel()
            self._disconnect_task = None

        self.client = None
        self.eventbus.send(DEVICE_DISCONNECT, self)

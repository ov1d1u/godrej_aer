"""Config flow, reconfigure, entry setup and sensors against Home Assistant."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.godrej_aer.const import CONF_COMMAND_CHAR, CONF_REPLY_CHAR, DOMAIN
from custom_components.godrej_aer.godrej.exception import (
    ConnectionError as DeviceConnectionError,
    InvalidDeviceError,
)

MAC = "A4:C1:38:CB:03:37"
C1 = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
C3 = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
C4 = "6e400004-b5a3-f393-e0a9-e50e24dcca9e"
CF = "custom_components.godrej_aer.config_flow"
DETECT = f"{CF}.SmartMatic.detect_channels"
UNAVAILABLE = []


@pytest.fixture(autouse=True)
def no_deps():
    # Skip setting up the bluetooth_adapters dependency in the test hass.
    with patch("homeassistant.setup.async_process_deps_reqs", AsyncMock()), \
         patch("homeassistant.config_entries.async_process_deps_reqs", AsyncMock(), create=True):
        yield


def discovered():
    return [SimpleNamespace(address=MAC, name="Smart Matic")]


async def start_flow(hass):
    with patch(f"{CF}.async_discovered_service_info", return_value=discovered()):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    return result


async def pick_device(hass, result, detect):
    with patch(DETECT, detect), \
         patch(f"{CF}.SmartMatic.disconnect", AsyncMock()), \
         patch(f"{CF}.async_discovered_service_info", return_value=discovered()):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"mac": MAC}
        )
        result, _ = await finish_progress(hass, result)
        return result


async def finish_progress(hass, result):
    """Let detection run and follow the flow until it leaves the progress steps."""
    seen = []
    while result["type"] in (FlowResultType.SHOW_PROGRESS, FlowResultType.SHOW_PROGRESS_DONE):
        if result["type"] is FlowResultType.SHOW_PROGRESS:
            seen.append((result["progress_action"], dict(result.get("description_placeholders") or {})))
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
    return result, seen


async def test_setup_stores_detected_channels(hass):
    result = await start_flow(hass)
    detect = AsyncMock(return_value=(C4, C1))
    result = await pick_device(hass, result, detect)
    detect.assert_awaited_once()
    assert result["step_id"] == "name"
    assert result["description_placeholders"] == {"command": "6e400004", "reply": "6e400001"}

    with patch("custom_components.godrej_aer.async_setup_entry", AsyncMock(return_value=True)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"name": "Hall"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        "mac": MAC, "name": "Hall", CONF_COMMAND_CHAR: C4, CONF_REPLY_CHAR: C1,
    }


@pytest.mark.parametrize(
    ("detect", "error"),
    [
        (AsyncMock(return_value=None), "no_reply"),
        (AsyncMock(side_effect=DeviceConnectionError("x")), "cannot_connect"),
        (AsyncMock(side_effect=InvalidDeviceError("x")), "invalid_device"),
    ],
)
async def test_setup_without_detection_creates_nothing(hass, detect, error):
    result = await start_flow(hass)
    result = await pick_device(hass, result, detect)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"  # back to the device list, to retry
    assert result["errors"] == {"base": error}
    assert hass.config_entries.async_entries(DOMAIN) == []


def make_entry(hass, command_char=C4, reply_char=C1):
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=MAC.lower(),
        data={
            "mac": MAC, "name": "Hall",
            CONF_COMMAND_CHAR: command_char, CONF_REPLY_CHAR: reply_char,
        },
    )
    entry.add_to_hass(hass)
    return entry


async def start_reconfigure(hass, entry):
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "reconfigure"
    return result


async def test_reconfigure_reports_progress_and_detected_channels(hass):
    entry = make_entry(hass)
    result = await start_reconfigure(hass, entry)
    flow_id = result["flow_id"]
    probed, finish = asyncio.Event(), asyncio.Event()

    async def detect(self, *, progress=None, **kwargs):
        progress("connecting", {}, 0.0)
        await probed.wait()
        progress("probing", {"index": "2", "total": "3", "channel": "6e400003"}, 0.33)
        await finish.wait()
        return (C3, C1)

    async def settle():
        # Not async_block_till_done: it would wait for the gated detection.
        for _ in range(20):
            await asyncio.sleep(0)

    def shown():
        """What the dialog currently shows (HA advances the flow by itself)."""
        step = hass.config_entries.flow._progress[flow_id].cur_step
        return step["progress_action"], dict(step.get("description_placeholders") or {})

    with patch(DETECT, detect), \
         patch.object(hass.config_entries, "async_schedule_reload") as reload:
        result = await hass.config_entries.flow.async_configure(flow_id, {})
        assert result["type"] is FlowResultType.SHOW_PROGRESS
        await settle()
        assert shown() == ("connecting", {})

        probed.set()
        await settle()
        assert shown() == ("probing", {"index": "2", "total": "3", "channel": "6e400003"})

        finish.set()
        await hass.async_block_till_done()
        # The frontend continues once the progress is done.
        result = await hass.config_entries.flow.async_configure(flow_id)
    assert result["reason"] == "channels_detected"
    assert entry.data[CONF_COMMAND_CHAR] == C3
    assert entry.data[CONF_REPLY_CHAR] == C1
    reload.assert_called_once_with(entry.entry_id)


async def test_reconfigure_finishes_with_the_detected_channels(hass):
    entry = make_entry(hass)
    result = await start_reconfigure(hass, entry)
    with patch(DETECT, AsyncMock(return_value=(C3, C1))), \
         patch.object(hass.config_entries, "async_schedule_reload"):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        result, _ = await finish_progress(hass, result)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "channels_detected"
    assert result["description_placeholders"] == {"command": "6e400003", "reply": "6e400001"}


async def test_reconfigure_without_reply_changes_nothing(hass):
    entry = make_entry(hass)
    result = await start_reconfigure(hass, entry)
    with patch(DETECT, AsyncMock(return_value=None)), \
         patch.object(hass.config_entries, "async_schedule_reload") as reload:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        result, _ = await finish_progress(hass, result)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {"base": "no_reply"}
    assert entry.data[CONF_COMMAND_CHAR] == C4
    reload.assert_not_called()


async def test_reconfigure_form_is_plain(hass):
    entry = make_entry(hass, None, None)
    result = await start_reconfigure(hass, entry)
    assert not result.get("description_placeholders")
    assert result["errors"] == {}


async def test_reconfigure_uses_the_loaded_instance(hass):
    """The device takes a single connection, so probe through the running one."""
    from custom_components.godrej_aer.godrej.godrej import SmartMatic

    entry = make_entry(hass)
    running = SmartMatic(hass, MAC, command_char=C4, reply_char=C1)
    entry.runtime_data = running
    seen = []

    async def detect(self, **kwargs):
        seen.append(self)
        return (C4, C1)

    result = await start_reconfigure(hass, entry)
    with patch.object(SmartMatic, "detect_channels", detect), \
         patch.object(hass.config_entries, "async_schedule_reload"):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await finish_progress(hass, result)
    assert seen == [running]


DISABLED_BY_DEFAULT = (
    ("sensor", "command_channel"), ("sensor", "reply_channel"), ("sensor", "battery_voltage"),
)


def enable_diagnostics(hass, entry):
    """Pre-register the entities that are disabled by default, enabled."""
    from homeassistant.helpers import entity_registry as er

    ent_reg = er.async_get(hass)
    for platform, key in DISABLED_BY_DEFAULT:
        ent_reg.async_get_or_create(
            platform, DOMAIN, f"{entry.entry_id}_{key}", config_entry=entry,
            suggested_object_id={
                "command_channel": "hall_channel_command",
                "reply_channel": "hall_channel_reply",
                "battery_voltage": "hall_battery_voltage",
            }[key],
            disabled_by=None,
        )


async def setup_entry(hass, entry, service_info=None, connect=None):
    from custom_components.godrej_aer.godrej.godrej import SmartMatic

    patches = [
        patch("homeassistant.components.bluetooth.async_address_present", return_value=True),
        patch("homeassistant.components.bluetooth.async_register_callback", return_value=lambda: None),
        patch("homeassistant.components.bluetooth.async_track_unavailable",
              side_effect=lambda hass, cb, mac, connectable=True: UNAVAILABLE.append(cb) or (lambda: None)),
        patch("homeassistant.components.bluetooth.async_last_service_info", return_value=service_info),
        patch("homeassistant.components.bluetooth.async_scanner_by_source",
              return_value=SimpleNamespace(name="esp32-bluetooth-proxy")),
        patch.object(SmartMatic, "connect_if_needed", connect or AsyncMock(return_value=True)),
        patch.object(SmartMatic, "disconnect", AsyncMock()),
    ]
    for p in patches:
        p.start()
    try:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    finally:
        for p in patches:
            p.stop()


async def test_entry_setup_uses_saved_channels_and_shows_diagnostics(hass):
    entry = make_entry(hass)
    enable_diagnostics(hass, entry)
    info = SimpleNamespace(rssi=-61, source="80:F3:DA:41:58:DB")
    await setup_entry(hass, entry, info)

    instance = entry.runtime_data
    assert (instance.command_char, instance.reply_char) == (C4, C1)

    command = hass.states.get("sensor.hall_channel_command")
    reply = hass.states.get("sensor.hall_channel_reply")
    signal = hass.states.get("sensor.hall_signal_strength")
    assert command.attributes["friendly_name"] == "Hall Channel: command"
    assert command.state == "6e400004" and command.attributes["uuid"] == C4
    assert reply.state == "6e400001"
    assert signal.state == "-61"
    assert signal.attributes["source"] == "80:F3:DA:41:58:DB"
    assert signal.attributes["source_name"] == "esp32-bluetooth-proxy"
    assert signal.attributes["unit_of_measurement"] == "dBm"


async def test_legacy_entry_uses_the_default_channels(hass):
    entry = make_entry(hass, None, None)
    enable_diagnostics(hass, entry)
    await setup_entry(hass, entry)
    assert hass.states.get("sensor.hall_channel_command").state == "6e400002"
    assert hass.states.get("sensor.hall_channel_reply").state == "6e400001"
    assert hass.states.get("sensor.hall_signal_strength").state == "unknown"


async def test_diagnostic_sensors_are_disabled_by_default(hass):
    from homeassistant.helpers import entity_registry as er

    entry = make_entry(hass)
    await setup_entry(hass, entry)
    ent_reg = er.async_get(hass)
    for platform, key in DISABLED_BY_DEFAULT:
        entity_id = ent_reg.async_get_entity_id(platform, DOMAIN, f"{entry.entry_id}_{key}")
        assert ent_reg.async_get(entity_id).disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert hass.states.get(entity_id) is None
    assert hass.states.get("sensor.hall_battery") is not None
    assert hass.states.get("sensor.hall_signal_strength") is not None


async def call(hass, domain, service, entity_id, **data):
    await hass.services.async_call(
        domain, service, {"entity_id": entity_id, **data}, blocking=True
    )


async def expect_unconfirmed(hass, domain, service, entity_id, key="command_not_confirmed", **data):
    from homeassistant.exceptions import HomeAssistantError

    with pytest.raises(HomeAssistantError) as err:
        await call(hass, domain, service, entity_id, **data)
    assert err.value.translation_domain == DOMAIN
    assert err.value.translation_key == key


@pytest.mark.parametrize("entity_id, method", [
    ("button.hall_spray_now", "spray_now"),
    ("button.hall_reset_refill_level", "reset_refill"),
])
async def test_unconfirmed_button_press_shows_an_error(hass, entity_id, method):
    from custom_components.godrej_aer.godrej.exception import CommandNotConfirmed

    entry = make_entry(hass)
    await setup_entry(hass, entry)
    instance = entry.runtime_data

    with patch.object(instance, method, AsyncMock(side_effect=CommandNotConfirmed("x"))):
        await expect_unconfirmed(hass, "button", "press", entity_id)
    with patch.object(instance, method, AsyncMock()) as ok:
        await call(hass, "button", "press", entity_id)
    ok.assert_awaited_once()


async def test_auto_spray_switch_follows_the_acknowledgement(hass):
    from custom_components.godrej_aer.godrej.exception import CommandNotConfirmed

    entry = make_entry(hass)
    await setup_entry(hass, entry)
    instance = entry.runtime_data
    switch = "switch.hall_spray_auto"
    assert hass.states.get(switch).state == "off"

    # Not acknowledged: stays off (the frontend's optimistic toggle reverts).
    with patch.object(instance, "set_spray_interval", AsyncMock(side_effect=CommandNotConfirmed("x"))):
        await expect_unconfirmed(hass, "switch", "turn_on", switch)
    assert hass.states.get(switch).state == "off"
    # Unreachable: same.
    with patch.object(instance, "set_spray_interval", AsyncMock(side_effect=DeviceConnectionError("x"))):
        await expect_unconfirmed(hass, "switch", "turn_on", switch, key="cannot_reach")
    assert hass.states.get(switch).state == "off"

    # Acknowledged: on straight away, before the status read-back.
    with patch.object(instance, "set_spray_interval", AsyncMock()):
        await call(hass, "switch", "turn_on", switch)
    assert hass.states.get(switch).state == "on"

    with patch.object(instance, "stop_spray", AsyncMock(side_effect=CommandNotConfirmed("x"))):
        await expect_unconfirmed(hass, "switch", "turn_off", switch)
    assert hass.states.get(switch).state == "on"
    with patch.object(instance, "stop_spray", AsyncMock()):
        await call(hass, "switch", "turn_off", switch)
    assert hass.states.get(switch).state == "off"


async def test_spray_interval_follows_the_acknowledgement(hass):
    from custom_components.godrej_aer.godrej.exception import CommandNotConfirmed

    entry = make_entry(hass)
    await setup_entry(hass, entry)
    instance = entry.runtime_data
    select = "select.hall_spray_interval"
    before = hass.states.get(select).state

    with patch.object(instance, "set_spray_interval", AsyncMock(side_effect=CommandNotConfirmed("x"))):
        await expect_unconfirmed(hass, "select", "select_option", select, option="20 minutes")
    assert hass.states.get(select).state == before

    with patch.object(instance, "set_spray_interval", AsyncMock()) as ok:
        await call(hass, "select", "select_option", select, option="20 minutes")
    ok.assert_awaited_once_with(20)
    assert hass.states.get(select).state == "20 minutes"


async def test_signal_strength_updates_are_throttled(hass):
    from custom_components.godrej_aer.sensor import SmartMaticSignalStrengthSensor

    entry = make_entry(hass)
    await setup_entry(hass, entry, SimpleNamespace(rssi=-61, source="proxy"))
    sensor = next(
        e for e in hass.data["entity_components"]["sensor"].entities
        if isinstance(e, SmartMaticSignalStrengthSensor)
    )
    with patch("homeassistant.components.bluetooth.async_scanner_by_source", return_value=None):
        sensor._on_advertisement(SimpleNamespace(rssi=-63, source="proxy"), None)
        assert hass.states.get("sensor.hall_signal_strength").state == "-61"  # small change
        sensor._on_advertisement(SimpleNamespace(rssi=-70, source="proxy"), None)
        assert hass.states.get("sensor.hall_signal_strength").state == "-70"  # big change
        sensor._on_advertisement(SimpleNamespace(rssi=-71, source="other"), None)
        assert hass.states.get("sensor.hall_signal_strength").state == "-71"  # new source


async def test_health_entities(hass):
    from homeassistant.helpers import entity_registry as er

    UNAVAILABLE.clear()
    entry = make_entry(hass)
    ent_reg = er.async_get(hass)
    await setup_entry(hass, entry)

    instance = entry.runtime_data
    status_id = "binary_sensor.hall_connection_status"
    status = hass.states.get(status_id)
    assert status.state == "unknown"
    assert status.attributes["friendly_name"] == "Hall Connection status"
    assert ent_reg.async_get(status_id).translation_key == "status"

    # A status reply arrives: OK, with the time as an attribute.
    from test_devicestatus_frame import FRAME
    instance._on_notification(C1, FRAME)
    await hass.async_block_till_done()
    status = hass.states.get(status_id)
    assert status.state == "on"
    assert status.attributes["last_update"] is not None

    # The routine disconnect after an exchange changes nothing.
    instance._on_disconnect(None)
    await hass.async_block_till_done()
    assert hass.states.get(status_id).state == "on"

    # No advertisements for a while: Unreachable, until it is seen again.
    assert len(UNAVAILABLE) == 1
    UNAVAILABLE[0](SimpleNamespace(address=MAC))
    await hass.async_block_till_done()
    assert hass.states.get(status_id).state == "off"
    instance.set_advertising(True)
    await hass.async_block_till_done()
    assert hass.states.get(status_id).state == "on"

    # The raw link sensor is a new entity, disabled by default.
    link = ent_reg.async_get("binary_sensor.hall_connection_link")
    assert link.unique_id == f"{entry.entry_id}_bluetooth_link"
    assert link.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert link.translation_key == "bluetooth_link"
    assert hass.states.get("binary_sensor.hall_connection_link") is None


async def test_reset_refill_is_in_the_configuration_section(hass):
    from homeassistant.helpers import entity_registry as er

    entry = make_entry(hass)
    await setup_entry(hass, entry)
    ent_reg = er.async_get(hass)
    reset = next(
        e for e in er.async_entries_for_config_entry(ent_reg, entry.entry_id)
        if e.unique_id == f"{entry.entry_id}_reset_refill"
    )
    spray = next(
        e for e in er.async_entries_for_config_entry(ent_reg, entry.entry_id)
        if e.unique_id == f"{entry.entry_id}_trigger"
    )
    assert reset.entity_category is er.EntityCategory.CONFIG
    assert spray.entity_category is None


async def frontend_like_run(hass, flow_id, start):
    """Drive a flow the way the frontend does: refresh (GET = configure) on
    every change and progress event. Returns the last result, and whether
    the frontend would have hit "Invalid flow specified"."""
    from homeassistant.data_entry_flow import (
        EVENT_DATA_ENTRY_FLOW_PROGRESSED,
        EVENT_DATA_ENTRY_FLOW_PROGRESS_UPDATE,
        UnknownFlow,
    )

    results, invalid = [], []

    async def refresh(event):
        if event.data.get("flow_id") != flow_id:
            return
        await asyncio.sleep(0.01)  # the browser's round trip
        try:
            results.append(await hass.config_entries.flow.async_configure(flow_id))
        except UnknownFlow:
            invalid.append(event.event_type)

    unsub = [
        hass.bus.async_listen(EVENT_DATA_ENTRY_FLOW_PROGRESSED, refresh),
        hass.bus.async_listen(EVENT_DATA_ENTRY_FLOW_PROGRESS_UPDATE, refresh),
    ]
    results.append(await start())
    for _ in range(100):
        await asyncio.sleep(0.01)
    for u in unsub:
        u()
    return results[-1], invalid


async def staged_detect(self, *, progress=None, **kwargs):
    progress("connecting", {}, 0.0)
    await asyncio.sleep(0.05)
    for i, ch in enumerate(("6e400002", "6e400003", "6e400004"), 1):
        progress("probing", {"index": str(i), "total": "3", "channel": ch}, (i - 1) / 6)
        await asyncio.sleep(0.05)
    return (C4, C1)


async def test_reconfigure_with_frontend_refreshes(hass):
    entry = make_entry(hass, C3, C1)
    result = await start_reconfigure(hass, entry)
    flow_id = result["flow_id"]
    with patch(DETECT, staged_detect), patch.object(hass.config_entries, "async_schedule_reload"):
        last, invalid = await frontend_like_run(
            hass, flow_id, lambda: hass.config_entries.flow.async_configure(flow_id, {})
        )
    assert invalid == []
    assert last["type"] is FlowResultType.ABORT and last["reason"] == "channels_detected"
    assert entry.data[CONF_COMMAND_CHAR] == C4


async def test_setup_with_frontend_refreshes(hass):
    result = await start_flow(hass)
    flow_id = result["flow_id"]
    with patch(DETECT, staged_detect), \
         patch(f"{CF}.SmartMatic.disconnect", AsyncMock()), \
         patch(f"{CF}.async_discovered_service_info", return_value=discovered()):
        last, invalid = await frontend_like_run(
            hass, flow_id, lambda: hass.config_entries.flow.async_configure(flow_id, {"mac": MAC})
        )
    assert invalid == []
    assert last["type"] is FlowResultType.FORM and last["step_id"] == "name"
    assert last["description_placeholders"] == {"command": "6e400004", "reply": "6e400001"}


async def test_battery_is_shown_as_a_percentage(hass):
    from homeassistant.helpers import entity_registry as er
    from test_devicestatus_frame import FRAME

    entry = make_entry(hass)
    enable_diagnostics(hass, entry)
    await setup_entry(hass, entry)
    entry.runtime_data._on_notification(C1, FRAME)
    await hass.async_block_till_done()

    battery = hass.states.get("sensor.hall_battery")
    voltage = hass.states.get("sensor.hall_battery_voltage")
    mv = entry.runtime_data.device_status.battery_mv
    assert battery.attributes["friendly_name"] == "Hall Battery"
    assert voltage.attributes["friendly_name"] == "Hall Battery voltage"
    assert battery.attributes["device_class"] == "battery"
    assert battery.attributes["unit_of_measurement"] == "%"
    assert battery.state == str(entry.runtime_data.device_status.battery_percent)
    assert voltage.state == str(mv)
    ent_reg = er.async_get(hass)
    assert ent_reg.async_get("sensor.hall_battery_voltage").entity_category is er.EntityCategory.DIAGNOSTIC


async def test_flat_batteries_show_a_toast_but_keep_the_new_state(hass):
    from custom_components.godrej_aer.godrej.exception import BatteryTooLow

    entry = make_entry(hass)
    await setup_entry(hass, entry)
    instance = entry.runtime_data
    flat = AsyncMock(side_effect=BatteryTooLow("x"))

    with patch.object(instance, "spray_now", flat):
        await expect_unconfirmed(hass, "button", "press", "button.hall_spray_now", key="battery_too_low")
    with patch.object(instance, "set_spray_interval", flat):
        await expect_unconfirmed(hass, "switch", "turn_on", "switch.hall_spray_auto", key="battery_too_low")
        await expect_unconfirmed(
            hass, "select", "select_option", "select.hall_spray_interval",
            key="battery_too_low", option="40 minutes",
        )
    # The device did take the mode change; it just can't spray.
    assert hass.states.get("switch.hall_spray_auto").state == "on"
    assert hass.states.get("select.hall_spray_interval").state == "40 minutes"


async def test_control_names(hass):
    entry = make_entry(hass)
    await setup_entry(hass, entry)
    names = {
        "button.hall_spray_now": "Hall Spray now",
        "button.hall_reset_refill_level": "Hall Reset refill level",
        "switch.hall_spray_auto": "Hall Spray: auto",
        "select.hall_spray_interval": "Hall Spray: interval",
        "sensor.hall_signal_strength": "Hall Signal strength",
    }
    for entity_id, name in names.items():
        assert hass.states.get(entity_id).attributes["friendly_name"] == name


async def test_failed_poll_retries_when_the_backoff_ends(hass):
    from datetime import timedelta
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed
    from custom_components.godrej_aer.godrej.godrej import SmartMatic

    entry = make_entry(hass)
    connect = AsyncMock(side_effect=[DeviceConnectionError("proxy not ready"), True])
    with patch.object(SmartMatic, "retry_delay", 30.0), \
         patch("homeassistant.components.bluetooth.async_address_present", return_value=True):
        await setup_entry(hass, entry, connect=connect)
        with patch.object(SmartMatic, "connect_if_needed", connect):
            assert connect.await_count == 1
            async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=20))
            await hass.async_block_till_done()
            assert connect.await_count == 1
            async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
            await hass.async_block_till_done()
            assert connect.await_count == 2  # not a minute later


async def test_update_status_works_with_the_current_target_helper(hass, caplog):
    """HA 2026.10 removed the old (hass, call) form; it used to log a warning."""
    from homeassistant.helpers import device_registry as dr

    entry = make_entry(hass)
    await setup_entry(hass, entry)
    device = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)[0]
    with patch.object(entry.runtime_data, "connect", AsyncMock(return_value=True)) as connect:
        await hass.services.async_call(
            DOMAIN, "update_status", {"device_id": device.id}, blocking=True
        )
    connect.assert_awaited_once_with(purpose="update status")
    assert "deprecated argument hass" not in caplog.text

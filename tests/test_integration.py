"""Integration tests that run in a real Home Assistant (test harness).

    pip install pytest-homeassistant-custom-component
    pytest tests/
"""
import pytest
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

DOMAIN = "aquarium_lighting"


@pytest.fixture(autouse=True)
def _enable(enable_custom_integrations):
    yield


async def _setup(hass, **extra):
    assert await async_setup_component(hass, "input_boolean", {"input_boolean": {"aquarium_auto_mode": {}}})
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="input_boolean.aquarium_auto_mode",
        title="Aquarium Lighting (Reef)",
        data={
            "name": "Reef",
            "auto_entity": "input_boolean.aquarium_auto_mode",
            "output_mode": "ha_light",
            "transition_seconds": 60,
            "resume_transition_seconds": 15,
            "initial_channel": "blue",
            "initial_target": "light.reef_blue",
            **extra,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_entities_are_created(hass):
    await _setup(hass)
    ids = sorted(e for e in hass.states.async_entity_ids() if "blue" in e)
    print("ENTITIES:", ids)
    assert len(ids) == 7
    assert hass.states.get("time.reef_blue_sunrise_start").state == "08:00:00"
    assert hass.states.get("number.reef_blue_maximum_brightness").state == "255.0"
    assert hass.states.get("select.reef_blue_curve").state == "cosine"


async def _until(predicate, timeout=3.0):
    import asyncio

    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return predicate()


async def test_entity_edits_update_the_schedule(hass):
    from homeassistant.exceptions import ServiceValidationError

    entry = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    await hass.services.async_call("time", "set_value", {"entity_id": "time.reef_blue_sunrise_start", "time": "07:30:00"}, blocking=True)
    assert coordinator.schedules["blue"].sunrise_start == "07:30"
    assert hass.states.get("time.reef_blue_sunrise_start").state == "07:30:00"

    await hass.services.async_call("number", "set_value", {"entity_id": "number.reef_blue_minimum_brightness", "value": 20}, blocking=True)
    assert coordinator.schedules["blue"].min == 20

    # invalid: minimum above maximum is refused with a readable error
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call("time", "set_value", {"entity_id": "time.reef_blue_sunrise_end", "time": "07:00:00"}, blocking=True)
    assert coordinator.schedules["blue"].sunrise_end == "11:00"  # unchanged

    # switching the curve to multi_segment seeds points from the current ramps
    await hass.services.async_call("select", "select_option", {"entity_id": "select.reef_blue_curve", "option": "multi_segment"}, blocking=True)
    assert coordinator.schedules["blue"].curve == "multi_segment"
    assert len(coordinator.schedules["blue"].points) >= 2


async def test_auto_mode_follows_the_helper_and_pushes_lights(hass):
    from pytest_homeassistant_custom_component.common import async_mock_service

    calls = async_mock_service(hass, "light", "turn_on")
    hass.states.async_set("light.reef_blue", "off")
    entry = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert not coordinator.auto_enabled and not calls  # off: nothing is sent

    await hass.services.async_call("input_boolean", "turn_on", {"entity_id": "input_boolean.aquarium_auto_mode"}, blocking=True)
    assert await _until(lambda: calls), "turning the helper on should push immediately"
    assert calls[0].data["entity_id"] == "light.reef_blue"
    assert 10 <= calls[0].data["brightness"] <= 255
    assert calls[0].data["transition"] == 15  # resume transition

    await hass.services.async_call("input_boolean", "turn_off", {"entity_id": "input_boolean.aquarium_auto_mode"}, blocking=True)
    assert await _until(lambda: not coordinator.auto_enabled)


async def test_channels_added_and_deleted_via_websocket(hass, hass_ws_client):
    entry = await _setup(hass)
    client = await hass_ws_client(hass)
    sched = {"sunrise_start": "09:00", "sunrise_end": "12:00", "sunset_start": "16:00", "sunset_end": "19:00", "min": 0, "max": 200, "curve": "linear", "target": "light.reef_red", "color": "#ff0000"}

    await client.send_json({"id": 1, "type": "aquarium_lighting/set_channel", "name": "red", "schedule": sched})
    msg = await client.receive_json()
    assert msg["success"], msg
    await hass.async_block_till_done()
    assert hass.states.get("select.reef_red_curve").state == "linear"
    assert hass.states.get("number.reef_red_maximum_brightness").state == "200.0"
    assert msg["result"]["channels"]["red"]["color"] == "#ff0000"

    await client.send_json({"id": 2, "type": "aquarium_lighting/get"})
    snap = (await client.receive_json())["result"]
    assert snap["auto_entity"] == "input_boolean.aquarium_auto_mode" and set(snap["channels"]) == {"blue", "red"}

    await client.send_json({"id": 3, "type": "aquarium_lighting/delete_channel", "name": "red"})
    assert (await client.receive_json())["success"]
    await hass.async_block_till_done()
    assert hass.states.get("select.reef_red_curve") is None
    assert hass.states.get("select.reef_blue_curve") is not None

    # a bad schedule comes back as a readable error
    await client.send_json({"id": 4, "type": "aquarium_lighting/preview", "schedule": {**sched, "min": 300}})
    bad = await client.receive_json()
    assert not bad["success"] and bad["error"]["code"] == "invalid_schedule"
    assert entry.state.name == "LOADED"


# --------------------------------------------------------------------------- #
# moonlight
# --------------------------------------------------------------------------- #
from datetime import datetime  # noqa: E402

from pytest_homeassistant_custom_component.common import async_mock_service  # noqa: E402


def _when(hhmm):
    h, m = map(int, hhmm.split(":"))
    return datetime(2026, 11, 14, h, m)


async def _moon_setup(hass, phase="waxing_crescent", **moon_over):
    entry = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    hass.states.async_set("sensor.moon_phase", phase)
    hass.states.async_set("light.moon", "off")
    await coordinator.async_set_moonlight(
        {"enabled": True, "target": "light.moon", "start": "21:00", "end": "01:00", "min": 10, "max": 50, **moon_over}
    )
    return entry, coordinator


async def test_moonlight_entities_and_edits(hass):
    from homeassistant.exceptions import ServiceValidationError

    entry = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert hass.states.get("switch.reef_moonlight_enabled").state == "off"
    assert hass.states.get("number.reef_moonlight_new_moon_brightness").state == "10.0"
    assert hass.states.get("number.reef_moonlight_full_moon_brightness").state == "50.0"
    assert hass.states.get("time.reef_moonlight_start_time").state == "21:00:00"
    assert hass.states.get("time.reef_moonlight_end_time").state == "01:00:00"

    await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.reef_moonlight_enabled"}, blocking=True)
    await hass.services.async_call("number", "set_value", {"entity_id": "number.reef_moonlight_full_moon_brightness", "value": 80}, blocking=True)
    await hass.services.async_call("time", "set_value", {"entity_id": "time.reef_moonlight_end_time", "time": "02:30:00"}, blocking=True)
    assert coordinator.moonlight.enabled and coordinator.moonlight.max == 80
    assert coordinator.moonlight.end == "02:30"
    assert hass.states.get("switch.reef_moonlight_enabled").state == "on"

    with pytest.raises(ServiceValidationError):  # new moon above full moon
        await hass.services.async_call("number", "set_value", {"entity_id": "number.reef_moonlight_new_moon_brightness", "value": 90}, blocking=True)
    assert coordinator.moonlight.min == 10
    # the channel entities are unaffected (still 7 for "blue")
    assert len([e for e in hass.states.async_entity_ids() if "blue" in e]) == 7


async def test_moonlight_survives_reload(hass):
    entry, coordinator = await _moon_setup(hass, min=15, max=60, start="22:15")
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    reloaded = hass.data[DOMAIN][entry.entry_id]
    assert reloaded is not coordinator
    assert (reloaded.moonlight.enabled, reloaded.moonlight.min, reloaded.moonlight.max, reloaded.moonlight.start) == (True, 15, 60, "22:15")
    assert reloaded.schedules["blue"].target == "light.reef_blue"  # channels untouched


async def test_moonlight_pushes_the_worked_example(hass):
    _entry, c = await _moon_setup(hass)
    on = async_mock_service(hass, "light", "turn_on")
    off = async_mock_service(hass, "light", "turn_off")

    await c._async_apply_moonlight(_when("23:00"), 60, False)
    assert len(on) == 1 and off == []
    assert on[0].data["entity_id"] == "light.moon"
    assert on[0].data["brightness"] == 51  # 20% of 255
    assert on[0].data["transition"] == 60


async def test_moonlight_off_outside_window_is_sent_once(hass):
    _entry, c = await _moon_setup(hass)
    on = async_mock_service(hass, "light", "turn_on")
    off = async_mock_service(hass, "light", "turn_off")

    await c._async_apply_moonlight(_when("23:00"), 60, False)
    await c._async_apply_moonlight(_when("01:00"), 60, False)  # level reaches 0
    await c._async_apply_moonlight(_when("01:01"), 60, False)  # still off: no call
    await c._async_apply_moonlight(_when("14:00"), 60, False)  # still off: no call
    assert len(on) == 1 and len(off) == 1

    await c._async_apply_moonlight(_when("14:01"), 60, True)  # resume re-asserts "off"
    assert len(off) == 2


async def test_moonlight_phase_is_latched_for_the_night(hass):
    _entry, c = await _moon_setup(hass, phase="waxing_crescent")
    on = async_mock_service(hass, "light", "turn_on")
    async_mock_service(hass, "light", "turn_off")

    await c._async_apply_moonlight(_when("22:00"), 60, False)
    hass.states.async_set("sensor.moon_phase", "full_moon")  # rolls over mid-window
    await c._async_apply_moonlight(_when("23:00"), 60, False)
    assert on[-1].data["brightness"] == 51  # still the 20% night

    await c._async_apply_moonlight(_when("12:00"), 60, False)  # window closed -> unlatch
    await c._async_apply_moonlight(_when("23:00"), 60, False)
    assert on[-1].data["brightness"] == 128  # new night: full moon 50% = 127.5


async def test_moonlight_keeps_last_phase_when_sensor_unavailable(hass):
    _entry, c = await _moon_setup(hass, phase="first_quarter")
    assert c.moon_level(_when("23:00")) == 76  # 30% of 255 = 76.5 (round half to even)
    c._moon_night_phase = None  # new night
    hass.states.async_set("sensor.moon_phase", "unavailable")
    assert c.moon_level(_when("23:00")) == 76  # last known phase, not dark
    c._moon_night_phase, c._moon_last_phase = None, None
    assert c.moon_level(_when("23:00")) == 0  # never had a phase: stays off


async def test_moonlight_disabling_turns_the_light_off_once(hass):
    _entry, c = await _moon_setup(hass)
    on = async_mock_service(hass, "light", "turn_on")
    off = async_mock_service(hass, "light", "turn_off")
    await c._async_apply_moonlight(_when("23:00"), 60, False)
    await c.async_update_moonlight({"enabled": False})
    await c._async_apply_moonlight(_when("23:01"), 60, False)
    await c._async_apply_moonlight(_when("23:02"), 60, False)
    assert len(on) == 1 and len(off) == 1


async def test_moonlight_not_touched_when_never_enabled(hass):
    entry = await _setup(hass)  # moonlight default: disabled, no light
    c = hass.data[DOMAIN][entry.entry_id]
    on = async_mock_service(hass, "light", "turn_on")
    off = async_mock_service(hass, "light", "turn_off")
    await c._async_apply_moonlight(_when("23:00"), 60, True)
    assert on == [] and off == []


async def test_snapshot_reports_moonlight(hass):
    _entry, c = await _moon_setup(hass)
    c.moon_level(_when("23:00"))
    snap = c.snapshot()["moonlight"]
    assert snap["enabled"] and snap["phase"] == "waxing_crescent" and snap["peak_percent"] == 20


async def test_options_flow_moonlight_step(hass):
    entry = await _setup(hass)
    c = hass.data[DOMAIN][entry.entry_id]
    hass.states.async_set("light.moon", "off")
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"auto_entity": "input_boolean.aquarium_auto_mode", "output_mode": "ha_light", "transition_seconds": 60, "resume_transition_seconds": 15},
    )
    assert result["step_id"] == "moonlight"

    # enabling without a light is refused
    bad = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"enabled": True, "sensor": "sensor.moon_phase", "min": 10, "max": 50, "start": "21:00:00", "end": "01:00:00"},
    )
    assert bad["type"] == "form" and bad["errors"] == {"target": "light_required"}
    # min above max is refused
    bad = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"enabled": True, "sensor": "sensor.moon_phase", "target": "light.moon", "min": 60, "max": 50, "start": "21:00:00", "end": "01:00:00"},
    )
    assert bad["errors"] == {"base": "min_above_max"}

    done = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"enabled": True, "sensor": "sensor.my_moon", "target": "light.moon", "min": 5, "max": 40, "start": "20:30:00", "end": "00:30:00"},
    )
    assert done["type"] == "create_entry"
    await hass.async_block_till_done()
    m = hass.data[DOMAIN][entry.entry_id].moonlight  # entry may have been reloaded
    assert (m.enabled, m.sensor, m.target, m.min, m.max, m.start, m.end) == (
        True, "sensor.my_moon", "light.moon", 5, 40, "20:30", "00:30",
    )


async def test_full_tick_drives_channels_and_moonlight_together(hass, freezer):
    from homeassistant.util import dt as dt_util

    _entry, c = await _moon_setup(hass)
    hass.states.async_set("light.reef_blue", "off")
    on = async_mock_service(hass, "light", "turn_on")
    async_mock_service(hass, "light", "turn_off")
    # 22:59 local + the 60 s look-ahead = 23:00, the middle of the window
    freezer.move_to(dt_util.parse_datetime("2026-11-14T22:59:00").replace(tzinfo=dt_util.DEFAULT_TIME_ZONE))
    await c._async_apply(False)
    by_entity = {call.data["entity_id"]: call.data["brightness"] for call in on}
    assert by_entity["light.moon"] == 51
    assert "light.reef_blue" in by_entity  # the channel schedule is still driven
    assert c._failing == set()

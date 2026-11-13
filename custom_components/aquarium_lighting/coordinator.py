"""Background task that computes and pushes brightness once per minute.

Design rules (from the requirements):

* No HA automations, no SQL.
* Brightness is NEVER stored. Every tick it is recomputed from
  schedule + wall-clock time. Only the *schedules* are persisted
  (``.storage/aquarium_lighting.<entry_id>``).
* Auto mode is an ``input_boolean`` helper you create (one per aquarium). HA
  itself restores its state across restarts; we just follow it.
* While auto mode is OFF the task is parked on an ``asyncio.Event`` - it does
  not wake up, compute or push anything.
* Smoothness: each tick sends the level the schedule will have
  ``transition`` seconds from now, together with that transition time, so the
  device finishes the fade exactly when the next tick arrives (no lag, no
  staircase). Devices with their own ``default_transition_length`` (ESPHome)
  do the actual interpolation.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import datetime, timedelta
from typing import Any, Callable

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, EVENT_HOMEASSISTANT_STARTED, STATE_ON
from homeassistant.core import CoreState, Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONF_AUTO_ENTITY,
    CONF_INITIAL_CHANNEL,
    CONF_INITIAL_TARGET,
    CONF_OUTPUT_MODE,
    CONF_RESUME_TRANSITION,
    CONF_TRANSITION,
    DEFAULT_AUTO_ENTITY,
    DEFAULT_RESUME_TRANSITION,
    DEFAULT_TRANSITION,
    DOMAIN,
    MODE_LIGHT,
    MODE_MQTT,
    MODE_REST,
    STORAGE_VERSION,
)
from .light_controller import (
    ChannelSchedule,
    MoonlightSchedule,
    compute_all,
    compute_brightness,
    default_points,
    minute_of_day,
    normalize_moon_phase,
    preview_curve,
    preview_moon_curve,
    validate_channel_name,
)

_LOGGER = logging.getLogger(__name__)
_MOON_KEY = "(moonlight)"  # failure-tracking key; can never clash with a channel name


class AquariumLightingCoordinator:
    """Owns the schedules, the auto-mode flag and the background loop."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.schedules: dict[str, ChannelSchedule] = {}
        self.moonlight = MoonlightSchedule()
        # In-memory only (never persisted): last phase the sensor reported, the
        # phase latched for the current night, and the last level sent.
        self._moon_last_phase: str | None = None
        self._moon_night_phase: str | None = None
        self._moon_sent: int | None = None
        self._moon_warned = False

        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        self._auto_event = asyncio.Event()  # set <=> auto mode ON
        self._kick = asyncio.Event()  # wakes the loop early
        self._resume = False  # next push uses the short "resume" transition
        self._task: asyncio.Task | None = None
        self._listeners: list[Callable[[], None]] = []
        self._failing: set[str] = set()
        self._unsub_auto: Callable[[], None] | None = None

    # ------------------------------------------------------------------ #
    # options
    # ------------------------------------------------------------------ #
    @property
    def _options(self) -> dict[str, Any]:
        return {**self.entry.data, **self.entry.options}

    @property
    def tank_name(self) -> str:
        return self._options.get(CONF_NAME) or "Aquarium"

    @property
    def auto_entity(self) -> str:
        return self._options.get(CONF_AUTO_ENTITY) or DEFAULT_AUTO_ENTITY

    @property
    def auto_enabled(self) -> bool:
        return self._auto_event.is_set()

    @property
    def mode(self) -> str:
        return self._options.get(CONF_OUTPUT_MODE, MODE_LIGHT)

    @property
    def transition(self) -> int:
        return int(self._options.get(CONF_TRANSITION, DEFAULT_TRANSITION))

    @property
    def resume_transition(self) -> int:
        return int(self._options.get(CONF_RESUME_TRANSITION, DEFAULT_RESUME_TRANSITION))

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    async def async_load(self) -> None:
        """Load schedules + auto flag from storage (seed on first run)."""
        data = await self._store.async_load()
        if not data:
            data = {"channels": {}}
            name = self._options.get(CONF_INITIAL_CHANNEL)
            if name:
                data["channels"][name] = ChannelSchedule(
                    target=self._options.get(CONF_INITIAL_TARGET, "")
                ).to_dict()
            await self._store.async_save(data)

        for name, raw in data.get("channels", {}).items():
            try:
                self.schedules[name] = ChannelSchedule.from_dict(raw)
            except ValueError as err:
                _LOGGER.error("Ignoring invalid schedule for channel %s: %s", name, err)

        try:
            self.moonlight = MoonlightSchedule.from_dict(data.get("moonlight") or {})
        except ValueError as err:
            _LOGGER.error("Ignoring invalid moonlight settings: %s", err)


    @callback
    def async_start(self) -> None:
        """Start the background loop (cancelled automatically on unload)."""
        self._unsub_auto = async_track_state_change_event(
            self.hass, [self.auto_entity], self._handle_auto_state
        )
        self._sync_auto_from_state()
        self._task = self.entry.async_create_background_task(
            self.hass, self._run(), f"{DOMAIN}_loop"
        )

    async def async_stop(self) -> None:
        if self._unsub_auto:
            self._unsub_auto()
            self._unsub_auto = None
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _async_save(self) -> None:
        await self._store.async_save(
            {
                "channels": {n: s.to_dict() for n, s in self.schedules.items()},
                "moonlight": self.moonlight.to_dict(),
            }
        )

    # ------------------------------------------------------------------ #
    # listeners (switch entity refresh)
    # ------------------------------------------------------------------ #
    @callback
    def async_add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(cb)

        def _remove() -> None:
            with contextlib.suppress(ValueError):
                self._listeners.remove(cb)

        return _remove

    @callback
    def _notify(self) -> None:
        """Tell entities the schedules changed (not called on ticks)."""
        for cb in list(self._listeners):
            cb()

    # ------------------------------------------------------------------ #
    # public actions
    # ------------------------------------------------------------------ #
    @callback
    def _handle_auto_state(self, event: Event) -> None:
        new = event.data.get("new_state")
        self._set_auto(new is not None and new.state == STATE_ON)

    @callback
    def _sync_auto_from_state(self) -> None:
        state = self.hass.states.get(self.auto_entity)
        self._set_auto(state is not None and state.state == STATE_ON)

    @callback
    def _set_auto(self, enabled: bool) -> None:
        """Follow the helper. Turning ON applies the level for *now* smoothly."""
        if enabled == self._auto_event.is_set():
            return
        if enabled:
            self._resume = True  # smooth jump to where the schedule is *now*
            self._auto_event.set()
            self._kick.set()
        else:
            self._auto_event.clear()  # loop parks; nothing runs while OFF

    async def async_set_channel(self, name: str, data: dict[str, Any]) -> None:
        validate_channel_name(name)
        self.schedules[name] = ChannelSchedule.from_dict(data)  # may raise ValueError
        await self._async_save()
        self.request_refresh()
        self._notify()

    async def async_update_channel(self, name: str, changes: dict[str, Any]) -> None:
        """Change some fields of one channel (used by the number/time/select entities)."""
        schedule = self.schedules.get(name)
        if schedule is None:
            raise ValueError(f"Unknown channel {name!r}")
        data = {**schedule.to_dict(), **changes}
        if data.get("curve") == "multi_segment" and not data.get("points"):
            data["points"] = default_points(schedule)  # start from the current shape
        await self.async_set_channel(name, data)

    async def async_set_moonlight(self, data: dict[str, Any]) -> None:
        """Replace the moonlight settings (validated, saved, applied at once)."""
        self.moonlight = MoonlightSchedule.from_dict(data)  # may raise ValueError
        await self._async_save()
        self.request_refresh()
        self._notify()

    async def async_update_moonlight(self, changes: dict[str, Any]) -> None:
        """Change some moonlight fields (used by the moonlight entities)."""
        await self.async_set_moonlight({**self.moonlight.to_dict(), **changes})

    async def async_delete_channel(self, name: str) -> None:
        if name not in self.schedules:
            raise ValueError(f"Unknown channel {name!r}")
        del self.schedules[name]
        await self._async_save()
        # Removing the channel's device also removes its entities.
        dev_reg = dr.async_get(self.hass)
        device = dev_reg.async_get_device(identifiers={(DOMAIN, f"{self.entry.entry_id}_{name}")})
        if device:
            dev_reg.async_remove_device(device.id)
        self._notify()

    @callback
    def request_refresh(self) -> None:
        """Re-apply immediately (e.g. after a schedule edit) if auto is ON."""
        if self.auto_enabled:
            self._resume = True
            self._kick.set()

    def snapshot(self) -> dict[str, Any]:
        """JSON-safe view for the Lovelace card. Everything is derived."""
        now = dt_util.now()
        return {
            "auto": self.auto_enabled,
            "auto_entity": self.auto_entity,
            "name": self.entry.title,
            "mode": self.mode,
            "now_minute": minute_of_day(now),
            "channels": {n: s.to_dict() for n, s in self.schedules.items()},
            "moonlight": self._moon_snapshot(now),
            "current": compute_all(self.schedules, now),
            "curves": {n: preview_curve(s, 10) for n, s in self.schedules.items()},
        }

    # ------------------------------------------------------------------ #
    # the background loop
    # ------------------------------------------------------------------ #
    async def _wait_for_start(self) -> None:
        """Let HA finish booting so light entities / MQTT exist."""
        if self.hass.state is CoreState.running:
            return
        started = asyncio.Event()
        unsub = self.hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STARTED, lambda _evt: started.set()
        )
        try:
            await started.wait()
        finally:
            unsub()

    async def _run(self) -> None:
        await self._wait_for_start()
        self._sync_auto_from_state()
        if self.hass.states.get(self.auto_entity) is None:
            _LOGGER.warning(
                "Auto-mode helper %s does not exist. Create a Toggle helper with "
                "that entity id (Settings > Devices & services > Helpers); the "
                "lights are not controlled until it exists and is on",
                self.auto_entity,
            )
        while True:
            await self._auto_event.wait()  # blocks completely while auto is OFF
            resume, self._resume = self._resume, False
            self._kick.clear()
            try:
                await self._async_apply(resume)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the loop must never die
                _LOGGER.exception("Unexpected error while applying lighting schedule")

            now = dt_util.utcnow()
            delay = 60.0 - now.second - now.microsecond / 1e6 + 0.25  # next :00
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._kick.wait(), timeout=delay)

    async def _async_apply(self, resume: bool) -> None:
        transition = self.resume_transition if resume else self.transition
        # Look ahead by one transition: the device reaches this level exactly
        # when we next push, so the fade tracks the schedule without lag.
        when = dt_util.now() + timedelta(seconds=transition)

        names, jobs = [], []
        for name, sched in self.schedules.items():
            if not sched.target:
                continue
            names.append(name)
            jobs.append(
                self._async_push(sched.target, compute_brightness(sched, when), transition)
            )
        names.append(_MOON_KEY)
        jobs.append(self._async_apply_moonlight(when, transition, resume))

        for name, result in zip(names, await asyncio.gather(*jobs, return_exceptions=True)):
            if isinstance(result, Exception):
                if name not in self._failing:  # warn once, not every minute
                    _LOGGER.warning("Channel %s: could not set brightness: %s", name, result)
                self._failing.add(name)
            elif name in self._failing:
                _LOGGER.info("Channel %s: recovered", name)
                self._failing.discard(name)

    # ------------------------------------------------------------------ #
    # moonlight
    # ------------------------------------------------------------------ #
    def _read_moon_phase(self) -> str | None:
        """Current phase from the configured sensor, remembering the last good one.

        A sensor that is briefly unavailable (restart, integration reload) keeps
        the last known phase rather than switching the moonlight off.
        """
        state = self.hass.states.get(self.moonlight.sensor)
        phase = normalize_moon_phase(state.state if state else None)
        if phase is not None:
            self._moon_last_phase = phase
        return self._moon_last_phase

    def peek_moon_phase(self, sensor: str | None = None) -> str | None:
        """Phase from ``sensor`` (default: the configured one) without latching anything.

        Used by the card's preview; falls back to the last phase seen.
        """
        state = self.hass.states.get(sensor or self.moonlight.sensor)
        return normalize_moon_phase(state.state if state else None) or self._moon_last_phase

    def _moon_snapshot(self, now: datetime) -> dict[str, Any]:
        """Moonlight block for the card. Read-only: never touches the night latch."""
        cfg = self.moonlight
        minute = minute_of_day(now)
        if self._moon_night_phase is not None and cfg.in_window(minute):
            phase = self._moon_night_phase
        else:
            phase = self._read_moon_phase()
        return {
            **cfg.to_dict(),
            "phase": phase,
            "peak_percent": cfg.peak_percent(phase),
            "current": int(round(cfg.level_at(minute, phase))),
            "curve": preview_moon_curve(cfg, phase, 10),
        }

    def moon_level(self, when: datetime) -> int:
        """Moonlight brightness 0-255 at ``when``.

        The phase is read once when a night's window opens and kept until it
        closes, so a sensor that rolls over at midnight cannot make the level
        jump half-way through a window that crosses midnight.
        """
        cfg = self.moonlight
        minute = minute_of_day(when)
        if not cfg.enabled or not cfg.in_window(minute):
            self._moon_night_phase = None
            return 0
        if self._moon_night_phase is None:
            self._moon_night_phase = self._read_moon_phase()
            if self._moon_night_phase is None and not self._moon_warned:
                self._moon_warned = True
                _LOGGER.warning(
                    "Moonlight: %s has no usable moon phase (need one of new_moon, "
                    "waxing_crescent, first_quarter, ... ); the moonlight stays off. "
                    "The default sensor.moon_phase comes from the Moon integration.",
                    cfg.sensor,
                )
        return int(round(cfg.level_at(minute, self._moon_night_phase)))

    async def _async_apply_moonlight(
        self, when: datetime, transition: int, resume: bool
    ) -> None:
        cfg = self.moonlight
        level = self.moon_level(when)
        if not cfg.target:
            return
        if not cfg.enabled and not self._moon_sent:
            return  # disabled and nothing of ours left on: not ours to touch
        if level == 0 and self._moon_sent == 0 and not resume:
            # Already off: leave the light alone so it can be used by hand
            # outside the window. A resume (start-up, auto on, edit) re-asserts it.
            return
        await self._push_light(cfg.target, level, transition)
        self._moon_sent = level

    # ------------------------------------------------------------------ #
    # output back-ends
    # ------------------------------------------------------------------ #
    async def _async_push(self, target: str, level: int, transition: int) -> None:
        if self.mode == MODE_REST:
            await self._push_rest(target, level, transition)
        elif self.mode == MODE_MQTT:
            await self._push_mqtt(target, level, transition)
        else:
            await self._push_light(target, level, transition)

    async def _push_light(self, entity_id: str, level: int, transition: int) -> None:
        if self.hass.states.get(entity_id) is None:
            raise HomeAssistantError(f"entity {entity_id} does not exist (yet)")
        if level <= 0:
            service, data = "turn_off", {"entity_id": entity_id, "transition": transition}
        else:
            service, data = "turn_on", {
                "entity_id": entity_id,
                "brightness": level,
                "transition": transition,
            }
        await self.hass.services.async_call("light", service, data, blocking=False)

    async def _push_rest(self, base_url: str, level: int, transition: int) -> None:
        """ESPHome web_server REST: POST <base>/turn_on?brightness=..&transition=.."""
        base = base_url.rstrip("/")
        if level <= 0:
            url, params = f"{base}/turn_off", {"transition": transition}
        else:
            url, params = f"{base}/turn_on", {"brightness": level, "transition": transition}
        session = async_get_clientsession(self.hass)
        async with session.post(
            url, params=params, timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            resp.raise_for_status()

    async def _push_mqtt(self, topic: str, level: int, transition: int) -> None:
        """ESPHome MQTT light (JSON schema): publish to the light's command topic."""
        if not self.hass.config_entries.async_entries("mqtt"):
            raise HomeAssistantError("the MQTT integration is not set up")
        from homeassistant.components import mqtt  # lazy: optional dependency

        payload: dict[str, Any] = (
            {"state": "OFF", "transition": transition}
            if level <= 0
            else {"state": "ON", "brightness": level, "transition": transition}
        )
        await mqtt.async_publish(self.hass, topic, json.dumps(payload))

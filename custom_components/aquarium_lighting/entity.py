"""Shared base for the per-channel number / time / select entities.

These entities are *views* of the schedule held by the coordinator: they hold no
state of their own. Writing one updates the schedule (validated, saved, applied
immediately if auto mode is on); changes made from the card or any other entity
are pushed back to all of them.
"""
from __future__ import annotations

from typing import Any, Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.const import EntityCategory
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import AquariumLightingCoordinator
from .light_controller import ChannelSchedule, MoonlightSchedule


class ChannelEntity(Entity):
    """One schedule parameter of one channel."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: AquariumLightingCoordinator,
        entry: ConfigEntry,
        channel: str,
        key: str,
        name: str,
        icon: str,
    ) -> None:
        self._coordinator = coordinator
        self._channel = channel
        self._key = key
        self._attr_name = name
        self._attr_icon = icon
        self._attr_unique_id = f"{entry.entry_id}_{channel}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_{channel}")},
            name=f"{coordinator.tank_name} {channel.replace('_', ' ').title()}",
            manufacturer="Aquarium Lighting",
            model="Lighting channel",
        )

    @property
    def _schedule(self) -> ChannelSchedule | None:
        return self._coordinator.schedules.get(self._channel)

    @property
    def available(self) -> bool:
        return self._schedule is not None

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._coordinator.async_add_listener(self._handle_change))

    @callback
    def _handle_change(self) -> None:
        if self._schedule is not None:
            self.async_write_ha_state()

    async def _async_change(self, **changes: Any) -> None:
        """Apply a change; an invalid schedule is reported to the user."""
        try:
            await self._coordinator.async_update_channel(self._channel, changes)
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err


class MoonlightEntity(Entity):
    """One moonlight setting. Same view-of-the-stored-config idea as ChannelEntity.

    Identifiers use a colon, which a channel name can never contain, so a channel
    called "moonlight" cannot clash with the moonlight device or its entities.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: AquariumLightingCoordinator,
        entry: ConfigEntry,
        key: str,
        name: str,
        icon: str,
    ) -> None:
        self._coordinator = coordinator
        self._key = key
        self._attr_name = name
        self._attr_icon = icon
        self._attr_unique_id = f"{entry.entry_id}:moonlight:{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}:moonlight")},
            name=f"{coordinator.tank_name} Moonlight",
            manufacturer="Aquarium Lighting",
            model="Moonlight",
        )

    @property
    def _moonlight(self) -> MoonlightSchedule:
        return self._coordinator.moonlight

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._coordinator.async_add_listener(self._handle_change))

    @callback
    def _handle_change(self) -> None:
        self.async_write_ha_state()

    async def _async_change(self, **changes: Any) -> None:
        try:
            await self._coordinator.async_update_moonlight(changes)
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err


@callback
def async_setup_channel_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    build: Callable[[AquariumLightingCoordinator, ConfigEntry, str], list[Entity]],
) -> None:
    """Create entities for every channel now, and for channels added later."""
    coordinator: AquariumLightingCoordinator = hass.data[DOMAIN][entry.entry_id]
    known: set[str] = set()

    @callback
    def _sync() -> None:
        known.intersection_update(coordinator.schedules)  # forget deleted channels
        new = [c for c in coordinator.schedules if c not in known]
        if new:
            known.update(new)
            async_add_entities([e for c in new for e in build(coordinator, entry, c)])

    _sync()
    entry.async_on_unload(coordinator.async_add_listener(_sync))

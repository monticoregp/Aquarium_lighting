"""switch entity: turn the moonlight on or off."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import MoonlightEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([MoonlightSwitch(coordinator, entry, "enabled", "Enabled", "mdi:weather-night")])


class MoonlightSwitch(MoonlightEntity, SwitchEntity):
    """Enable the moonlight. It still only runs while auto mode is on."""

    @property
    def is_on(self) -> bool:
        return self._moonlight.enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_change(enabled=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_change(enabled=False)

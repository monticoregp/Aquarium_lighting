"""number entities: min/max brightness (0-255) per channel, new/full moon % for the moonlight."""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import ChannelEntity, MoonlightEntity, async_setup_channel_entities

_NUMBERS = (
    ("min", "Minimum brightness", "mdi:brightness-3"),
    ("max", "Maximum brightness", "mdi:brightness-7"),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_setup_channel_entities(
        hass,
        entry,
        async_add_entities,
        lambda coord, e, ch: [ChannelNumber(coord, e, ch, k, n, i) for k, n, i in _NUMBERS],
    )
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [MoonlightNumber(coordinator, entry, k, n, i) for k, n, i in _MOON_NUMBERS]
    )


_MOON_NUMBERS = (
    ("min", "New moon brightness", "mdi:moon-new"),
    ("max", "Full moon brightness", "mdi:moon-full"),
)


class MoonlightNumber(MoonlightEntity, NumberEntity):
    """Moonlight peak brightness in percent at new moon (min) and full moon (max)."""

    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_mode = NumberMode.SLIDER

    @property
    def native_value(self) -> float | None:
        return float(getattr(self._moonlight, self._key))

    async def async_set_native_value(self, value: float) -> None:
        await self._async_change(**{self._key: int(value)})


class ChannelNumber(ChannelEntity, NumberEntity):
    _attr_native_min_value = 0
    _attr_native_max_value = 255
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER

    @property
    def native_value(self) -> float | None:
        schedule = self._schedule
        return None if schedule is None else float(getattr(schedule, self._key))

    async def async_set_native_value(self, value: float) -> None:
        await self._async_change(**{self._key: int(value)})

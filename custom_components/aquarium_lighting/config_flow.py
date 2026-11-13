"""Config flow: choose output mode + transitions, then create the first channel.

More channels (and all schedule editing) are done from the Lovelace card.
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TimeSelector,
)

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
)
from .light_controller import DEFAULT_MOON_SENSOR, validate_channel_name

_MODE_OPTIONS = [
    SelectOptionDict(value=MODE_LIGHT, label="Home Assistant light entities"),
    SelectOptionDict(value=MODE_REST, label="ESPHome REST API (direct HTTP to ESP32)"),
    SelectOptionDict(value=MODE_MQTT, label="MQTT (ESPHome JSON light schema)"),
]


def _settings_schema(defaults: dict[str, Any], *, creating: bool = False) -> vol.Schema:
    seconds = NumberSelectorConfig(
        min=0, max=120, step=1, unit_of_measurement="s", mode=NumberSelectorMode.BOX
    )
    first: dict[Any, Any] = {}
    if creating:
        first[vol.Required(CONF_NAME, default="Aquarium")] = TextSelector()
        auto_key = vol.Required(CONF_AUTO_ENTITY)
    else:
        auto_key = vol.Required(
            CONF_AUTO_ENTITY, default=defaults.get(CONF_AUTO_ENTITY) or DEFAULT_AUTO_ENTITY
        )
    first[auto_key] = EntitySelector(EntitySelectorConfig(domain="input_boolean"))
    return vol.Schema(
        {
            **first,
            vol.Required(
                CONF_OUTPUT_MODE, default=defaults.get(CONF_OUTPUT_MODE, MODE_LIGHT)
            ): SelectSelector(
                SelectSelectorConfig(options=_MODE_OPTIONS, mode=SelectSelectorMode.DROPDOWN)
            ),
            vol.Required(
                CONF_TRANSITION, default=defaults.get(CONF_TRANSITION, DEFAULT_TRANSITION)
            ): NumberSelector(seconds),
            vol.Required(
                CONF_RESUME_TRANSITION,
                default=defaults.get(CONF_RESUME_TRANSITION, DEFAULT_RESUME_TRANSITION),
            ): NumberSelector(seconds),
        }
    )


class AquariumLightingConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._settings: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            # one aquarium per auto-mode helper; create more entries for more tanks
            await self.async_set_unique_id(user_input[CONF_AUTO_ENTITY])
            self._abort_if_unique_id_configured()
            self._settings = user_input
            return await self.async_step_channel()
        return self.async_show_form(
            step_id="user", data_schema=_settings_schema({}, creating=True)
        )

    async def async_step_channel(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        mode = self._settings[CONF_OUTPUT_MODE]
        if user_input is not None:
            name = user_input["name"].strip().lower().replace(" ", "_")
            try:
                validate_channel_name(name)
            except ValueError:
                errors["name"] = "invalid_name"
            else:
                tank = self._settings.get(CONF_NAME, "Aquarium")
                return self.async_create_entry(
                    title=f"Aquarium Lighting ({tank})",
                    data={
                        **self._settings,
                        CONF_INITIAL_CHANNEL: name,
                        CONF_INITIAL_TARGET: user_input.get("target", ""),
                    },
                )

        target_selector = (
            EntitySelector(EntitySelectorConfig(domain="light"))
            if mode == MODE_LIGHT
            else TextSelector()
        )
        schema = vol.Schema(
            {
                vol.Required("name", default="blue"): TextSelector(),
                vol.Optional("target"): target_selector,
            }
        )
        return self.async_show_form(
            step_id="channel",
            data_schema=schema,
            errors=errors,
            description_placeholders={"target_help": _TARGET_HELP[mode]},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return AquariumLightingOptionsFlow()


class AquariumLightingOptionsFlow(config_entries.OptionsFlow):
    """Step 1: output mode / transition lengths. Step 2: moonlight.

    Step 1 is kept in the entry options (and reloads the entry). Step 2 is
    stored with the schedules, like the moonlight entities do, and takes effect
    immediately.
    """

    def __init__(self) -> None:
        self._options: dict[str, Any] = {}

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self._options = user_input
            return await self.async_step_moonlight()
        current = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(step_id="init", data_schema=_settings_schema(current))

    async def async_step_moonlight(self, user_input: dict[str, Any] | None = None):
        coordinator = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
        if coordinator is None:  # entry failed to load: nothing to configure
            return self.async_create_entry(data=self._options)
        errors: dict[str, str] = {}
        values: dict[str, Any] = coordinator.moonlight.to_dict()
        if user_input is not None:
            values = {**values, **user_input, "target": user_input.get("target") or ""}
            if values["enabled"] and not values["target"]:
                errors["target"] = "light_required"
            elif int(values["min"]) > int(values["max"]):
                errors["base"] = "min_above_max"
            elif str(values["start"])[:5] == str(values["end"])[:5]:
                errors["base"] = "same_times"
            else:
                try:
                    await coordinator.async_set_moonlight(values)
                except ValueError:
                    errors["base"] = "invalid_moonlight"
                else:
                    return self.async_create_entry(data=self._options)

        percent = NumberSelectorConfig(
            min=0, max=100, step=1, unit_of_measurement="%", mode=NumberSelectorMode.SLIDER
        )
        target_key = (
            vol.Optional("target", description={"suggested_value": values["target"]})
            if values["target"]
            else vol.Optional("target")
        )
        schema = vol.Schema(
            {
                vol.Required("enabled", default=bool(values["enabled"])): BooleanSelector(),
                vol.Required(
                    "sensor", default=values["sensor"] or DEFAULT_MOON_SENSOR
                ): EntitySelector(EntitySelectorConfig(domain="sensor")),
                target_key: EntitySelector(EntitySelectorConfig(domain="light")),
                vol.Required("min", default=int(values["min"])): NumberSelector(percent),
                vol.Required("max", default=int(values["max"])): NumberSelector(percent),
                vol.Required("start", default=f"{str(values['start'])[:5]}:00"): TimeSelector(),
                vol.Required("end", default=f"{str(values['end'])[:5]}:00"): TimeSelector(),
            }
        )
        return self.async_show_form(step_id="moonlight", data_schema=schema, errors=errors)


_TARGET_HELP = {
    MODE_LIGHT: "Pick the light entity that powers this channel.",
    MODE_REST: "Base URL of the ESPHome light, e.g. http://reef-light.local/light/blue",
    MODE_MQTT: "The light's MQTT command topic, e.g. reef-light/light/blue/command",
}

"""Constants for Aquarium Lighting."""

DOMAIN = "aquarium_lighting"
VERSION = "0.4.1"

CONF_AUTO_ENTITY = "auto_entity"  # input_boolean that switches auto mode on/off
CONF_OUTPUT_MODE = "output_mode"
CONF_TRANSITION = "transition_seconds"
CONF_RESUME_TRANSITION = "resume_transition_seconds"
CONF_INITIAL_CHANNEL = "initial_channel"
CONF_INITIAL_TARGET = "initial_target"

# Used when an older config entry has no auto entity stored yet.
DEFAULT_AUTO_ENTITY = "input_boolean.aquarium_auto_mode"

MODE_LIGHT = "ha_light"  # call light.turn_on on a Home Assistant light entity
MODE_REST = "esphome_rest"  # POST to the ESPHome web_server REST API
MODE_MQTT = "mqtt"  # publish ESPHome JSON-schema command over MQTT
OUTPUT_MODES = (MODE_LIGHT, MODE_REST, MODE_MQTT)

# Regular ticks: how long each fade lasts. Each tick sends the brightness the
# schedule will have *one transition from now*, so the device arrives exactly
# when the next tick fires.
DEFAULT_TRANSITION = 60
# First push after HA start / switching to auto / editing a schedule.
DEFAULT_RESUME_TRANSITION = 15

STORAGE_VERSION = 1

FRONTEND_URL = "/aquarium_lighting/aquarium-lighting-card.js"

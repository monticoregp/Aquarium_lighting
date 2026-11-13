# Changelog

## 0.4.0
- New **moonlight**: one light entity per aquarium that follows the moon phase. Set
  it up under the integration's **Configure** (second page of the options). The moon
  phase sensor defaults to `sensor.moon_phase` and can be any sensor that reports the
  same eight phase names.
- Peak brightness runs from the *new moon* level to the *full moon* level in four
  equal steps (new moon 0, waxing crescent 1, first quarter 2, waxing gibbous 3,
  full moon 4, then back down). Each night the light follows a parabola between the
  start and end time (off, peak in the middle, off), and the window may cross midnight.
- The phase is read when the night's window opens and kept until it closes, so a
  sensor that changes at midnight cannot make the light jump mid-window.
- New entities (device "<aquarium> Moonlight"): enable `switch`, new/full moon
  brightness `number` (%), start/end `time`.
- Like the channels, the moonlight only runs while auto mode is on. Outside the
  window it is switched off once and then left alone, so you can still use it by hand.
- The websocket `aquarium_lighting/get` result now includes a `moonlight` block.

## 0.3.1
- The card script is now registered as a dashboard resource automatically (storage
  mode), with a version-stamped URL so upgrades refresh the browser cache. A resource
  you added by hand is updated rather than duplicated, and it is removed again when the
  last entry is deleted. In YAML resource mode add the resource to your dashboard YAML.
- Cards are no longer listed twice in the card picker

## 0.3.0
- Every schedule parameter is now an editable entity per channel: sunrise/sunset
  start and end (`time`), minimum and maximum brightness (`number`), curve (`select`)
- Entities and the editor card stay in sync; channels added/removed in the card
  appear/disappear as entities

## 0.2.0
- Auto mode is now an `input_boolean` you create (one per aquarium); several aquariums supported
- Per-channel graph colours
- "Show all channels" toggle in the editor card
- New graph-only card (`aquarium-lighting-graph-card`)
- Removed `switch.aquarium_auto_mode` (cleaned up automatically)

## 0.1.0
- Initial release: schedule engine, background task, config flow, editor card, ESPHome example

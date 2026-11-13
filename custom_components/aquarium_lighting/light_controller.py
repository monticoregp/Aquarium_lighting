"""Brightness engine for Aquarium Lighting.

This module has NO Home Assistant imports. Everything here is a pure function
of (channel schedule, wall-clock time), which gives us three properties:

* the integration never needs to remember "current brightness" - it is always
  recomputed, so restarts, pauses and manual overrides cannot cause drift;
* it can be unit-tested stand-alone (see tests/test_light_controller.py);
* the Lovelace card previews curves with exactly the code that drives the
  lights (via the websocket ``preview`` command).

Levels use the Home Assistant light scale, 0-255.
"""
from __future__ import annotations

import math
import re
from bisect import bisect_right
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from typing import Any

MINUTES_PER_DAY = 1440
MAX_LEVEL = 255
CURVES = ("linear", "cosine", "parabolic", "multi_segment")
CHANNEL_NAME_RE = re.compile(r"^[a-z0-9_]{1,32}$")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_TIME_KEYS = ("sunrise_start", "sunrise_end", "sunset_start", "sunset_end")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def parse_hhmm(value: Any) -> int:
    """Parse "HH:MM" (or "HH:MM:SS") into minutes since midnight."""
    try:
        hours_s, minutes_s = str(value).strip().split(":")[:2]
        hours, minutes = int(hours_s), int(minutes_s)
    except (ValueError, TypeError):
        raise ValueError(f"Invalid time {value!r}; expected HH:MM") from None
    if not (0 <= hours < 24 and 0 <= minutes < 60):
        raise ValueError(f"Invalid time {value!r}; expected HH:MM (00:00-23:59)")
    return hours * 60 + minutes


def format_hhmm(minutes: int) -> str:
    """Format minutes since midnight as zero-padded HH:MM."""
    minutes %= MINUTES_PER_DAY
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def validate_channel_name(name: str) -> str:
    """Return the name if valid, else raise ValueError."""
    if not isinstance(name, str) or not CHANNEL_NAME_RE.match(name):
        raise ValueError(
            "Channel names may only contain lowercase letters, digits and "
            "underscores (max 32 characters)"
        )
    return name


def minute_of_day(when: datetime) -> float:
    """Fractional minutes since local midnight for ``when``."""
    return when.hour * 60 + when.minute + when.second / 60 + when.microsecond / 6e7


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _shape(curve: str, p: float) -> float:
    """Map linear progress 0..1 to eased progress 0..1."""
    p = _clamp(p, 0.0, 1.0)
    if curve == "cosine":
        return (1.0 - math.cos(math.pi * p)) / 2.0
    if curve == "parabolic":  # quadratic ease-in / ease-out
        return 2.0 * p * p if p < 0.5 else 1.0 - 2.0 * (1.0 - p) ** 2
    return p  # linear


def _progress(t: float, start: int, end: int) -> float | None:
    """Progress 0..1 of ``t`` through the window start->end, or None if outside.

    Windows may wrap midnight (e.g. 23:00 -> 01:00).
    """
    span = (end - start) % MINUTES_PER_DAY
    if span == 0:
        return None
    offset = (t - start) % MINUTES_PER_DAY
    return offset / span if offset <= span else None


# --------------------------------------------------------------------------- #
# schedule
# --------------------------------------------------------------------------- #
@dataclass
class ChannelSchedule:
    """Independent schedule for one lighting channel."""

    sunrise_start: str = "08:00"
    sunrise_end: str = "11:00"
    sunset_start: str = "17:00"
    sunset_end: str = "20:00"
    min: int = 10
    max: int = 255
    curve: str = "cosine"
    # multi_segment only: [["HH:MM", level], ...] interpolated linearly, cyclic
    points: list[list[Any]] = field(default_factory=list)
    # where to send brightness: light entity_id / ESPHome REST URL / MQTT topic
    target: str = ""
    # graph colour as #rrggbb; empty = the card picks one automatically
    color: str = ""

    def __post_init__(self) -> None:
        self.validate()

    # -- validation --------------------------------------------------------- #
    def validate(self) -> None:
        """Normalise fields in place; raise ValueError if the schedule is bad."""
        t = {k: parse_hhmm(getattr(self, k)) for k in _TIME_KEYS}
        for key, minutes in t.items():
            setattr(self, key, format_hhmm(minutes))

        try:
            self.min, self.max = int(self.min), int(self.max)
        except (TypeError, ValueError):
            raise ValueError("min and max must be whole numbers") from None
        if not (0 <= self.min <= self.max <= MAX_LEVEL):
            raise ValueError("Need 0 <= min <= max <= 255")
        if self.curve not in CURVES:
            raise ValueError(f"curve must be one of {', '.join(CURVES)}")

        o1 = (t["sunrise_end"] - t["sunrise_start"]) % MINUTES_PER_DAY
        o2 = (t["sunset_start"] - t["sunrise_start"]) % MINUTES_PER_DAY
        o3 = (t["sunset_end"] - t["sunrise_start"]) % MINUTES_PER_DAY
        if not (0 < o1 <= o2 < o3):
            raise ValueError(
                "Times must run in order within 24 h: sunrise start, sunrise "
                "end, sunset start, sunset end (ramps need at least 1 minute)"
            )
        self._t = t

        pts: list[tuple[int, float]] = []
        for raw in self.points or []:
            try:
                when, level = raw[0], float(raw[1])
            except (TypeError, ValueError, IndexError):
                raise ValueError("points must look like [[\"HH:MM\", level], ...]") from None
            if not 0 <= level <= MAX_LEVEL:
                raise ValueError("point levels must be between 0 and 255")
            pts.append((parse_hhmm(when), level))
        pts.sort()
        if len({m for m, _ in pts}) != len(pts):
            raise ValueError("point times must be unique")
        if self.curve == "multi_segment" and len(pts) < 2:
            raise ValueError("multi_segment needs at least 2 points")
        self.points = [[format_hhmm(m), lvl] for m, lvl in pts]
        self._pts = pts
        self.target = (self.target or "").strip()
        self.color = (self.color or "").strip().lower()
        if self.color and not COLOR_RE.match(self.color):
            raise ValueError("color must look like #rrggbb")

    # -- (de)serialisation -------------------------------------------------- #
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChannelSchedule":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in dict(data).items() if k in known})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    # -- evaluation --------------------------------------------------------- #
    def level_at(self, minute: float) -> float:
        """Brightness (0-255, float) at ``minute`` since midnight."""
        minute %= MINUTES_PER_DAY
        value = (
            self._multi_segment(minute)
            if self.curve == "multi_segment"
            else self._ramped(minute)
        )
        return _clamp(value, self.min, self.max)

    def _ramped(self, minute: float) -> float:
        t, span = self._t, self.max - self.min
        p = _progress(minute, t["sunrise_start"], t["sunrise_end"])
        if p is not None:
            return self.min + span * _shape(self.curve, p)
        p = _progress(minute, t["sunset_start"], t["sunset_end"])
        if p is not None:
            return self.max - span * _shape(self.curve, p)
        # Outside both ramps: plateau between sunrise_end and sunset_start,
        # otherwise night.
        day_len = (t["sunset_start"] - t["sunrise_end"]) % MINUTES_PER_DAY
        into_day = (minute - t["sunrise_end"]) % MINUTES_PER_DAY
        return float(self.max if into_day <= day_len else self.min)

    def _multi_segment(self, minute: float) -> float:
        pts = self._pts
        times = [m for m, _ in pts]
        i = bisect_right(times, minute) - 1
        t0, v0 = pts[i] if i >= 0 else (pts[-1][0] - MINUTES_PER_DAY, pts[-1][1])
        t1, v1 = (
            pts[i + 1] if i + 1 < len(pts) else (pts[0][0] + MINUTES_PER_DAY, pts[0][1])
        )
        if t1 == t0:
            return v0
        return v0 + (v1 - v0) * (minute - t0) / (t1 - t0)


# --------------------------------------------------------------------------- #
# moonlight
# --------------------------------------------------------------------------- #
# The eight states of Home Assistant's ``sensor.moon_phase``, in cycle order.
MOON_PHASES = (
    "new_moon",
    "waxing_crescent",
    "first_quarter",
    "waxing_gibbous",
    "full_moon",
    "waning_gibbous",
    "last_quarter",
    "waning_crescent",
)
# Steps away from the new moon: 0 1 2 3 4 3 2 1. Four steps span min -> max, so
# each step is a quarter of the way between the new-moon and full-moon levels.
MOON_STEPS = {phase: min(i, len(MOON_PHASES) - i) for i, phase in enumerate(MOON_PHASES)}
DEFAULT_MOON_SENSOR = "sensor.moon_phase"


def normalize_moon_phase(value: Any) -> str | None:
    """Return a known phase name for a sensor state, or None (unknown/unavailable)."""
    phase = str(value or "").strip().lower().replace(" ", "_").replace("-", "_")
    return phase if phase in MOON_STEPS else None


@dataclass
class MoonlightSchedule:
    """Nightly moonlight for one aquarium (one light, brightness in percent).

    The peak of the night comes from the moon phase: ``min`` at new moon up to
    ``max`` at full moon in four equal steps. Within the nightly window the
    light follows a parabola: off at ``start``, peak at the middle of the
    window, off again at ``end``. The window may cross midnight.
    """

    enabled: bool = False
    sensor: str = DEFAULT_MOON_SENSOR  # entity whose state is the moon phase
    target: str = ""  # HA light entity_id
    start: str = "21:00"
    end: str = "01:00"
    min: int = 10  # percent, at new moon
    max: int = 50  # percent, at full moon

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        self.enabled = bool(self.enabled)
        self.sensor = (self.sensor or "").strip() or DEFAULT_MOON_SENSOR
        self.target = (self.target or "").strip()
        start, end = parse_hhmm(self.start), parse_hhmm(self.end)
        if start == end:
            raise ValueError("Moonlight start and end times must differ")
        self.start, self.end = format_hhmm(start), format_hhmm(end)
        self._start, self._end = start, end
        try:
            self.min, self.max = int(self.min), int(self.max)
        except (TypeError, ValueError):
            raise ValueError("min and max must be whole numbers") from None
        if not (0 <= self.min <= self.max <= 100):
            raise ValueError("Need 0 <= min <= max <= 100 (percent)")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MoonlightSchedule":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in dict(data).items() if k in known})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def in_window(self, minute: float) -> bool:
        return _progress(minute % MINUTES_PER_DAY, self._start, self._end) is not None

    def peak_percent(self, phase: str | None) -> float | None:
        """Brightness (percent) at the middle of the window for ``phase``."""
        steps = MOON_STEPS.get(phase or "")
        if steps is None:
            return None
        return self.min + (self.max - self.min) * steps / 4

    def level_at(self, minute: float, phase: str | None) -> float:
        """Brightness 0-255 (float) at ``minute`` since midnight; 0 outside the window.

        Unknown phase -> 0, and so is a disabled schedule.
        """
        peak = self.peak_percent(phase)
        p = _progress(minute % MINUTES_PER_DAY, self._start, self._end)
        if not self.enabled or peak is None or p is None:
            return 0.0
        return MAX_LEVEL * (peak / 100.0) * 4.0 * p * (1.0 - p)


# --------------------------------------------------------------------------- #
# public API used by the coordinator / websocket layer
# --------------------------------------------------------------------------- #
def compute_brightness(schedule: ChannelSchedule, when: datetime) -> int:
    """Integer brightness 0-255 for ``schedule`` at wall-clock time ``when``."""
    return int(round(schedule.level_at(minute_of_day(when))))


def compute_all(
    schedules: dict[str, ChannelSchedule], when: datetime
) -> dict[str, int]:
    return {name: compute_brightness(s, when) for name, s in schedules.items()}


def preview_curve(schedule: ChannelSchedule, step_minutes: int = 10) -> list[int]:
    """Brightness samples for a full day (0:00 .. 24:00 inclusive)."""
    step = max(1, int(step_minutes))
    return [int(round(schedule.level_at(m))) for m in range(0, MINUTES_PER_DAY + 1, step)]


def preview_moon_curve(
    moonlight: MoonlightSchedule, phase: str | None, step_minutes: int = 10
) -> list[int]:
    """Moonlight brightness samples (0-255) for a full day at one moon phase."""
    step = max(1, int(step_minutes))
    return [
        int(round(moonlight.level_at(m, phase))) for m in range(0, MINUTES_PER_DAY + 1, step)
    ]


def default_points(schedule: ChannelSchedule) -> list[list[Any]]:
    """Seed points that reproduce the schedule's current ramps.

    Used when the curve is switched to ``multi_segment`` from somewhere that
    cannot supply points (e.g. the curve select entity). Levels are sampled from
    the ramp model, so midnight-crossing ramps come out right.
    """
    minutes = {0, *schedule._t.values()}  # noqa: SLF001 - same module
    return [
        [format_hhmm(m), int(round(schedule._ramped(m)))]  # noqa: SLF001
        for m in sorted(minutes)
    ]

"""Tests for the pure brightness engine (no Home Assistant needed).

    pip install pytest
    pytest tests/
"""
import importlib.util
import pathlib
import sys
from datetime import datetime

import pytest

_PATH = (
    pathlib.Path(__file__).parents[1]
    / "custom_components"
    / "aquarium_lighting"
    / "light_controller.py"
)
_spec = importlib.util.spec_from_file_location("light_controller", _PATH)
lc = importlib.util.module_from_spec(_spec)
sys.modules["light_controller"] = lc  # needed by dataclasses
_spec.loader.exec_module(lc)

BASE = {
    "sunrise_start": "08:00",
    "sunrise_end": "11:00",
    "sunset_start": "17:00",
    "sunset_end": "20:00",
    "min": 10,
    "max": 255,
}


def sched(curve="cosine", **over):
    return lc.ChannelSchedule.from_dict({**BASE, "curve": curve, **over})


def at(hhmm, sec=0):
    h, m = map(int, hhmm.split(":"))
    return datetime(2026, 10, 14, h, m, sec)


@pytest.mark.parametrize("curve", ["linear", "cosine", "parabolic"])
def test_night_day_plateaus(curve):
    s = sched(curve)
    assert lc.compute_brightness(s, at("03:00")) == 10
    assert lc.compute_brightness(s, at("08:00")) == 10
    assert lc.compute_brightness(s, at("11:00")) == 255
    assert lc.compute_brightness(s, at("14:00")) == 255
    assert lc.compute_brightness(s, at("17:00")) == 255
    assert lc.compute_brightness(s, at("20:00")) == 10
    assert lc.compute_brightness(s, at("23:59")) == 10


@pytest.mark.parametrize("curve", ["linear", "cosine", "parabolic"])
def test_midpoints_are_halfway(curve):
    s = sched(curve)
    half = (10 + 255) / 2
    assert abs(lc.compute_brightness(s, at("09:30")) - half) <= 1
    assert abs(lc.compute_brightness(s, at("18:30")) - half) <= 1


@pytest.mark.parametrize("curve", ["linear", "cosine", "parabolic"])
def test_ramps_are_monotonic_and_continuous(curve):
    s = sched(curve)
    prev = None
    for m in range(8 * 60, 11 * 60 + 1):  # sunrise
        v = s.level_at(m)
        if prev is not None:
            assert v >= prev - 1e-9
            assert v - prev < 10  # no jumps: ~245 levels over 180 min
        prev = v
    prev = None
    for m in range(17 * 60, 20 * 60 + 1):  # sunset
        v = s.level_at(m)
        if prev is not None:
            assert v <= prev + 1e-9
            assert prev - v < 10
        prev = v


def test_cosine_is_gentler_at_the_ends_than_linear():
    lin, cos = sched("linear"), sched("cosine")
    assert cos.level_at(8 * 60 + 10) < lin.level_at(8 * 60 + 10)


def test_sunset_wraps_past_midnight():
    s = sched(sunset_start="22:00", sunset_end="01:00", sunrise_start="07:00", sunrise_end="09:00")
    assert lc.compute_brightness(s, at("12:00")) == 255
    assert lc.compute_brightness(s, at("21:59")) == 255
    assert abs(lc.compute_brightness(s, at("23:30")) - 132.5) <= 1  # halfway down
    assert lc.compute_brightness(s, at("01:00")) == 10
    assert lc.compute_brightness(s, at("05:00")) == 10


def test_seconds_are_used_so_resume_is_exact():
    s = sched("linear")
    a = s.level_at(lc.minute_of_day(at("09:30", 0)))
    b = s.level_at(lc.minute_of_day(at("09:30", 30)))
    assert b > a


def test_lookahead_tracks_schedule_better_than_sending_current_level():
    """Simulate a device that fades linearly to each pushed level over 60 s.

    Pushing the level for (now + 60 s) lands exactly on the schedule at the next
    tick; pushing the level for "now" makes the lights lag by a minute.
    """
    s = sched("cosine")

    def worst_error(lookahead_min):
        err = 0.0
        for k in range(8 * 60, 11 * 60):  # one tick per minute across sunrise
            start = s.level_at(k - 1 + lookahead_min)  # level reached by previous push
            end = s.level_at(k + lookahead_min)  # level pushed at minute k
            for sec in range(0, 60, 5):
                device = start + (end - start) * sec / 60
                err = max(err, abs(device - s.level_at(k + sec / 60)))
        return err

    assert worst_error(1) < 0.5          # with look-ahead: sub-level error
    assert worst_error(0) > 2 * worst_error(1)  # without: visibly lagging


def test_multi_segment_interpolates_and_wraps():
    s = sched(
        "multi_segment",
        points=[["06:00", 0], ["12:00", 200], ["18:00", 200], ["22:00", 20]],
        min=0,
    )
    assert lc.compute_brightness(s, at("06:00")) == 0
    assert lc.compute_brightness(s, at("09:00")) == 100
    assert lc.compute_brightness(s, at("15:00")) == 200
    # 22:00 (20) -> 06:00 (0) next day: 04:00 is 3/4 of the way
    assert lc.compute_brightness(s, at("04:00")) == 5
    assert lc.compute_brightness(s, at("00:00")) == 15  # 2 h into the 8 h wrap segment


def test_multi_segment_respects_min_max_clamp():
    s = sched("multi_segment", points=[["00:00", 0], ["12:00", 255]], min=50, max=100)
    assert lc.compute_brightness(s, at("00:00")) == 50
    assert lc.compute_brightness(s, at("12:00")) == 100


def test_preview_curve_shape():
    vals = lc.preview_curve(sched(), 10)
    assert len(vals) == 145
    assert vals[0] == vals[-1] == 10
    assert max(vals) == 255


def test_roundtrip_and_normalisation():
    s = lc.ChannelSchedule.from_dict({**BASE, "sunrise_start": "8:00", "target": " light.x "})
    d = s.to_dict()
    assert d["sunrise_start"] == "08:00" and d["target"] == "light.x"
    assert lc.ChannelSchedule.from_dict(d).to_dict() == d


@pytest.mark.parametrize(
    "bad",
    [
        {"sunrise_start": "25:00"},
        {"sunrise_start": "banana"},
        {"min": 200, "max": 100},
        {"max": 300},
        {"curve": "wobbly"},
        {"sunrise_end": "07:00", "sunrise_start": "08:00", "sunset_start": "17:00", "sunset_end": "20:00"},
        {"sunset_start": "10:00"},  # sunset before sunrise ends
        {"curve": "multi_segment", "points": [["08:00", 10]]},
        {"curve": "multi_segment", "points": [["08:00", 10], ["08:00", 20]]},
    ],
)
def test_validation_rejects(bad):
    with pytest.raises(ValueError):
        lc.ChannelSchedule.from_dict({**BASE, **bad})


def test_channel_names():
    assert lc.validate_channel_name("warm_white") == "warm_white"
    for bad in ("", "Blue", "a b", "x" * 33):
        with pytest.raises(ValueError):
            lc.validate_channel_name(bad)


def test_channels_are_independent():
    blue = sched("cosine")
    uv = sched("linear", sunrise_start="09:00", sunrise_end="12:00", min=0, max=128)
    out = lc.compute_all({"blue": blue, "uv": uv}, at("08:30"))
    assert out["uv"] == 0 and out["blue"] > 10


def test_color_is_normalised_and_validated():
    assert sched(color="#FFAA00").color == "#ffaa00"
    assert sched(color="").color == ""
    assert sched().to_dict()["color"] == ""
    for bad in ("red", "#fff", "#gggggg", "ffaa00"):
        with pytest.raises(ValueError):
            sched(color=bad)


@pytest.mark.parametrize("curve", ["linear", "cosine", "parabolic"])
def test_default_points_reproduce_the_ramps(curve):
    """Switching to multi_segment (e.g. from the select entity) keeps the shape."""
    base = sched(curve)
    seeded = lc.ChannelSchedule.from_dict({**base.to_dict(), "curve": "multi_segment", "points": lc.default_points(base)})
    for hhmm in ("03:00", "08:00", "11:00", "14:00", "17:00", "20:00", "23:00"):
        # plateaus and ramp endpoints match exactly
        assert lc.compute_brightness(seeded, at(hhmm)) == lc.compute_brightness(base, at(hhmm))


def test_default_points_handle_midnight_crossing_ramp():
    base = sched("linear", sunset_start="22:00", sunset_end="01:00", sunrise_start="07:00", sunrise_end="09:00")
    pts = lc.default_points(base)
    assert pts[0][0] == "00:00"
    assert 10 < pts[0][1] < 255  # midnight is part-way down the sunset ramp, not "min"
    seeded = lc.ChannelSchedule.from_dict({**base.to_dict(), "curve": "multi_segment", "points": pts})
    assert abs(lc.compute_brightness(seeded, at("23:30")) - lc.compute_brightness(base, at("23:30"))) <= 1


# --------------------------------------------------------------------------- #
# moonlight
# --------------------------------------------------------------------------- #
def moon(**over):
    base = {"enabled": True, "target": "light.moon", "start": "21:00", "end": "01:00", "min": 10, "max": 50}
    return lc.MoonlightSchedule.from_dict({**base, **over})


def minutes(hhmm):
    return lc.parse_hhmm(hhmm)


def test_moon_peak_rises_in_four_equal_steps_and_is_symmetric():
    m = moon()
    peaks = {p: m.peak_percent(p) for p in lc.MOON_PHASES}
    assert peaks["new_moon"] == 10
    assert peaks["waxing_crescent"] == 20  # (50-10)/4 + 10
    assert peaks["first_quarter"] == 30
    assert peaks["waxing_gibbous"] == 40
    assert peaks["full_moon"] == 50
    # waning side mirrors the waxing side
    assert peaks["waning_gibbous"] == 40
    assert peaks["last_quarter"] == 30
    assert peaks["waning_crescent"] == 20
    assert m.peak_percent("nonsense") is None and m.peak_percent(None) is None


def test_moon_worked_example_parabola_across_midnight():
    # 21:00-01:00, one quarter step above new moon: peak 20% = 51/255 at 23:00.
    m = moon()
    lvl = lambda hhmm: m.level_at(minutes(hhmm), "waxing_crescent")
    assert lvl("20:59") == 0  # before the window
    assert lvl("21:00") == 0  # starts from off
    assert lvl("23:00") == pytest.approx(255 * 0.20)  # peak in the middle
    assert lvl("22:00") == pytest.approx(255 * 0.20 * 0.75)  # parabola, not linear
    assert lvl("00:00") == pytest.approx(255 * 0.20 * 0.75)  # symmetric
    assert lvl("01:00") == pytest.approx(0, abs=1e-9)  # back to off at the end
    assert lvl("01:01") == 0 and lvl("12:00") == 0
    assert lvl("22:00") == pytest.approx(lvl("00:00"))


def test_moon_first_quarter_is_two_steps_not_one():
    # The 8-state sensor: first_quarter is 2 steps from new moon -> 30% here.
    m = moon()
    assert m.level_at(minutes("23:00"), "first_quarter") == pytest.approx(255 * 0.30)


def test_moon_window_inside_one_day():
    m = moon(start="20:00", end="22:00")
    assert m.in_window(minutes("21:00")) and not m.in_window(minutes("23:00"))
    assert m.level_at(minutes("21:00"), "full_moon") == pytest.approx(255 * 0.5)


def test_moon_disabled_or_unknown_phase_is_dark():
    assert moon(enabled=False).level_at(minutes("23:00"), "full_moon") == 0
    assert moon().level_at(minutes("23:00"), None) == 0


def test_moon_validation():
    with pytest.raises(ValueError):
        moon(start="21:00", end="21:00")
    with pytest.raises(ValueError):
        moon(min=60, max=50)
    with pytest.raises(ValueError):
        moon(max=101)
    with pytest.raises(ValueError):
        moon(start="25:00")
    assert moon(sensor="").sensor == lc.DEFAULT_MOON_SENSOR  # empty -> default
    assert moon(start="21:00:00").start == "21:00"  # time selector format


def test_moon_round_trip_and_phase_normalising():
    m = moon()
    assert lc.MoonlightSchedule.from_dict(m.to_dict()) == m
    assert lc.normalize_moon_phase("Waxing Crescent") == "waxing_crescent"
    assert lc.normalize_moon_phase("unavailable") is None
    assert lc.normalize_moon_phase(None) is None

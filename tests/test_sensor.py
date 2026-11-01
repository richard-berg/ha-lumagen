"""Tests for the sensor platform's state wiring.

These exercise the description ``value_fn``s and display helpers directly
against a constructed ``LumagenState``. No ``hass`` fixture and no transport:
protocol behaviour is covered upstream in aiolumagen, so what's worth pinning
here is that each entity reads the field we think it reads.
"""

from __future__ import annotations

import pytest
from aiolumagen import AutoAspectStatus, LumagenState, SourceMode

from custom_components.lumagen.sensor import (
    SENSORS,
    _active_outputs_label,
    _resolution_label,
)

_BY_KEY = {description.key: description for description in SENSORS}


def test_sensor_keys_are_unique() -> None:
    """Duplicate keys would collide on unique_id and silently drop an entity."""
    keys = [description.key for description in SENSORS]
    assert len(keys) == len(set(keys))


# ---------- Refresh rate: the correctness fix ----------


def test_refresh_rate_sensors_read_the_derived_hz_field() -> None:
    """Regression: these used to report a 59.94 Hz signal as 59.0 Hz.

    The wire codes (RRR/PPP) are the *truncated* integer part of the rate, so
    the old ``float("059")`` produced 59.0. aiolumagen now derives the real
    value and the sensors read that field instead.
    """
    state = LumagenState(
        source_vrate="059",
        output_vrate="060",
        source_refresh_hz=59.94,
        output_refresh_hz=60.0,
    )
    assert _BY_KEY["source_vrate"].value_fn(state) == pytest.approx(59.94)
    assert _BY_KEY["output_vrate"].value_fn(state) == pytest.approx(60.0)


def test_refresh_rate_is_none_before_first_observation() -> None:
    assert _BY_KEY["source_vrate"].value_fn(LumagenState()) is None
    assert _BY_KEY["output_vrate"].value_fn(LumagenState()) is None


# ---------- Combined resolution label ----------


@pytest.mark.parametrize(
    ("vertical", "width", "mode", "expected"),
    [
        ("2160", 3840, SourceMode.PROGRESSIVE, "3840x2160p"),
        ("1080", 1920, SourceMode.INTERLACED, "1920x1080i"),
        ("2160", 5119, SourceMode.PROGRESSIVE, "5119x2160p"),
        # No width derived (aspect not reported yet) — still useful.
        ("1080", None, SourceMode.PROGRESSIVE, "1080p"),
        # No scan mode yet — drop the suffix rather than guessing.
        ("2160", 3840, None, "3840x2160"),
        # "No input" scan modes carry no meaningful suffix.
        ("2160", 3840, SourceMode.NO_INPUT, "3840x2160"),
        ("2160", 3840, SourceMode.NO_INPUT_V5, "3840x2160"),
    ],
)
def test_resolution_label(
    vertical: str, width: int | None, mode: SourceMode | None, expected: str
) -> None:
    assert _resolution_label(vertical, width, mode) == expected


@pytest.mark.parametrize("vertical", [None, "", "abc", "0000", "0", "-1"])
def test_resolution_label_none_without_usable_height(vertical: str | None) -> None:
    """``0000`` is the device's no-signal placeholder, not a real height."""
    assert _resolution_label(vertical, 3840, SourceMode.PROGRESSIVE) is None


def test_resolution_sensors_wire_source_and_output_independently() -> None:
    """Each path must read its own height/width/mode trio, not the other's."""
    state = LumagenState(
        source_resolution="1080",
        source_width=1920,
        source_mode=SourceMode.INTERLACED,
        output_resolution="2160",
        output_width=3840,
        output_scan_mode=SourceMode.PROGRESSIVE,
    )
    assert _BY_KEY["source_resolution_full"].value_fn(state) == "1920x1080i"
    assert _BY_KEY["output_resolution_full"].value_fn(state) == "3840x2160p"


# ---------- Active outputs ----------


def test_active_outputs_label_lists_enabled_outputs() -> None:
    assert _active_outputs_label((2, 3, 4)) == "2, 3, 4"
    assert _active_outputs_label((1,)) == "1"


def test_active_outputs_label_distinguishes_all_off_from_unobserved() -> None:
    """Empty tuple is a real state; None means we haven't seen a status line."""
    assert _active_outputs_label(()) == "None"
    assert _active_outputs_label(None) is None


# ---------- Extended !I24/!I25 fields ----------


def test_extended_field_sensors_read_their_state_fields() -> None:
    state = LumagenState(
        physical_input="02",
        detected_source_aspect="178",
        detected_content_aspect="235",
        output_aspect="237",
        output_cms=2,
        output_style=1,
        input_config="0",
        active_outputs=(2, 3, 4),
    )
    assert _BY_KEY["physical_input"].value_fn(state) == "02"
    assert _BY_KEY["detected_source_aspect"].value_fn(state) == "178"
    assert _BY_KEY["detected_content_aspect"].value_fn(state) == "235"
    assert _BY_KEY["output_aspect"].value_fn(state) == "237"
    assert _BY_KEY["output_cms"].value_fn(state) == 2
    assert _BY_KEY["output_style"].value_fn(state) == 1
    assert _BY_KEY["input_config"].value_fn(state) == "0"
    assert _BY_KEY["active_outputs"].value_fn(state) == "2, 3, 4"


def test_detected_aspect_can_differ_from_applied_aspect() -> None:
    """The pair exists precisely so a manual-override mismatch is visible."""
    state = LumagenState(content_aspect="178", detected_content_aspect="235")
    assert _BY_KEY["content_aspect"].value_fn(state) == "178"
    assert _BY_KEY["detected_content_aspect"].value_fn(state) == "235"


# ---------- Enum sensors ----------


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (SourceMode.INTERLACED, "interlaced"),
        (SourceMode.PROGRESSIVE, "progressive"),
        (SourceMode.NO_INPUT, "no_input"),
        (SourceMode.NO_INPUT_V5, "no_input"),
        (None, None),
    ],
)
@pytest.mark.parametrize("key", ["source_mode", "output_scan_mode"])
def test_scan_mode_reports_state_key_and_declares_all_options(
    key: str, mode: SourceMode | None, expected: str | None
) -> None:
    description = _BY_KEY[key]
    assert description.value_fn(LumagenState(**{key: mode})) == expected
    # Every state the sensor can report must be declared, or HA logs the state
    # as invalid for the entity.
    assert description.options == ["interlaced", "progressive", "no_input"]


def test_auto_aspect_status_reports_enum_value_and_declares_all_options() -> None:
    description = _BY_KEY["auto_aspect_status"]
    assert (
        description.value_fn(LumagenState(auto_aspect_status=AutoAspectStatus.DISABLED))
        == "disabled"
    )
    assert set(description.options or []) == {"off", "disabled", "on"}


def test_auto_aspect_status_unknown_on_firmware_that_omits_it() -> None:
    """The field is absent on firmware 030225, so the sensor reads unknown.

    On such firmware aiolumagen falls back to polling ZQI54, so the auto_aspect
    switch still works; only this diagnostic sensor goes unknown.
    """
    assert _BY_KEY["auto_aspect_status"].value_fn(LumagenState()) is None

"""Tests for the aspect-ratio mapping helpers in select.py.

The Lumagen doesn't report which aspect preset is currently selected;
we snap the detected content aspect to the closest preset label as a
best-effort display. These tests pin that mapping.
"""

from __future__ import annotations

import pytest
from aiolumagen import LumagenState, SubtitleShift

from custom_components.lumagen.select import (
    _SUBTITLE_SHIFT_TO_LEVEL,
    _SUBTITLE_SHIFT_WIRE_TO_LABEL,
    _closest_aspect_label,
    _current_subtitle_shift,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("133", "4:3"),  # exactly 1.33
        ("178", "16:9"),  # exactly 1.78
        ("185", "1.85"),  # exactly 1.85
        ("235", "2.35"),  # exactly 2.35
        ("240", "2.40"),  # exactly 2.40
        ("190", "1.90"),  # exactly 1.90
        ("200", "2.00"),  # exactly 2.00
        ("210", "2.10"),  # exactly 2.10
        ("220", "2.20"),  # exactly 2.20
        ("255", "2.55"),  # exactly 2.55
        ("276", "2.76"),  # exactly 2.76
        ("180", "16:9"),  # between 1.78 and 1.85 — closer to 16:9
        ("238", "2.40"),  # between 2.35 and 2.40 — closer to 2.40
        ("175", "16:9"),  # slightly under 1.78 still rounds to 16:9
        # 250 lands between 2.40 and 2.55 and is nearer the latter. It used to
        # read 2.40 simply because that was the widest ratio offered; now that
        # the full set is selectable, snapping upward is the correct answer.
        ("250", "2.55"),
        ("300", "2.76"),  # above the widest ratio clamps to the top entry
        ("100", "4:3"),  # well below 1.33 clamps to the bottom entry
    ],
)
def test_closest_aspect_label(raw: str, expected: str) -> None:
    assert _closest_aspect_label(raw) == expected


def test_every_numeric_aspect_option_is_reachable_from_a_reported_value() -> None:
    """The dropdown and the reverse lookup must agree on the numeric ratios.

    A selectable ratio with no entry in the reverse table would snap straight to
    a neighbour on read-back: pick 2.10 and the UI would show 2.00. Letterbox
    and 16:9 NZ are excluded because they're framing variants sharing a detected
    aspect with their base ratio, so no reported value can identify them.
    """
    from custom_components.lumagen.select import (
        _ASPECT_COMMANDS,
        _CONTENT_ASPECT_TO_LABEL,
    )

    framing_variants = {"Letterbox", "16:9 NZ"}
    numeric_options = set(_ASPECT_COMMANDS) - framing_variants
    reachable = {label for _code, label in _CONTENT_ASPECT_TO_LABEL}
    assert numeric_options == reachable


@pytest.mark.parametrize("raw", [None, "", "abc", "not-a-number"])
def test_closest_aspect_label_handles_garbage(raw: str | None) -> None:
    assert _closest_aspect_label(raw) is None


# ---------- Subtitle shift read-back ----------
#
# This select was optimistic-only because the Lumagen has no ZQ query for
# subtitle shift. The Full v5 status push appears to carry it, so the dropdown
# can now reflect a change made with the device's own remote — but only where
# the firmware appends that field, hence the fallback.


def test_current_subtitle_shift_maps_device_value_to_label() -> None:

    assert _current_subtitle_shift(LumagenState(subtitle_shift=SubtitleShift.OFF)) == "off"
    assert _current_subtitle_shift(LumagenState(subtitle_shift=SubtitleShift.PERCENT_3)) == "small"
    assert _current_subtitle_shift(LumagenState(subtitle_shift=SubtitleShift.PERCENT_6)) == "large"


def test_current_subtitle_shift_falls_back_when_device_is_silent() -> None:
    """None keeps the optimistic value — the pre-existing behaviour.

    Firmware 030225 doesn't report this field, so returning None here is the
    common case, not the edge case. Getting it wrong would regress the
    dropdown to permanently "unknown" for most users.
    """

    assert _current_subtitle_shift(LumagenState()) is None


def test_subtitle_shift_labels_agree_between_read_and_write_paths() -> None:
    """The read map and the write map must cover the same option set.

    A mismatch would let the device report a label the dropdown can't select
    (or vice versa), which surfaces as a silently blank select.
    """

    assert set(_SUBTITLE_SHIFT_WIRE_TO_LABEL.values()) == set(_SUBTITLE_SHIFT_TO_LEVEL)

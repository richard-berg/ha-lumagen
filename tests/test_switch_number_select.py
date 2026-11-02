"""Tests for the new switch / number / select entities (Phase 1).

These exercise the wiring between HA entity callbacks and the aiolumagen
client. We build a minimal fake LumagenState + AsyncMock client and call
the dispatch functions directly — entity-platform integration is exercised
via test_full_setup below, but per-feature behavior is easier to pin by
calling the helpers.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiolumagen import LumagenState
from aiolumagen.state import SharpnessSensitivity

from custom_components.lumagen.coordinator import LumagenCoordinator
from custom_components.lumagen.number import _set_fan_speed
from custom_components.lumagen.select import (
    _closest_aspect_label,
    _current_sharpness_sensitivity,
    _select_subtitle_shift,
)
from custom_components.lumagen.switch import _set_game_mode

# ---------- Sharpness compound write (centralized on the coordinator) ----------
#
# ZY521 sends enabled + level + sensitivity together, so changing one field
# has to re-send the other two. That merge lives on the coordinator so all
# three entities share one source of truth; these tests exercise it directly.


def _sharpness_coordinator(state: LumagenState | None = None) -> LumagenCoordinator:
    """Build a coordinator with just the fields the sharpness path touches."""
    coord = LumagenCoordinator.__new__(LumagenCoordinator)
    client = MagicMock()
    client.set_sharpness = AsyncMock()
    coord.client = client
    coord.data = state if state is not None else LumagenState()
    client.state = coord.data
    coord._last_sharpness_enabled = None
    coord._last_sharpness_level = None
    coord._last_sharpness_sensitivity = None
    coord._sharpness_report_generation = None
    coord._sharpness_lock = asyncio.Lock()
    coord.firmware_update_active = False
    return coord


async def test_set_sharpness_level_preserves_enabled_and_sensitivity() -> None:
    coord = _sharpness_coordinator(
        LumagenState(
            sharpness_enabled=True,
            sharpness_level=2,
            sharpness_sensitivity=SharpnessSensitivity.HIGH,
        )
    )
    await coord.async_set_sharpness(level=7)
    coord.client.set_sharpness.assert_awaited_once_with(enabled=True, level=7, sensitivity="H")


async def test_set_sharpness_sensitivity_preserves_enabled_and_level() -> None:
    coord = _sharpness_coordinator(
        LumagenState(
            sharpness_enabled=True,
            sharpness_level=6,
            sharpness_sensitivity=SharpnessSensitivity.NORMAL,
        )
    )
    await coord.async_set_sharpness(sensitivity="H")
    coord.client.set_sharpness.assert_awaited_once_with(enabled=True, level=6, sensitivity="H")


async def test_set_sharpness_enabled_preserves_level_and_sensitivity() -> None:
    coord = _sharpness_coordinator(
        LumagenState(
            sharpness_enabled=False,
            sharpness_level=5,
            sharpness_sensitivity=SharpnessSensitivity.HIGH,
        )
    )
    await coord.async_set_sharpness(enabled=True)
    coord.client.set_sharpness.assert_awaited_once_with(enabled=True, level=5, sensitivity="H")


async def test_sharpness_writes_remember_each_other_without_device_readback() -> None:
    """The core regression: setting the level must not reset sensitivity.

    With no ZQI30 readback (unsupported firmware, or the window before the
    first reply), each entity used to default the fields it didn't own — so
    choosing High sensitivity and then moving the level slider silently
    reverted sensitivity to Normal.
    """
    coord = _sharpness_coordinator()  # device state entirely unknown

    await coord.async_set_sharpness(sensitivity="H")
    await coord.async_set_sharpness(level=6)

    assert coord.client.set_sharpness.await_args_list[-1].kwargs == {
        "enabled": False,
        "level": 6,
        "sensitivity": "H",  # preserved, not reverted to "N"
    }


async def test_sharpness_defaults_when_nothing_known() -> None:
    """Moving the slider on a fresh state must not silently enable sharpening."""
    coord = _sharpness_coordinator()
    await coord.async_set_sharpness(level=3)
    coord.client.set_sharpness.assert_awaited_once_with(enabled=False, level=3, sensitivity="N")


def test_effective_sharpness_prefers_device_state_over_last_written() -> None:
    """Device readback wins once it lands, even if we wrote something else."""
    coord = _sharpness_coordinator(
        LumagenState(
            sharpness_enabled=True,
            sharpness_level=1,
            sharpness_sensitivity=SharpnessSensitivity.HIGH,
        )
    )
    coord._last_sharpness_level = 7
    assert coord.effective_sharpness() == (True, 1, "H")


# ---------- Switch wiring ----------


async def test_switch_set_game_mode_dispatches_to_client() -> None:
    client = MagicMock()
    client.set_game_mode = AsyncMock()
    await _set_game_mode(client, LumagenState(), True)
    client.set_game_mode.assert_awaited_once_with(True)


# ---------- Number wiring ----------


async def test_number_sharpness_level_routes_through_coordinator() -> None:
    """The slider must use the compound merge, not write the triple itself."""
    from custom_components.lumagen.number import NUMBERS, LumagenNumber

    description = next(d for d in NUMBERS if d.key == "sharpness_level")
    coord = MagicMock()
    coord.async_set_sharpness = AsyncMock()

    entity = LumagenNumber.__new__(LumagenNumber)
    entity.coordinator = coord
    entity.entity_description = description
    entity._optimistic_value = None
    entity.async_write_ha_state = MagicMock()

    await entity.async_set_native_value(6)

    coord.async_set_sharpness.assert_awaited_once_with(level=6)


async def test_switch_sharpness_enabled_routes_through_coordinator() -> None:
    from custom_components.lumagen.switch import SWITCHES, LumagenSwitch

    description = next(d for d in SWITCHES if d.key == "sharpness_enabled")
    coord = MagicMock()
    coord.async_set_sharpness = AsyncMock()

    entity = LumagenSwitch.__new__(LumagenSwitch)
    entity.coordinator = coord
    entity.entity_description = description
    entity.async_write_ha_state = MagicMock()

    await entity.async_turn_on()
    coord.async_set_sharpness.assert_awaited_once_with(enabled=True)

    coord.async_set_sharpness.reset_mock()
    await entity.async_turn_off()
    coord.async_set_sharpness.assert_awaited_once_with(enabled=False)


async def test_select_sharpness_sensitivity_routes_through_coordinator() -> None:
    from custom_components.lumagen.select import SELECTS, LumagenSelect

    description = next(d for d in SELECTS if d.key == "sharpness_sensitivity")
    coord = MagicMock()
    coord.async_set_sharpness = AsyncMock()

    entity = LumagenSelect.__new__(LumagenSelect)
    entity.coordinator = coord
    entity.entity_description = description
    entity._optimistic_option = None
    entity.async_write_ha_state = MagicMock()

    await entity.async_select_option("high")
    coord.async_set_sharpness.assert_awaited_once_with(sensitivity="H")

    coord.async_set_sharpness.reset_mock()
    await entity.async_select_option("Bogus")
    coord.async_set_sharpness.assert_not_called()


def test_sharpness_entities_share_one_entity_category() -> None:
    """Enable / level / sensitivity must not be split across UI sections."""
    from custom_components.lumagen.number import NUMBERS
    from custom_components.lumagen.select import SELECTS
    from custom_components.lumagen.switch import SWITCHES

    categories = {
        next(d for d in SWITCHES if d.key == "sharpness_enabled").entity_category,
        next(d for d in NUMBERS if d.key == "sharpness_level").entity_category,
        next(d for d in SELECTS if d.key == "sharpness_sensitivity").entity_category,
    }
    assert len(categories) == 1, f"sharpness entities disagree: {categories}"


async def test_number_set_fan_speed_dispatches_to_client() -> None:
    client = MagicMock()
    client.set_fan_speed = AsyncMock()
    await _set_fan_speed(client, LumagenState(), 4)
    # Passed through in device units — aiolumagen owns the wire conversion,
    # so the integration must NOT pre-adjust the value here.
    client.set_fan_speed.assert_awaited_once_with(4)


def test_fan_speed_slider_matches_device_menu_range() -> None:
    """1-10, matching what the Lumagen displays.

    Regression: the slider was 0-9 (the raw wire range), so every value the
    user picked showed up one higher on the device.
    """
    from custom_components.lumagen.number import NUMBERS

    description = next(d for d in NUMBERS if d.key == "fan_speed")
    assert description.native_min_value == 1
    assert description.native_max_value == 10


# ---------- Select wiring ----------


async def test_select_subtitle_shift_off_small_large_dispatch() -> None:
    client = MagicMock()
    client.set_subtitle_shift = AsyncMock()

    await _select_subtitle_shift(client, LumagenState(), "off")
    await _select_subtitle_shift(client, LumagenState(), "small")
    await _select_subtitle_shift(client, LumagenState(), "large")

    assert [c.args for c in client.set_subtitle_shift.await_args_list] == [(0,), (1,), (2,)]


async def test_select_subtitle_shift_unknown_label_is_no_op() -> None:
    client = MagicMock()
    client.set_subtitle_shift = AsyncMock()
    await _select_subtitle_shift(client, LumagenState(), "Bogus")
    client.set_subtitle_shift.assert_not_called()


@pytest.mark.parametrize(
    ("sensitivity", "expected"),
    [
        (SharpnessSensitivity.NORMAL, "normal"),
        (SharpnessSensitivity.HIGH, "high"),
        (None, None),
    ],
)
def test_current_sharpness_sensitivity_label(
    sensitivity: SharpnessSensitivity | None, expected: str | None
) -> None:
    assert (
        _current_sharpness_sensitivity(LumagenState(sharpness_sensitivity=sensitivity)) == expected
    )


# Sanity that the existing aspect helper still passes (regression after edits).
def test_closest_aspect_label_still_works() -> None:
    assert _closest_aspect_label("178") == "16:9"
    assert _closest_aspect_label("240") == "2.40"


# ---------- Phase 2 HDR mapping (compound write via coordinator) ----------


def _coordinator_with(client: MagicMock) -> LumagenCoordinator:
    """Build a minimal coordinator stub with the HDR optimistic fields."""
    coord = LumagenCoordinator.__new__(LumagenCoordinator)
    coord.client = client
    coord.data = LumagenState()
    coord.hdr_mapping_max_nits = 0
    coord.hdr_mapping_gamma_mode = "A"
    coord._hdr_mapping_lock = asyncio.Lock()
    coord.firmware_update_active = False
    coord.async_update_listeners = MagicMock()
    return coord


async def test_hdr_mapping_max_nits_set_preserves_gamma_mode() -> None:
    """Changing the nits writes ZY417 with the *current* gamma_mode preserved."""
    from custom_components.lumagen.number import NUMBERS, LumagenNumber

    description = next(d for d in NUMBERS if d.key == "hdr_mapping_max_nits")
    client = MagicMock()
    client.set_hdr_intensity_mapping = AsyncMock()
    coord = _coordinator_with(client)
    coord.hdr_mapping_gamma_mode = "H"  # user previously chose force-HDR

    entity = LumagenNumber.__new__(LumagenNumber)
    entity.coordinator = coord
    entity.entity_description = description
    entity._optimistic_value = None
    entity.async_write_ha_state = MagicMock()

    await entity.async_set_native_value(1500)

    client.set_hdr_intensity_mapping.assert_awaited_once_with(display_max_nits=1500, gamma_mode="H")
    # Coordinator's optimistic state updates so subsequent reads reflect it.
    assert coord.hdr_mapping_max_nits == 1500


async def test_hdr_gamma_mode_set_preserves_max_nits() -> None:
    """Changing the gamma mode writes ZY417 with the *current* max_nits preserved."""
    from custom_components.lumagen.select import SELECTS, LumagenSelect

    description = next(d for d in SELECTS if d.key == "hdr_gamma_mode")
    client = MagicMock()
    client.set_hdr_intensity_mapping = AsyncMock()
    coord = _coordinator_with(client)
    coord.hdr_mapping_max_nits = 1000  # user previously set 1000 nits

    entity = LumagenSelect.__new__(LumagenSelect)
    entity.coordinator = coord
    entity.entity_description = description
    entity._optimistic_option = None
    entity.async_write_ha_state = MagicMock()

    await entity.async_select_option("hdr")

    client.set_hdr_intensity_mapping.assert_awaited_once_with(display_max_nits=1000, gamma_mode="H")
    assert coord.hdr_mapping_gamma_mode == "H"


async def test_hdr_gamma_mode_unknown_label_is_no_op() -> None:
    """A label outside the documented Auto/HDR/SDR set must not write."""
    from custom_components.lumagen.select import SELECTS, LumagenSelect

    description = next(d for d in SELECTS if d.key == "hdr_gamma_mode")
    client = MagicMock()
    client.set_hdr_intensity_mapping = AsyncMock()
    coord = _coordinator_with(client)

    entity = LumagenSelect.__new__(LumagenSelect)
    entity.coordinator = coord
    entity.entity_description = description
    entity._optimistic_option = None
    entity.async_write_ha_state = MagicMock()

    await entity.async_select_option("Bogus")

    client.set_hdr_intensity_mapping.assert_not_called()


async def test_auto_aspect_switch_sends_command_without_a_follow_up_query() -> None:
    """Auto aspect rides the Full v5 push, so no confirming query is needed.

    Regression guard. This used to call ``query_auto_aspect()`` after the write,
    because auto aspect was believed to be absent from the push stream. It isn't:
    payload index 26 of ``!I25`` carries it, the device emits an unsolicited
    ``!I25`` on every auto-aspect change, and aiolumagen v0.10.0 feeds
    ``state.auto_aspect`` from that index. The query was redundant work that also
    arrived later than the push it duplicated.

    Unlike game mode, which genuinely has no push equivalent and still queries.
    """
    from unittest.mock import AsyncMock, MagicMock

    from custom_components.lumagen.switch import SWITCHES

    description = next(d for d in SWITCHES if d.key == "auto_aspect")
    assert description.set_fn is not None

    client = MagicMock()
    client.send_command = AsyncMock()
    client.query_auto_aspect = AsyncMock()

    await description.set_fn(client, LumagenState(), True)
    client.send_command.assert_awaited_once()
    assert client.send_command.await_args.kwargs.get("refresh") is False
    client.query_auto_aspect.assert_not_awaited()

    client.send_command.reset_mock()
    await description.set_fn(client, LumagenState(), False)
    on_cmd = client.send_command.await_args.args[0]
    client.send_command.reset_mock()
    await description.set_fn(client, LumagenState(), True)
    off_cmd = client.send_command.await_args.args[0]
    assert on_cmd != off_cmd, "enable and disable must send different commands"

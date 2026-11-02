"""Remote for the Lumagen Radiance Pro.

Exposes the Lumagen's command surface as a Home Assistant ``remote`` entity —
the idiomatic home for OSD navigation (Menu / OK / arrows / Exit) and any
one-shot command, driven from automations via ``remote.send_command``.

``send_command`` accepts friendly command names (see ``_COMMANDS``) and, for
anything not in that map, falls through to sending the token as a raw wire
command. So ``remote.send_command(command="menu")`` and
``remote.send_command(command=["down", "down", "ok"])`` both work, as does a
raw ``remote.send_command(command="i3")``. ``num_repeats`` and ``delay_secs``
are honored, so "press down three times" is a single call.

This mirrors the media_player + remote dual-entity pattern used by Apple TV /
Android TV: the media_player owns power + source, the remote owns command
sending. Power is exposed on both (turn_on/turn_off here map to the same
``%`` / ``$`` commands) so either entity can wake the device.

Raw pass-through sends *without* a carriage return. Commands that require a CR
(most ``ZY…`` calibration commands) should use the ``send_raw_command``
service, which takes an explicit ``cr`` flag — the remote deliberately doesn't
guess at CR handling.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any

from aiolumagen import Aspect, Input, Memory, Misc, Navigation, Power
from homeassistant.components.remote import (
    ATTR_DELAY_SECS,
    ATTR_NUM_REPEATS,
    DEFAULT_DELAY_SECS,
    DEFAULT_NUM_REPEATS,
    RemoteEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import LumagenConfigEntry, LumagenCoordinator, lumagen_command
from .entity import LumagenBaseEntity

# Friendly command name -> Lumagen wire command (no CR). Wire values come
# from aiolumagen's command enums, which are the single source of truth for
# them — do not re-type the literals here. Anything not listed is sent
# verbatim, so the remote doubles as an escape hatch for one-shot commands
# the integration doesn't name (CR-terminated commands excepted — use the
# send_raw_command service for those). That pass-through is also why a
# missing entry is dangerous rather than merely absent: the friendly name
# itself would go out as ASCII, so keep this table generated from the enums.
_COMMANDS: dict[str, str] = {
    # Power
    "power_on": Power.ON,
    "standby": Power.STANDBY,
    # OSD navigation
    "menu": Navigation.MENU,
    "exit": Navigation.EXIT,
    "ok": Navigation.OK,
    "menu_off": Navigation.MENU_OFF,
    "up": Navigation.UP,
    "down": Navigation.DOWN,
    "left": Navigation.LEFT,
    "right": Navigation.RIGHT,
    # Direct inputs
    "input_1": Input.INPUT_1,
    "input_2": Input.INPUT_2,
    "input_3": Input.INPUT_3,
    "input_4": Input.INPUT_4,
    "input_5": Input.INPUT_5,
    "input_6": Input.INPUT_6,
    "input_7": Input.INPUT_7,
    "input_8": Input.INPUT_8,
    "previous_input": Input.PREVIOUS,
    # Aspect ratio
    "aspect_4_3": Aspect.RATIO_4_3,
    "aspect_letterbox": Aspect.LETTERBOX,
    "aspect_16_9": Aspect.RATIO_16_9,
    "aspect_16_9_nz": Aspect.RATIO_16_9_NZ,
    "aspect_1_85": Aspect.RATIO_1_85,
    "aspect_1_90": Aspect.RATIO_1_90,
    "aspect_2_00": Aspect.RATIO_2_00,
    "aspect_2_10": Aspect.RATIO_2_10,
    "aspect_2_20": Aspect.RATIO_2_20,
    "aspect_2_35": Aspect.RATIO_2_35,
    "aspect_2_40": Aspect.RATIO_2_40,
    "aspect_2_55": Aspect.RATIO_2_55,
    "aspect_2_76": Aspect.RATIO_2_76,
    "auto_aspect_on": Aspect.AUTO_ENABLE,
    "auto_aspect_off": Aspect.AUTO_DISABLE,
    # Non-linear stretch is a modifier, not a mode: send an aspect first, then
    # this. As a command list that's one call —
    # command=["aspect_4_3", "nls"].
    "nls": Aspect.NLS,
    # Memory
    "memory_a": Memory.A,
    "memory_b": Memory.B,
    "memory_c": Memory.C,
    "memory_d": Memory.D,
    # Misc
    "hdr_setup": Misc.HDR_SETUP,
    "test_pattern": Misc.TEST_PATTERN,
    "osd_on": Misc.OSD_ON,
    "osd_off": Misc.OSD_OFF,
    "zone": Misc.ZONE,
    # Menu-context keys. "help" needs the menu open and an item highlighted;
    # "alt" prefixes the key it modifies, so it only makes sense in a list —
    # command=["alt", "aspect_2_35"].
    "help": Navigation.HELP,
    "alt": Navigation.ALT,
}
# Deliberately absent: show_aspect and save. Both need a CR terminator and
# this entity sends without one (see the module docstring), so a friendly name
# here would produce a command the Lumagen silently ignores. They're buttons
# instead, where the CR is handled by the client method.


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LumagenConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities([LumagenRemote(coordinator)])


class LumagenRemote(LumagenBaseEntity, RemoteEntity):
    """Lumagen command sender: OSD navigation + arbitrary command passthrough."""

    # Like media_player, this represents the device itself (dual-entity
    # pattern), so it takes the device name rather than a suffix.
    _attr_name = None

    def __init__(self, coordinator: LumagenCoordinator) -> None:
        super().__init__(coordinator, key="remote")

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.data.power_on

    @lumagen_command
    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.client.power_on()

    @lumagen_command
    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.client.standby()

    @lumagen_command
    async def async_send_command(self, command: Iterable[str], **kwargs: Any) -> None:
        num_repeats: int = kwargs.get(ATTR_NUM_REPEATS, DEFAULT_NUM_REPEATS)
        delay: float = kwargs.get(ATTR_DELAY_SECS, DEFAULT_DELAY_SECS)
        # Resolve friendly names once; unknown tokens pass through as raw
        # wire commands.
        wire_commands = [_COMMANDS.get(token, token) for token in command]
        for repeat in range(num_repeats):
            for index, wire in enumerate(wire_commands):
                self.coordinator.async_check_command_allowed()
                await self.coordinator.client.send_command(wire)
                is_last = repeat == num_repeats - 1 and index == len(wire_commands) - 1
                if delay and not is_last:
                    await asyncio.sleep(delay)

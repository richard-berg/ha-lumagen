"""Select dropdowns for the Lumagen Radiance Pro."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from aiolumagen import (
    Aspect,
    HdrGammaMode,
    LumagenClient,
    LumagenState,
    Memory,
    SharpnessSensitivity,
    SubtitleShift,
)
from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import LumagenConfigEntry, LumagenCoordinator, lumagen_command
from .entity import LumagenBaseEntity


@dataclass(frozen=True, kw_only=True)
class LumagenSelectDescription(SelectEntityDescription):
    """Bidirectional select description.

    ``current_fn`` reads the current option label from coordinator state.
    Returning ``None`` means "fall back to the entity's optimistic value",
    which covers both knobs the Lumagen never reports (HDR gamma mode) and
    ones it reports only on some firmware (subtitle shift).

    ``select_fn`` is given the client AND a snapshot of state at write
    time. State access is needed by compound-write commands like
    ``set_sharpness`` that must preserve other components' values.

    Note the optimistic fallback is per-entity and in-memory, so it resets to
    "unknown" on restart for anything the device won't report back.

    ``select_fn`` is ``None`` for the entries whose write path
    :meth:`LumagenSelect.async_select_option` handles itself (the
    compound ZY521 / ZY417 commands, which need coordinator state rather
    than just the client). ``None`` rather than a stand-in callable so a
    mismatch between the override branch and the descriptor surfaces as a
    loud failure instead of quietly dispatching the wrong command.
    """

    current_fn: Callable[[LumagenState], str | None]
    select_fn: Callable[[LumagenClient, LumagenState, str], Awaitable[None]] | None = None


# --- Aspect ratio (existing) ---
# Aspect labels match the Lumagen manual. Order matches the old ESPHome YAML
# so existing dashboards/automations don't need re-labeling. Wire commands
# come from aiolumagen's Aspect enum — never re-type the literals here (`w`
# is 16:9 and `W` is 2.35, so a case slip is a silently wrong preset).
_ASPECT_COMMANDS: dict[str, Aspect] = {
    "4:3": Aspect.RATIO_4_3,
    "Letterbox": Aspect.LETTERBOX,
    "16:9": Aspect.RATIO_16_9,
    "16:9 NZ": Aspect.RATIO_16_9_NZ,
    "1.85": Aspect.RATIO_1_85,
    "1.90": Aspect.RATIO_1_90,
    "2.00": Aspect.RATIO_2_00,
    "2.10": Aspect.RATIO_2_10,
    "2.20": Aspect.RATIO_2_20,
    "2.35": Aspect.RATIO_2_35,
    "2.40": Aspect.RATIO_2_40,
    "2.55": Aspect.RATIO_2_55,
    "2.76": Aspect.RATIO_2_76,
}


# Best-effort mapping from the Lumagen's !I24 content_aspect value to the
# closest preset label. The Lumagen does not report which preset is
# actually active — only detected content aspect — so when the user
# selects a mismatched preset (e.g. 4:3 on 16:9 content) the display will
# be wrong until they pick another preset. Protocol limitation, not a bug.
#
# Every *numeric* ratio offered in the dropdown needs an entry here, or picking
# it would immediately snap the shown value to a neighbour: with 2.10 offered
# but unmapped, selecting it on 2.10 content would read back as "2.00" or
# "2.20". The test suite asserts the two tables agree on that set.
#
# Letterbox and 16:9 NZ are deliberately absent: they're framing/zoom variants
# that share a detected content aspect with their base ratio (4:3 and 16:9), so
# no reported value could ever distinguish them. Selecting either shows the
# base ratio back, which is the honest answer.
_CONTENT_ASPECT_TO_LABEL: tuple[tuple[int, str], ...] = (
    (133, "4:3"),
    (178, "16:9"),
    (185, "1.85"),
    (190, "1.90"),
    (200, "2.00"),
    (210, "2.10"),
    (220, "2.20"),
    (235, "2.35"),
    (240, "2.40"),
    (255, "2.55"),
    (276, "2.76"),
)


# --- Memory (A-D) ---
# The dropdown label is NOT the wire command. The Lumagen reports the active
# memory as an uppercase letter but *recalls* one with a lowercase command,
# so deriving the command from the label (the old ``value.lower()``) only
# held while the labels happened to be bare letters — relabeling them to
# e.g. "Memory A" would have silently sent garbage. Map explicitly.
_MEMORY_COMMANDS: dict[str, Memory] = {
    "A": Memory.A,
    "B": Memory.B,
    "C": Memory.C,
    "D": Memory.D,
}


# --- Sharpness sensitivity (compound-write — preserves enabled + level) ---
_SHARPNESS_SENSITIVITY_TO_WIRE: dict[str, SharpnessSensitivity] = {
    "normal": SharpnessSensitivity.NORMAL,
    "high": SharpnessSensitivity.HIGH,
}
_SHARPNESS_WIRE_TO_LABEL = {v: k for k, v in _SHARPNESS_SENSITIVITY_TO_WIRE.items()}


# --- Subtitle shift (read-back where the firmware provides it) ---
# There is no ZQ query for subtitle shift, so this was purely optimistic. The
# Lumagen does appear to report it in the Full v5 status push, which lets the
# dropdown reflect a change made with the device's own remote — but only on
# firmware that appends that field. See _current_subtitle_shift.
_SUBTITLE_SHIFT_OPTIONS = ("off", "small", "large")
_SUBTITLE_SHIFT_TO_LEVEL = {"off": 0, "small": 1, "large": 2}
_SUBTITLE_SHIFT_WIRE_TO_LABEL: dict[SubtitleShift, str] = {
    SubtitleShift.OFF: "off",
    SubtitleShift.PERCENT_3: "small",
    SubtitleShift.PERCENT_6: "large",
}


# --- HDR gamma mode (compound-write — pairs with hdr_mapping_max_nits) ---
# The Lumagen has no documented query for the active mapping values, so
# both halves of the ZY417 compound are tracked optimistically on the
# coordinator. The entity handles read/write directly against that state
# rather than going through select_fn.
_HDR_GAMMA_LABEL_TO_WIRE: dict[str, HdrGammaMode] = {
    "auto": HdrGammaMode.AUTO,
    "hdr": HdrGammaMode.HDR,
    "sdr": HdrGammaMode.SDR,
}
_HDR_GAMMA_WIRE_TO_LABEL = {v: k for k, v in _HDR_GAMMA_LABEL_TO_WIRE.items()}


# --- select_fn implementations ---


async def _select_aspect(client: LumagenClient, _state: LumagenState, value: str) -> None:
    cmd = _ASPECT_COMMANDS.get(value)
    if cmd is not None:
        await client.send_command(cmd)


async def _select_memory(client: LumagenClient, _state: LumagenState, value: str) -> None:
    command = _MEMORY_COMMANDS.get(value)
    if command is not None:
        await client.send_command(command)


async def _select_subtitle_shift(client: LumagenClient, _state: LumagenState, value: str) -> None:
    level = _SUBTITLE_SHIFT_TO_LEVEL.get(value)
    if level is not None:
        await client.set_subtitle_shift(level)


# --- current_fn helpers ---


def _closest_aspect_label(raw: str | None) -> str | None:
    """Return the dropdown label whose target is closest to ``raw``.

    ``raw`` is the Lumagen's !I24 SSS field — zero-padded integer of
    ``aspect * 100`` (e.g. ``178`` for 16:9). Snap to the nearest entry;
    if ``raw`` can't be parsed return ``None`` so the UI shows no selection.
    """
    if raw is None:
        return None
    try:
        value = int(raw)
    except TypeError, ValueError:
        return None
    return min(
        _CONTENT_ASPECT_TO_LABEL,
        key=lambda entry: abs(entry[0] - value),
    )[1]


def _current_sharpness_sensitivity(state: LumagenState) -> str | None:
    if state.sharpness_sensitivity is None:
        return None
    return _SHARPNESS_WIRE_TO_LABEL.get(state.sharpness_sensitivity)


def _current_subtitle_shift(state: LumagenState) -> str | None:
    """Device-reported subtitle shift, or ``None`` to keep the optimistic value.

    Returning ``None`` is the descriptor's documented "fall back to what we
    last wrote", so behaviour is unchanged on firmware that doesn't report
    this: the dropdown still shows the last selection. Where the firmware
    *does* report it, a change made with the Lumagen's own remote now shows
    up here, which the optimistic-only path could never do.

    The underlying field is empirically mapped rather than documented and is
    absent on firmware 030225 (see aiolumagen's protocol module), which is
    another reason the fallback matters — this must not regress the common
    case to chase the uncommon one.
    """
    if state.subtitle_shift is None:
        return None
    return _SUBTITLE_SHIFT_WIRE_TO_LABEL.get(state.subtitle_shift)


# --- Entity descriptors ---


SELECTS: tuple[LumagenSelectDescription, ...] = (
    LumagenSelectDescription(
        key="aspect_select",
        translation_key="aspect_select",
        options=list(_ASPECT_COMMANDS),
        current_fn=lambda s: _closest_aspect_label(s.content_aspect),
        select_fn=_select_aspect,
    ),
    LumagenSelectDescription(
        key="memory_select",
        translation_key="memory_select",
        options=list(_MEMORY_COMMANDS),
        current_fn=lambda s: s.input_memory,
        select_fn=_select_memory,
    ),
    LumagenSelectDescription(
        key="sharpness_sensitivity",
        translation_key="sharpness_sensitivity",
        options=list(_SHARPNESS_SENSITIVITY_TO_WIRE),
        # Deliberately NOT EntityCategory.CONFIG: this belongs with the
        # sharpness enable switch and level slider (both plain controls),
        # and splitting the trio across Controls/Configuration in the
        # device page made them awkward to use together.
        current_fn=_current_sharpness_sensitivity,
        # Compound ZY521 write — dispatched via the coordinator so enabled
        # and level are preserved, so no select_fn (see the descriptor).
    ),
    LumagenSelectDescription(
        key="subtitle_shift",
        translation_key="subtitle_shift",
        options=list(_SUBTITLE_SHIFT_OPTIONS),
        entity_category=EntityCategory.CONFIG,
        # Reads the device's value when the firmware reports it, otherwise
        # returns None and the entity shows the locally-tracked optimistic
        # value — the previous behaviour.
        current_fn=_current_subtitle_shift,
        select_fn=_select_subtitle_shift,
    ),
    LumagenSelectDescription(
        key="hdr_gamma_mode",
        translation_key="hdr_gamma_mode",
        options=list(_HDR_GAMMA_LABEL_TO_WIRE),
        entity_category=EntityCategory.CONFIG,
        # Read/write goes through coordinator.hdr_mapping_gamma_mode — the
        # entity overrides both halves of the dispatch, so no select_fn
        # (see the descriptor). current_fn still has to be supplied; it
        # never runs for this entry.
        current_fn=lambda _s: None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LumagenConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(LumagenSelect(coordinator, description) for description in SELECTS)


class LumagenSelect(LumagenBaseEntity, SelectEntity):
    """Bidirectional Lumagen dropdown.

    Falls back to a locally-tracked optimistic value when ``current_fn``
    returns ``None`` — either because the Lumagen has no query for the
    setting at all (HDR gamma mode) or because this firmware doesn't report
    it (subtitle shift).

    The HDR gamma-mode entry is a special case: the underlying ZY417
    command pairs the gamma byte with a numeric max-nits value. Both
    halves live as optimistic state on the coordinator; this entity
    reads/writes through that shared state.
    """

    entity_description: LumagenSelectDescription

    def __init__(
        self,
        coordinator: LumagenCoordinator,
        description: LumagenSelectDescription,
    ) -> None:
        super().__init__(coordinator, key=description.key)
        self.entity_description = description
        self._optimistic_option: str | None = None

    @property
    def current_option(self) -> str | None:
        if self.entity_description.key == "hdr_gamma_mode":
            label = _HDR_GAMMA_WIRE_TO_LABEL.get(self.coordinator.hdr_mapping_gamma_mode)
            return label if label in (self.entity_description.options or []) else None
        current = self.entity_description.current_fn(self.coordinator.data)
        options = self.entity_description.options or []
        if current is None:
            return self._optimistic_option if self._optimistic_option in options else None
        return current if current in options else None

    @lumagen_command
    async def async_select_option(self, option: str) -> None:
        if self.entity_description.key == "hdr_gamma_mode":
            gamma_mode = _HDR_GAMMA_LABEL_TO_WIRE.get(option)
            if gamma_mode is None:
                return
            await self.coordinator.client.set_hdr_intensity_mapping(
                display_max_nits=self.coordinator.hdr_mapping_max_nits,
                gamma_mode=gamma_mode,
            )
            self.coordinator.hdr_mapping_gamma_mode = gamma_mode
            self.async_write_ha_state()
            return
        if self.entity_description.key == "sharpness_sensitivity":
            sensitivity = _SHARPNESS_SENSITIVITY_TO_WIRE.get(option)
            if sensitivity is None:
                return
            await self.coordinator.async_set_sharpness(sensitivity=sensitivity)
            self._optimistic_option = option
            self.async_write_ha_state()
            return
        select_fn = self.entity_description.select_fn
        if select_fn is None:
            raise HomeAssistantError(
                f"Lumagen select {self.entity_description.key!r} has no write path; "
                "an entity override was expected to handle it."
            )
        await select_fn(self.coordinator.client, self.coordinator.data, option)
        self._optimistic_option = option
        self.async_write_ha_state()

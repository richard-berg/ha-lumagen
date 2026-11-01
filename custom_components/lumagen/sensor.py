"""Sensors for the Lumagen Radiance Pro."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from aiolumagen import (
    AutoAspectStatus,
    Colorspace,
    HdrStatus,
    InputStatus,
    LumagenState,
    SourceMode,
)
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfFrequency
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType

from .coordinator import LumagenConfigEntry
from .entity import LumagenBaseEntity


@dataclass(frozen=True, kw_only=True)
class LumagenSensorDescription(SensorEntityDescription):
    """Sensor description with a pull-from-state function."""

    value_fn: Callable[[LumagenState], StateType]


def _state_key(member: StrEnum | None) -> StateType:
    """Return the translation key for an enum state: its member name, lowercased.

    Raw device values like ``Rec.709`` or ``No Source`` aren't valid
    translation keys, and the member names are stable even if the wire
    values change.
    """
    return member.name.lower() if member is not None else None


# The firmware reports "no input" as ``-`` or, on Full v5, ``n``.
_SOURCE_MODE_STATES: dict[SourceMode, str] = {
    SourceMode.INTERLACED: "interlaced",
    SourceMode.PROGRESSIVE: "progressive",
    SourceMode.NO_INPUT: "no_input",
    SourceMode.NO_INPUT_V5: "no_input",
}
_SOURCE_MODE_OPTIONS = list(dict.fromkeys(_SOURCE_MODE_STATES.values()))


def _source_mode_state(mode: SourceMode | None) -> StateType:
    return _SOURCE_MODE_STATES.get(mode) if mode is not None else None


SENSORS: tuple[LumagenSensorDescription, ...] = (
    # --- Diagnostic: always-on identity + firmware ---
    LumagenSensorDescription(
        key="model",
        translation_key="model",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.model,
    ),
    LumagenSensorDescription(
        key="firmware",
        translation_key="firmware",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.firmware,
    ),
    # --- Primary: what's playing right now ---
    LumagenSensorDescription(
        key="current_input",
        translation_key="current_input",
        value_fn=lambda s: s.current_input,
    ),
    LumagenSensorDescription(
        key="input_memory",
        translation_key="input_memory",
        value_fn=lambda s: s.input_memory,
    ),
    # --- Source characteristics ---
    LumagenSensorDescription(
        key="source_resolution",
        translation_key="source_resolution",
        value_fn=lambda s: s.source_resolution,
    ),
    LumagenSensorDescription(
        key="source_vrate",
        translation_key="source_vrate",
        device_class=SensorDeviceClass.FREQUENCY,
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: s.source_refresh_hz,
    ),
    LumagenSensorDescription(
        key="source_resolution_full",
        translation_key="source_resolution_full",
        value_fn=lambda s: _resolution_label(s.source_resolution, s.source_width, s.source_mode),
    ),
    LumagenSensorDescription(
        key="source_aspect",
        translation_key="source_aspect",
        value_fn=lambda s: s.source_aspect,
    ),
    LumagenSensorDescription(
        key="content_aspect",
        translation_key="content_aspect",
        value_fn=lambda s: s.content_aspect,
    ),
    # --- Output characteristics ---
    LumagenSensorDescription(
        key="output_resolution",
        translation_key="output_resolution",
        value_fn=lambda s: s.output_resolution,
    ),
    LumagenSensorDescription(
        key="output_vrate",
        translation_key="output_vrate",
        device_class=SensorDeviceClass.FREQUENCY,
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: s.output_refresh_hz,
    ),
    LumagenSensorDescription(
        key="output_resolution_full",
        translation_key="output_resolution_full",
        value_fn=lambda s: _resolution_label(
            s.output_resolution, s.output_width, s.output_scan_mode
        ),
    ),
    LumagenSensorDescription(
        key="output_aspect",
        translation_key="output_aspect",
        value_fn=lambda s: s.output_aspect,
    ),
    # --- Enum-backed status sensors ---
    LumagenSensorDescription(
        key="colorspace",
        translation_key="colorspace",
        device_class=SensorDeviceClass.ENUM,
        options=[cs.name.lower() for cs in Colorspace],
        value_fn=lambda s: _state_key(s.colorspace),
    ),
    LumagenSensorDescription(
        key="hdr_status",
        translation_key="hdr_status",
        device_class=SensorDeviceClass.ENUM,
        options=[hs.name.lower() for hs in HdrStatus],
        value_fn=lambda s: _state_key(s.hdr_status),
    ),
    LumagenSensorDescription(
        key="input_status",
        translation_key="input_status",
        device_class=SensorDeviceClass.ENUM,
        options=[ist.name.lower() for ist in InputStatus],
        value_fn=lambda s: _state_key(s.input_status),
    ),
    LumagenSensorDescription(
        key="source_mode",
        translation_key="source_mode",
        device_class=SensorDeviceClass.ENUM,
        options=_SOURCE_MODE_OPTIONS,
        value_fn=lambda s: _source_mode_state(s.source_mode),
    ),
    LumagenSensorDescription(
        key="output_scan_mode",
        translation_key="output_scan_mode",
        device_class=SensorDeviceClass.ENUM,
        options=_SOURCE_MODE_OPTIONS,
        value_fn=lambda s: _source_mode_state(s.output_scan_mode),
    ),
    # --- Signal-path diagnostics (from the extended !I24/!I25 fields) ---
    LumagenSensorDescription(
        key="physical_input",
        translation_key="physical_input",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.physical_input,
    ),
    # The detected/applied aspect pair is what makes auto-aspect behaviour
    # explainable: source_aspect and content_aspect are what the Lumagen is
    # *using*, these two are what it *saw*. A mismatch is the signature of a
    # manual preset overriding detection.
    LumagenSensorDescription(
        key="detected_source_aspect",
        translation_key="detected_source_aspect",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.detected_source_aspect,
    ),
    LumagenSensorDescription(
        key="detected_content_aspect",
        translation_key="detected_content_aspect",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.detected_content_aspect,
    ),
    LumagenSensorDescription(
        key="active_outputs",
        translation_key="active_outputs",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: _active_outputs_label(s.active_outputs),
    ),
    LumagenSensorDescription(
        key="output_cms",
        translation_key="output_cms",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.output_cms,
    ),
    LumagenSensorDescription(
        key="output_style",
        translation_key="output_style",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.output_style,
    ),
    LumagenSensorDescription(
        key="input_config",
        translation_key="input_config",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.input_config,
    ),
    # Tri-state counterpart to the auto_aspect switch. Confirmed on hardware
    # (Radiance Pro 4242, firmware 030326): the field is populated and payload
    # index 26 is correctly mapped. Absent on firmware 030225, where it reads
    # "unknown".
    #
    # Stays DIAGNOSTIC, and the switch deliberately keeps using the ZQI54
    # boolean, because in practice this field carries no more information:
    # measured against ZQI54 it always agrees, offset by one. Three independent
    # ways of turning auto aspect off all report identically:
    #
    #   auto aspect on              -> index 26 = 2, ZQI54 = 1
    #   off via serial 'V'          -> index 26 = 1, ZQI54 = 0
    #   off via the OSD menu        -> index 26 = 1, ZQI54 = 0
    #   inhibited by subtitle shift -> index 26 = 1, ZQI54 = 0
    #
    # So "Disabled" does NOT distinguish configured-but-inhibited from plain
    # off. Do not drive the switch's is_on from this field: treating "Disabled"
    # as on would show the switch enabled right after the user turned auto
    # aspect off. ("Off", index 26 = 0, has never been observed -- not even with
    # no source locked, which still reports 2 when auto aspect is enabled.)
    #
    # The one genuine advantage is latency, not content: index 26 rides the
    # Full v5 push while ZQI54 needs a poll. Acting on that belongs upstream in
    # aiolumagen, not here.
    LumagenSensorDescription(
        key="auto_aspect_status",
        translation_key="auto_aspect_status",
        device_class=SensorDeviceClass.ENUM,
        options=[aa.name.lower() for aa in AutoAspectStatus],
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: _state_key(s.auto_aspect_status),
    ),
    # --- HDR source mastering metadata (from !I52) ---
    # All three are diagnostic — they describe the encoded content, not
    # something the user controls. They become available only when the
    # source is HDR; SDR sources keep them at None and the entity reads
    # "unknown" rather than "0 nits".
    LumagenSensorDescription(
        key="hdr_source_max_luminance",
        translation_key="hdr_source_max_luminance",
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement="cd/m²",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: s.hdr_source_max_luminance,
    ),
    LumagenSensorDescription(
        key="hdr_source_min_luminance",
        translation_key="hdr_source_min_luminance",
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement="cd/m²",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        value_fn=lambda s: s.hdr_source_min_luminance,
    ),
    LumagenSensorDescription(
        key="hdr_source_max_cll",
        translation_key="hdr_source_max_cll",
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement="cd/m²",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: s.hdr_source_max_cll,
    ),
)


# Note on refresh rate: the source/output rate sensors read
# ``state.source_refresh_hz`` / ``state.output_refresh_hz``, which aiolumagen
# derives from the ``RRR``/``PPP`` wire codes. This replaced a local
# ``float(raw)`` that reported a 59.94 Hz signal as **59.0 Hz** — the codes are
# the *truncated* integer part of the rate, so the NTSC family all read one
# below nominal (Tip0011: "e.g. 059 for 59.94, 060 for 60.00"). Values for
# those rates therefore change, correctly, on upgrade.


def _resolution_label(
    vertical: str | None, width: int | None, mode: SourceMode | None
) -> str | None:
    """Build a familiar ``3840x2160p`` label for a signal path.

    The Lumagen reports only vertical resolution; the width comes from
    aiolumagen's ``source_width`` / ``output_width``, derived from vertical
    resolution plus raster aspect. Assembling the display string is
    presentation, so it belongs here rather than in the library.

    Degrades in steps rather than to nothing: without a width we still return
    ``2160p``, and without a scan mode we drop the suffix. ``None`` only when
    there's no usable vertical resolution at all, which is also how a
    no-signal report (``0000``) surfaces.
    """
    if vertical is None:
        return None
    try:
        height = int(vertical)
    except TypeError, ValueError:
        return None
    if height <= 0:
        return None
    if mode is SourceMode.INTERLACED:
        suffix = "i"
    elif mode is SourceMode.PROGRESSIVE:
        suffix = "p"
    else:
        # NO_INPUT / NO_INPUT_V5 / not yet observed — no meaningful suffix.
        suffix = ""
    if width is None:
        return f"{height}{suffix}"
    return f"{width}x{height}{suffix}"


def _active_outputs_label(outputs: tuple[int, ...] | None) -> str | None:
    """Render the decoded output-enable set as a readable list.

    ``None`` (not yet observed) stays ``None`` so the entity reads "unknown",
    while an empty tuple is a real state — every output disabled — and reads
    "None" rather than being conflated with "not observed".
    """
    if outputs is None:
        return None
    if not outputs:
        return "None"
    return ", ".join(str(n) for n in outputs)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LumagenConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(LumagenSensor(coordinator, description) for description in SENSORS)


class LumagenSensor(LumagenBaseEntity, SensorEntity):
    """Push-driven Lumagen sensor."""

    entity_description: LumagenSensorDescription

    def __init__(
        self,
        coordinator,  # type: ignore[no-untyped-def]
        description: LumagenSensorDescription,
    ) -> None:
        super().__init__(coordinator, key=description.key)
        self.entity_description = description

    @property
    def native_value(self) -> StateType:
        return self.entity_description.value_fn(self.coordinator.data)

"""Media player for the Lumagen Radiance Pro.

The Lumagen is a video processor between sources and a display — no media
transport, no volume. But its defining control *is* input switching, which is
exactly what a media_player's ``source`` / ``source_list`` model expresses.
Modelling it this way puts power + input on one media card and enables voice
control ("turn on the Lumagen", "set Lumagen source to Input 3").

Only ``TURN_ON`` / ``TURN_OFF`` / ``SELECT_SOURCE`` are advertised, so cards
don't render volume sliders or transport buttons the device can't honor.

Source selection is genuinely bidirectional: unlike aspect (which the Lumagen
never reports as an active preset), the current input *is* reported via
``!I24`` / ``!I25``, so ``source`` reflects real device state rather than an
optimistic guess.

The Lumagen isn't "playing media" itself, but it *does* report live
statistics about the signal passing through it. We surface that on the media
card the way any player surfaces "now playing": the source->output signal
path is mapped onto ``media_title`` (the card's bold primary line) and the
HDR/colorspace summary onto ``app_name`` (which the frontend's
``computeMediaDescription`` renders as the secondary line when
``media_content_type`` is left unset). Both are gated on power so a stale
signal path never lingers on the card when the device is in standby.

The same fields also ride along as extra state attributes — that's the
full, unformatted mirror shown in the more-info dialog. The authoritative
per-field entities remain the individual sensors; the card fields and
attributes here are a convenience view.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from aiolumagen import SourceMode
from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import LumagenConfigEntry, LumagenCoordinator, lumagen_command
from .entity import LumagenBaseEntity

# This integration surfaces inputs 1-8. The client accepts 1-19 for models
# with more physical inputs; an input the device reports outside 1-8 simply
# shows as no current selection.
#
# Source names come from the Lumagen's configured input labels
# (state.input_labels, populated by aiolumagen's query_input_labels). Until
# those land — or for any input the device didn't label — we fall back to
# "Input N". source_list and the reverse label->input lookup are derived from
# coordinator state on each read, so a relabel (or the first label arrival)
# shows up without recreating the entity.
_INPUT_COUNT = 8


def _fallback_label(number: int) -> str:
    """Default display name for input ``number`` when no configured label exists."""
    return f"Input {number}"


def _fmt_rate(hz: float | None) -> str | None:
    """Compact Hz for a card label: ``60.0`` -> ``60``, ``59.94`` -> ``59.94``.

    Takes the rate aiolumagen already decoded, not the raw wire code. This used
    to run ``int()`` over the code and label a 59.94 Hz signal "59" — the codes
    are the *truncated* integer part of the rate, so every NTSC-family rate
    came out one below nominal. Keeping the fractional part matters for this
    audience specifically: 59.94 vs 60 is the distinction worth seeing.

    Trailing zeros are dropped so integer rates stay short.
    """
    if hz is None:
        return None
    rounded = round(hz, 2)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:g}"


def _scan_letter(mode: SourceMode | None) -> str | None:
    """The ``p``/``i`` scan letter for a resolution label, or ``None``.

    The Lumagen's resolution fields carry only the line count (``2160``); the
    scan type is a separate field. Only interlaced/progressive contribute a
    letter — the "no input" sentinels (``-``/``n``) don't.
    """
    if mode in (SourceMode.PROGRESSIVE, SourceMode.INTERLACED):
        return mode.value  # the enum value *is* the wire letter ("p" / "i")
    return None


def _format_signal(
    resolution: str | None, rate: float | None, scan: str | None = None
) -> str | None:
    """Combine resolution + scan + refresh rate into one label, e.g. ``2160p59.94``.

    Without the scan letter the digits would run together (``216059.94``); the
    letter is what separates them. Degrades gracefully: resolution alone ->
    ``2160``; rate alone -> ``60Hz``; neither -> ``None``.
    """
    rate_fmt = _fmt_rate(rate)
    if resolution:
        return f"{resolution}{scan or ''}{rate_fmt or ''}"
    if rate_fmt:
        return f"{rate_fmt}Hz"
    return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LumagenConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities([LumagenMediaPlayer(coordinator)])


class LumagenMediaPlayer(LumagenBaseEntity, MediaPlayerEntity):
    """The Lumagen as an AV source-switcher: power + input selection."""

    # Primary entity for the device, so it takes the device name rather than
    # a "<device> Media player" suffix.
    _attr_name = None
    _attr_supported_features = (
        MediaPlayerEntityFeature.TURN_ON
        | MediaPlayerEntityFeature.TURN_OFF
        | MediaPlayerEntityFeature.SELECT_SOURCE
    )

    def __init__(self, coordinator: LumagenCoordinator) -> None:
        super().__init__(coordinator, key="media_player")

    @property
    def state(self) -> MediaPlayerState | None:
        power_on = self.coordinator.data.power_on
        if power_on is None:
            return None
        # The Lumagen's "off" is really standby (it stays reachable over
        # RS-232), but ON/OFF gives the cleanest power-toggle UX on the media
        # card and maps directly to the % / $ commands.
        return MediaPlayerState.ON if power_on else MediaPlayerState.OFF

    def _source_map(self) -> dict[str, int]:
        """Ordered ``{display label: input number}`` with unique labels.

        Source selection round-trips through the label string: the dropdown
        shows a label and HA hands that same string back to
        :meth:`async_select_source`, which must resolve it to an input
        number. That only works if every label is unique.

        The Lumagen doesn't guarantee uniqueness — inputs can share a
        configured label, or all still carry a generic default (we've seen a
        device report every input simply as "Input", with no number). Empty
        or duplicated labels are therefore replaced with the numbered
        ``Input N`` fallback, which is both unique and clearer; a distinct,
        non-empty custom label is shown as-is unless it steals another input's
        numbered fallback. Built fresh on each read so a relabel shows up
        without recreating the entity.
        """
        labels = self.coordinator.data.input_labels
        raw = {n: (labels.get(n) or "").strip() for n in range(1, _INPUT_COUNT + 1)}
        counts = Counter(label for label in raw.values() if label)
        fallbacks = {_fallback_label(n): n for n in raw}
        mapping: dict[str, int] = {}
        for n in range(1, _INPUT_COUNT + 1):
            label = raw[n]
            display = (
                label
                if label and counts[label] == 1 and fallbacks.get(label, n) == n
                else _fallback_label(n)
            )
            mapping[display] = n
        return mapping

    @property
    def source_list(self) -> list[str]:
        return list(self._source_map())

    @property
    def source(self) -> str | None:
        raw = self.coordinator.data.current_input
        if raw is None:
            return None
        try:
            number = int(raw)
        except TypeError, ValueError:
            return None
        if not 1 <= number <= _INPUT_COUNT:
            # Inputs outside the surfaced range show as no selection rather
            # than an out-of-list value the frontend would reject.
            return None
        for display, n in self._source_map().items():
            if n == number:
                return display
        return None

    # --- Card-facing "now playing" view -------------------------------------
    # These map the live signal path onto the media card's own text lines so
    # it shows on the card face, not just the more-info attributes. Gated on
    # power: in standby we report nothing rather than a stale path. We
    # deliberately leave media_content_type unset so the frontend's
    # computeMediaDescription renders app_name as the secondary line.

    @property
    def media_title(self) -> str | None:
        """Primary card line: the source -> output signal path."""
        state = self.coordinator.data
        if not state.power_on:
            return None
        source = _format_signal(
            state.source_resolution,
            state.source_refresh_hz,
            _scan_letter(state.source_mode),
        )
        # Output scan mode is reported (field H of !I24/!I25) — this used to
        # hardcode "p" on the assumption that it wasn't, which was true only of
        # the parser, not the protocol.
        output = _format_signal(
            state.output_resolution,
            state.output_refresh_hz,
            _scan_letter(state.output_scan_mode),
        )
        if source and output:
            return f"{source} → {output}"
        return source or output

    @property
    def app_name(self) -> str | None:
        """Secondary card line: dynamic range + colorspace, e.g. ``HDR · Rec.2020``."""
        state = self.coordinator.data
        if not state.power_on:
            return None
        parts = [value.value for value in (state.hdr_status, state.colorspace) if value is not None]
        return " · ".join(parts) if parts else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self.coordinator.data
        return {
            "source_resolution": state.source_resolution,
            # Decoded Hz, not the raw wire code: an attribute called
            # "refresh_rate" reporting "059" for 59.94 Hz was simply wrong.
            "source_refresh_rate": state.source_refresh_hz,
            "output_resolution": state.output_resolution,
            "output_refresh_rate": state.output_refresh_hz,
            "hdr_status": state.hdr_status.value if state.hdr_status else None,
            "colorspace": state.colorspace.value if state.colorspace else None,
        }

    @lumagen_command
    async def async_turn_on(self) -> None:
        await self.coordinator.client.power_on()

    @lumagen_command
    async def async_turn_off(self) -> None:
        await self.coordinator.client.standby()

    @lumagen_command
    async def async_select_source(self, source: str) -> None:
        # Resolve against the same unique mapping used to build source_list,
        # so the picked label maps to exactly one input.
        number = self._source_map().get(source)
        if number is not None:
            await self.coordinator.client.set_input(number)
        # Unknown source label — no-op.

"""Number entities for the Lumagen Radiance Pro.

* **Sharpness level** (0-7) — read from ``state.sharpness_level``,
  written via ``set_sharpness`` which preserves the current ``enabled``
  and ``sensitivity`` values. Defaults to ``enabled=False, sensitivity="N"``
  if those haven't been observed yet (the slider on its own doesn't
  imply sharpening should be on).
* **Minimum fan speed** (1-10) — write-only, and unavoidably so. No
  fan-speed query exists: it's absent from the Lumagen RS-232 doc and the
  firmware-strings command table, and probing the plausible undocumented
  slots (``ZQI55``-``ZQI57``, ``ZQS05``-``ZQS07``) returned empty payloads,
  which on this firmware is indistinguishable from a nonexistent code.
  So the slider tracks the last value we sent: it reads "unknown" until
  first set, and a change made on the device itself can't be seen. We
  deliberately don't seed a fake starting value — that would claim to
  report device state we don't have. The range is in the device's own
  units (what its menu displays); aiolumagen converts to the 0-based wire
  value.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from aiolumagen import LumagenClient, LumagenState
from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import LumagenConfigEntry, LumagenCoordinator, lumagen_command
from .entity import LumagenBaseEntity


@dataclass(frozen=True, kw_only=True)
class LumagenNumberDescription(NumberEntityDescription):
    """Number description with a value-from-state and a writer.

    ``value_fn`` may return None for write-only knobs that have no
    corresponding query; the entity falls back to a locally-tracked
    "last set" value in that case.
    """

    value_fn: Callable[[LumagenState], int | None]
    set_fn: Callable[[LumagenClient, LumagenState, int], Awaitable[None]]


async def _set_fan_speed(client: LumagenClient, _state: LumagenState, level: int) -> None:
    await client.set_fan_speed(level)


# HDR intensity-mapping max nits is a compound write — pairs with the
# gamma-mode select. Both entities share optimistic state on the
# coordinator since neither has a device-side query. The setter is
# wired up in the entity class (it needs the coordinator, not just the
# client) — the description's set_fn for this knob is unused.
NUMBERS: tuple[LumagenNumberDescription, ...] = (
    LumagenNumberDescription(
        key="sharpness_level",
        translation_key="sharpness_level",
        native_min_value=0,
        native_max_value=7,
        native_step=1,
        mode=NumberMode.SLIDER,
        value_fn=lambda s: s.sharpness_level,
        # Compound ZY521 write — dispatched via the coordinator so enabled
        # and sensitivity are preserved. set_fn is unused here.
        set_fn=_set_fan_speed,  # placeholder; entity overrides
    ),
    LumagenNumberDescription(
        key="fan_speed",
        translation_key="fan_speed",
        # 1-10 to match the number the Lumagen shows in its own menu. The
        # wire value is one lower; aiolumagen's fan_speed_command owns that
        # conversion, so this range is in device units.
        native_min_value=1,
        native_max_value=10,
        native_step=1,
        mode=NumberMode.SLIDER,
        entity_category=EntityCategory.CONFIG,
        # No query for fan speed — value_fn always returns None and we
        # fall back to the locally-tracked last-set value in the entity.
        value_fn=lambda _s: None,
        set_fn=_set_fan_speed,
    ),
    LumagenNumberDescription(
        key="hdr_mapping_max_nits",
        translation_key="hdr_mapping_max_nits",
        native_min_value=0,
        native_max_value=10000,
        native_step=50,
        native_unit_of_measurement="cd/m²",
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        # Read from coordinator's optimistic state, not LumagenState —
        # the entity overrides native_value to do this. set_fn is unused
        # for this entry; the entity dispatches directly to the
        # coordinator's compound writer.
        value_fn=lambda _s: None,
        set_fn=_set_fan_speed,  # placeholder; entity overrides
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LumagenConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(LumagenNumber(coordinator, description) for description in NUMBERS)


class LumagenNumber(LumagenBaseEntity, NumberEntity):
    """Bidirectional number backed by aiolumagen state + setter.

    The HDR mapping max-nits entry is a special case: the underlying
    ZY417 command pairs the nits value with a gamma-mode select. Both
    halves live as optimistic state on the coordinator; this entity
    reads/writes through that shared state rather than via state_fn.
    """

    entity_description: LumagenNumberDescription

    def __init__(
        self,
        coordinator: LumagenCoordinator,
        description: LumagenNumberDescription,
    ) -> None:
        super().__init__(coordinator, key=description.key)
        self.entity_description = description
        # Local fallback for write-only knobs (fan speed). Stays None
        # until the user changes it, at which point we display what they set.
        self._optimistic_value: int | None = None

    @property
    def native_value(self) -> int | None:
        if self.entity_description.key == "hdr_mapping_max_nits":
            return self.coordinator.hdr_mapping_max_nits
        from_state = self.entity_description.value_fn(self.coordinator.data)
        if from_state is not None:
            return from_state
        return self._optimistic_value

    @lumagen_command
    async def async_set_native_value(self, value: float) -> None:
        level = int(value)
        if self.entity_description.key == "hdr_mapping_max_nits":
            # Compound write: preserve the current gamma_mode from the
            # coordinator's optimistic state.
            await self.coordinator.client.set_hdr_intensity_mapping(
                display_max_nits=level,
                gamma_mode=self.coordinator.hdr_mapping_gamma_mode,
            )
            self.coordinator.hdr_mapping_max_nits = level
            self.async_write_ha_state()
            return
        if self.entity_description.key == "sharpness_level":
            await self.coordinator.async_set_sharpness(level=level)
            self._optimistic_value = level
            self.async_write_ha_state()
            return
        await self.entity_description.set_fn(self.coordinator.client, self.coordinator.data, level)
        # Track locally so the slider doesn't snap back when value_fn returns None.
        self._optimistic_value = level
        self.async_write_ha_state()

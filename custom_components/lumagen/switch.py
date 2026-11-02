"""Switches for the Lumagen Radiance Pro.

Three switches today:

* **Sharpness enabled** — toggles the Lumagen's edge-enhancement on/off.
  The underlying ``ZY521ELS`` command is compound (enabled + level +
  sensitivity), so flipping this switch reads the *current* level and
  sensitivity from coordinator state and writes them back unchanged
  alongside the new enabled bit. If level/sensitivity haven't been
  observed yet (e.g. first boot before ``ZQI30`` lands), we fall back to
  ``level=4`` and ``sensitivity="N"`` — sensible defaults that won't
  surprise the user.
* **Game mode** — single-bit ``ZY551X`` toggle, no compound write needed.
* **Auto aspect detection** — toggles the Lumagen's automatic aspect-ratio
  detection (``~`` on / ``V`` off). Unlike game mode it has no dedicated
  ``set_*`` wrapper on the client, so the write goes through
  ``send_command`` followed by a ``ZQI54`` re-query — auto-aspect isn't
  part of the Full v5 push stream, so state has to be pulled back. This
  replaces the old on/off buttons + read-only binary sensor with a single
  stateful control.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from aiolumagen import Aspect, LumagenClient, LumagenState
from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import LumagenConfigEntry, LumagenCoordinator, lumagen_command
from .entity import LumagenBaseEntity


@dataclass(frozen=True, kw_only=True)
class LumagenSwitchDescription(SwitchEntityDescription):
    """Switch description with a value-from-state and an on/off writer.

    ``set_fn`` is ``None`` for the sharpness switch, whose write path
    :meth:`LumagenSwitch._async_set` handles itself (the compound ZY521
    command needs coordinator state, not just the client). ``None`` rather
    than a stand-in callable so a mismatch between the override branch and
    the descriptor fails loudly instead of quietly toggling game mode.
    """

    value_fn: Callable[[LumagenState], bool | None]
    set_fn: Callable[[LumagenClient, LumagenState, bool], Awaitable[None]] | None = None


async def _set_game_mode(client: LumagenClient, _state: LumagenState, enabled: bool) -> None:
    await client.set_game_mode(enabled)


async def _set_auto_aspect(client: LumagenClient, _state: LumagenState, enabled: bool) -> None:
    """Toggle automatic aspect detection.

    There's no ``client.set_auto_aspect`` wrapper (the on/off commands are
    the same ``~``/``V`` aspect commands the client exposes generically), so
    this sends the raw command.

    No follow-up query, unlike ``_set_game_mode``. Auto aspect *does* ride the
    Full v5 push at payload index 26, and the device emits an unsolicited
    ``!I25`` on every auto-aspect change — verified on hardware by listening with
    no query outstanding. aiolumagen v0.10.0 feeds ``state.auto_aspect`` from that
    index, so the state arrives on its own and faster than a query would return
    it. The previous ``query_auto_aspect()`` here was redundant work.

    ``refresh=False`` because there is nothing to refresh: the push is the
    refresh.
    """
    await client.send_command(
        Aspect.AUTO_ENABLE if enabled else Aspect.AUTO_DISABLE,
        refresh=False,
    )


SWITCHES: tuple[LumagenSwitchDescription, ...] = (
    LumagenSwitchDescription(
        key="sharpness_enabled",
        translation_key="sharpness_enabled",
        value_fn=lambda s: s.sharpness_enabled,
        # Compound ZY521 write — dispatched via the coordinator so the level
        # and sensitivity are preserved, so no set_fn (see the descriptor).
    ),
    LumagenSwitchDescription(
        key="game_mode",
        translation_key="game_mode",
        value_fn=lambda s: s.game_mode,
        set_fn=_set_game_mode,
    ),
    LumagenSwitchDescription(
        key="auto_aspect",
        translation_key="auto_aspect",
        value_fn=lambda s: s.auto_aspect,
        set_fn=_set_auto_aspect,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LumagenConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(LumagenSwitch(coordinator, description) for description in SWITCHES)


class LumagenSwitch(LumagenBaseEntity, SwitchEntity):
    """Bidirectional switch backed by a aiolumagen state field + setter."""

    entity_description: LumagenSwitchDescription

    def __init__(
        self,
        coordinator: LumagenCoordinator,
        description: LumagenSwitchDescription,
    ) -> None:
        super().__init__(coordinator, key=description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value_fn(self.coordinator.data)

    async def _async_set(self, enabled: bool) -> None:
        if self.entity_description.key == "sharpness_enabled":
            await self.coordinator.async_set_sharpness(enabled=enabled)
            self.async_write_ha_state()
            return
        set_fn = self.entity_description.set_fn
        if set_fn is None:
            raise HomeAssistantError(
                f"Lumagen switch {self.entity_description.key!r} has no write path; "
                "an entity override was expected to handle it."
            )
        await set_fn(self.coordinator.client, self.coordinator.data, enabled)

    @lumagen_command
    async def async_turn_on(self, **kwargs: object) -> None:
        await self._async_set(True)

    @lumagen_command
    async def async_turn_off(self, **kwargs: object) -> None:
        await self._async_set(False)

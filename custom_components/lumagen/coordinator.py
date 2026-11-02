"""DataUpdateCoordinator wrapping aiolumagen's LumagenClient.

aiolumagen owns its own poll loop and pushes state via a subscribe callback.
This coordinator is push-first — ``update_interval`` is ``None`` — so the
only job of :meth:`_async_update_data` is to seed the initial state during
``async_config_entry_first_refresh``. Everything after that lands via
:meth:`_on_state_update`.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from aiolumagen import (
    HdrGammaMode,
    LumagenClient,
    LumagenConnectionError,
    LumagenError,
    LumagenState,
    LumagenTransport,
    SharpnessSensitivity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DEFAULT_POLL_INTERVAL, DOMAIN, FIRMWARE_POWER_QUERY_INTERVAL

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine

    from .release_coordinator import LumagenReleaseCoordinator

_LOGGER = logging.getLogger(__name__)

type LumagenConfigEntry = ConfigEntry[LumagenCoordinator]


def lumagen_command[**P, R](
    func: Callable[P, Awaitable[R]],
) -> Callable[P, Coroutine[Any, Any, R]]:
    """Report a command on a down link as a clear error instead of "Unknown error".

    Wraps every entity action and service that talks to the device. Home
    Assistant shows a ``HomeAssistantError``'s translated message to the user;
    anything else it reports as a generic "Unknown error", which is how a
    ``LumagenConnectionError`` ("Transport is not connected") reached them
    while the link was down.
    """

    @functools.wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await func(*args, **kwargs)
        except LumagenConnectionError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="not_connected",
                translation_placeholders={"error": str(err)},
            ) from err

    return wrapper


class LumagenCoordinator(DataUpdateCoordinator[LumagenState]):
    """Owns the LumagenClient and forwards state pushes into HA."""

    config_entry: LumagenConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: LumagenConfigEntry,
        client: LumagenClient,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=config_entry,
            always_update=False,
        )
        self.client = client
        self._unsubscribe: Callable[[], None] | None = None

        # Optimistic state for compound write-only commands.
        # The Lumagen has no query for the HDR intensity-mapping settings
        # (ZY417), so the number + select entities that touch it both
        # need a shared place to remember the last-set values. They live
        # here on the coordinator so a flip between entities doesn't lose
        # context. Defaults match the Lumagen's documented power-on state
        # (mapping disabled, auto gamma).
        self.hdr_mapping_max_nits: int = 0
        self.hdr_mapping_gamma_mode: HdrGammaMode = HdrGammaMode.AUTO

        # Last values we wrote via ZY521 (sharpness). Sharpness is a single
        # compound command — enabled + level + sensitivity go out together —
        # so changing one field means re-sending the other two. Device state
        # is preferred when known; these fill the gap when it isn't (the
        # window before the first ZQI30 reply lands, or firmware that never
        # answers it). Without them each entity defaulted the fields it
        # doesn't own, so setting the level silently reset sensitivity.
        self._last_sharpness_enabled: bool | None = None
        self._last_sharpness_level: int | None = None
        self._last_sharpness_sensitivity: SharpnessSensitivity | None = None

        # Firmware-update coordination. While ``firmware_update_active`` is set
        # the client is stopped (serial_proxy serves one subscriber, and the
        # firmware session needs it), so every entity except the update entity
        # reports unavailable and every domain service refuses. The lock makes
        # an install per-entry exclusive; ``reload_pending`` records an options
        # change that arrived mid-install, so its reload runs afterwards rather
        # than tearing the update entity down mid-flash.
        self.firmware_update_active: bool = False
        self.firmware_lock = asyncio.Lock()
        self.reload_pending: bool = False
        self.release_coordinator: LumagenReleaseCoordinator | None = None

    @callback
    def async_set_firmware_update_active(self, active: bool) -> None:
        """Flip the update gate and let every entity re-evaluate availability."""
        self.firmware_update_active = active
        self.async_update_listeners()

    async def async_pause_client(self) -> None:
        """Release the serial link for a firmware session. Idempotent."""
        await self.client.stop()

    async def async_resume_client(self) -> bool:
        """Restart the client after a firmware session.

        Returns False instead of raising, because the caller is in a
        ``finally`` and must not mask the session's own outcome. It does not
        schedule a reload itself: the caller does that as its very last step,
        so the reload can't tear the update entity down while it is still
        cleaning up.
        """
        try:
            await self.client.start()
        except (LumagenError, OSError) as err:
            _LOGGER.warning("Could not restart the Lumagen client after the update: %s", err)
            return False
        return True

    async def async_wait_for_power(self, on: bool, timeout: float) -> None:
        """Wait until the Lumagen reports ``power_on is on``.

        Re-queries power every ``FIRMWARE_POWER_QUERY_INTERVAL`` while waiting,
        because the client's own poll can be far apart. Raises the builtin
        :class:`TimeoutError` if the state never arrives.
        """
        if self.client.state.power_on is on:
            return
        reached = asyncio.Event()

        @callback
        def _check() -> None:
            if self.client.state.power_on is on:
                reached.set()

        unsubscribe = self.async_add_listener(_check)
        try:
            async with asyncio.timeout(timeout):
                while not reached.is_set():
                    try:
                        await self.client.query_power()
                    except LumagenError as err:
                        _LOGGER.debug("Power query while waiting failed: %s", err)
                    with suppress(TimeoutError):
                        async with asyncio.timeout(FIRMWARE_POWER_QUERY_INTERVAL):
                            await reached.wait()
                    # A push can land without a listener call if the coordinator
                    # already held equal data; re-check the client directly.
                    _check()
        finally:
            unsubscribe()

    async def _async_setup(self) -> None:
        """Start the aiolumagen client; called once by the coordinator.

        Errors here become ``ConfigEntryNotReady`` automatically — HA will
        retry setup on an exponential backoff. The subscription is wired
        before ``start()`` so the startup handshake's own responses feed
        straight into HA state. We raise without ``from err`` so HA logs
        a clean retry notice rather than the full stack trace — reconnect
        loops are expected, not bugs.

        After the handshake, we kick off queries for state that isn't part
        of aiolumagen's default startup sequence (sharpness, game mode,
        auto-aspect status). These aren't included in aiolumagen's handshake
        because not every consumer wants them; ha-lumagen entities do, so
        we ask for them here. Errors are tolerated — a slow Lumagen will
        still respond to the next 60s poll cycle, and the entities will
        remain ``unknown`` until then rather than blocking setup.
        """
        self._unsubscribe = self.client.subscribe(self._on_state_update)
        try:
            await self.client.start()
        except LumagenConnectionError as err:
            raise ConfigEntryNotReady(str(err)) from None
        except LumagenError as err:
            # Anything else from the library is unexpected at setup; surface
            # it with full context so it's debuggable.
            raise ConfigEntryNotReady(f"Unexpected Lumagen error: {err}") from err

        # Best-effort post-handshake queries. Don't propagate errors —
        # entity availability still works without them.
        #
        # query_input_labels is intentionally last: it serializes 8 label
        # queries, awaiting each !S1x reply before sending the next, so it's
        # the slowest of these even though it now finishes at device latency
        # rather than on a fixed timer. Running it after the quick
        # single-shot queries lets those populate first. Labels are read once
        # here; a mid-session relabel on the device won't refresh until the
        # integration reloads.
        for query in (
            self.client.query_sharpness,
            self.client.query_game_mode,
            self.client.query_auto_aspect,
            self.client.query_display_rec2020,
            self.client.query_source_hdr_status,
            self.client.query_input_labels,
        ):
            try:
                await query()
            except LumagenError as err:
                _LOGGER.debug("Initial %s query failed: %s", query.__name__, err)

    def effective_sharpness(self) -> tuple[bool, int, SharpnessSensitivity]:
        """Best-known ``(enabled, level, sensitivity)`` for a ZY521 write.

        Device-reported state wins; falls back to what we last wrote, then
        to conservative defaults (off, level 4, normal sensitivity).
        """
        state = self.data
        enabled = state.sharpness_enabled
        if enabled is None:
            enabled = self._last_sharpness_enabled
        level = state.sharpness_level
        if level is None:
            level = self._last_sharpness_level
        sensitivity = state.sharpness_sensitivity
        if sensitivity is None:
            sensitivity = self._last_sharpness_sensitivity
        return (
            enabled if enabled is not None else False,
            level if level is not None else 4,
            sensitivity if sensitivity is not None else SharpnessSensitivity.NORMAL,
        )

    async def async_set_sharpness(
        self,
        *,
        enabled: bool | None = None,
        level: int | None = None,
        sensitivity: SharpnessSensitivity | None = None,
    ) -> None:
        """Change one part of the sharpness triple, preserving the rest.

        ``ZY521ELS`` is a single compound command, so the two fields the
        caller didn't specify are carried over from
        :meth:`effective_sharpness` rather than re-defaulted. The written
        values are remembered so a follow-up change to a different field
        doesn't lose them if the device hasn't reported back yet.
        """
        current_enabled, current_level, current_sensitivity = self.effective_sharpness()
        new_enabled = current_enabled if enabled is None else enabled
        new_level = current_level if level is None else level
        new_sensitivity = current_sensitivity if sensitivity is None else sensitivity
        await self.client.set_sharpness(
            enabled=new_enabled, level=new_level, sensitivity=new_sensitivity
        )
        self._last_sharpness_enabled = new_enabled
        self._last_sharpness_level = new_level
        self._last_sharpness_sensitivity = new_sensitivity

    async def _async_update_data(self) -> LumagenState:
        """Seed the initial data after ``start()``.

        aiolumagen fires the callback each time anything changes, so by the
        time this method runs after ``_async_setup`` we already have a
        populated snapshot. The client's own poll loop handles ongoing
        freshness — ``update_interval`` is ``None`` here so this function
        is only hit via ``async_config_entry_first_refresh``.
        """
        if not self.client.connected:
            raise UpdateFailed("Lumagen client is not connected")
        return self.client.state

    async def async_shutdown(self) -> None:
        """Clean up the subscription and stop the client."""
        await super().async_shutdown()
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        await self.client.stop()

    @callback
    def _on_state_update(self, state: LumagenState, _codes: tuple[str, ...]) -> None:
        """Push-side path: aiolumagen -> coordinator -> entities."""
        self.async_set_updated_data(state)


def _stale_timeout_for(poll_interval: float) -> float:
    """Pick a staleness timeout that can't false-positive at this poll rate.

    aiolumagen requires ``stale_timeout`` to exceed the longest poll interval
    — it checks staleness right after sending a query, before the reply can
    arrive, so a timeout shorter than one cycle would trip a reconnect every
    cycle (it raises ValueError rather than let that happen).

    Scale with the interval and keep an absolute floor of 30s of slack for
    transient network delay. At the 60s default this yields exactly the 90s
    the library defaults to, so tuning the interval is the only behavior
    change.
    """
    return max(poll_interval * 1.5, poll_interval + 30.0)


async def create_lumagen_client(
    url: str, poll_interval: float = DEFAULT_POLL_INTERVAL
) -> LumagenClient:
    """Factory used by both config-flow validation and entry setup."""
    transport = LumagenTransport(url)
    return LumagenClient(
        transport,
        power_poll_interval=poll_interval,
        status_poll_interval=poll_interval,
        stale_timeout=_stale_timeout_for(poll_interval),
    )

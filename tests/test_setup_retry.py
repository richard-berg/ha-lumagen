"""An outage that spans setup retries is reported once, not once per attempt.

Regression: Home Assistant builds a new client for every setup retry, and each
new client's failed handshake logged a warning. On hardware, a three-minute
outage with a reload in it logged seven; HA's retry backoff would make that
about 45 an hour.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from aiolumagen import LumagenState
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.lumagen.const import CONF_URL, DOMAIN

CLIENT_FACTORY = "custom_components.lumagen.coordinator.create_lumagen_client"


def _client(*, connected: bool, available: bool) -> MagicMock:
    client = MagicMock()
    for name in (
        "start",
        "stop",
        "query_sharpness",
        "query_game_mode",
        "query_auto_aspect",
        "query_display_rec2020",
        "query_source_hdr_status",
        "query_input_labels",
    ):
        setattr(client, name, AsyncMock())
    client.connected = connected
    client.available = available
    client.subscribe = MagicMock(return_value=lambda: None)
    client.state = LumagenState(model="RadiancePro", firmware="030225")
    return client


def _down() -> MagicMock:
    """A bridge that accepted and dropped the connection: setup can't finish."""
    return _client(connected=False, available=False)


def _up() -> MagicMock:
    return _client(connected=True, available=True)


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_URL: "socket://127.0.0.1:1"}, unique_id="test_lumagen"
    )
    entry.add_to_hass(hass)
    return entry


def _announced(factory: AsyncMock) -> list[bool]:
    return [call.kwargs["outage_announced"] for call in factory.await_args_list]


async def _retry(hass: HomeAssistant) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=10))
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_setup_retries_during_an_outage_are_quiet(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    factory = AsyncMock(side_effect=[_down(), _down(), _down(), _up()])
    with patch(CLIENT_FACTORY, new=factory):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        assert entry.state is ConfigEntryState.SETUP_RETRY
        await _retry(hass)
        await _retry(hass)
        assert entry.state is ConfigEntryState.SETUP_RETRY
        await _retry(hass)
        assert entry.state is ConfigEntryState.LOADED

        # The first attempt reports why; the retries of the same outage don't.
        assert _announced(factory) == [False, True, True, True]

        # Back up, so the next outage is reported afresh.
        factory.side_effect = [_down()]
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
    assert _announced(factory)[-1] is False


async def test_reload_during_an_announced_outage_is_quiet(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    loaded = _up()
    factory = AsyncMock(side_effect=[loaded, _down(), _down()])
    with patch(CLIENT_FACTORY, new=factory):
        assert await hass.config_entries.async_setup(entry.entry_id)
        # The running client lost the device and announced it.
        loaded.available = False
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.SETUP_RETRY
        await _retry(hass)
    assert _announced(factory) == [False, True, True]


async def test_reload_of_a_healthy_entry_is_not_quiet(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    factory = AsyncMock(side_effect=[_up(), _up()])
    with patch(CLIENT_FACTORY, new=factory):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
    assert _announced(factory) == [False, False]

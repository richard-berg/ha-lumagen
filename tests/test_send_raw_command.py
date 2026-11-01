"""Tests for the lumagen.send_raw_command service."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiolumagen import LumagenState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from custom_components.lumagen.const import (
    ATTR_COMMAND,
    ATTR_CR,
    CONF_URL,
    DOMAIN,
    SERVICE_SEND_RAW_COMMAND,
)

FAKE_URL = "esphome://10.0.0.42:6053/?port_name=Lumagen&key=abc"
CLIENT_FACTORY = "custom_components.lumagen.coordinator.create_lumagen_client"


def _make_client_mock() -> MagicMock:
    client = MagicMock()
    client.start = AsyncMock()
    client.stop = AsyncMock()
    client.send_command = AsyncMock()
    client.query_sharpness = AsyncMock()
    client.query_game_mode = AsyncMock()
    client.query_auto_aspect = AsyncMock()
    client.query_display_rec2020 = AsyncMock()
    client.query_source_hdr_status = AsyncMock()
    client.query_input_labels = AsyncMock()
    client.connected = True
    client.subscribe = MagicMock(return_value=lambda: None)
    client.state = LumagenState(model="RadiancePro", firmware="030225")
    return client


async def _setup_entry(hass: HomeAssistant, client: MagicMock) -> None:
    """Bring up a Lumagen config entry against the supplied mock client."""
    with patch(CLIENT_FACTORY, new=AsyncMock(return_value=client)):
        # Bypass the config flow — register the entry directly.
        from pytest_homeassistant_custom_component.common import MockConfigEntry

        entry = MockConfigEntry(
            domain=DOMAIN,
            data={CONF_URL: FAKE_URL},
            unique_id="test_lumagen",
            title="Lumagen RadiancePro",
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


async def test_service_registered_on_setup(hass: HomeAssistant) -> None:
    """The service exists once a Lumagen entry is loaded."""
    assert not hass.services.has_service(DOMAIN, SERVICE_SEND_RAW_COMMAND)
    await _setup_entry(hass, _make_client_mock())
    assert hass.services.has_service(DOMAIN, SERVICE_SEND_RAW_COMMAND)


async def test_service_writes_through_to_client_with_cr(hass: HomeAssistant) -> None:
    """A successful call routes to client.send_command(cmd, cr=…, refresh=False)."""
    client = _make_client_mock()
    await _setup_entry(hass, client)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SEND_RAW_COMMAND,
        {ATTR_COMMAND: "ZY551", ATTR_CR: True},
        blocking=True,
    )

    # send_command receives (command, cr=True, refresh=False).
    # Find the call that matches our service invocation, ignoring the
    # startup-handshake calls the client made earlier.
    matching_calls = [
        c
        for c in client.send_command.await_args_list
        if c.args == ("ZY551",) and c.kwargs.get("cr") is True
    ]
    assert len(matching_calls) == 1
    assert matching_calls[0].kwargs.get("refresh") is False


async def test_service_defaults_cr_false(hass: HomeAssistant) -> None:
    """Omitting cr defaults to False — most non-ZY commands don't want it."""
    client = _make_client_mock()
    await _setup_entry(hass, client)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SEND_RAW_COMMAND,
        {ATTR_COMMAND: "%"},  # power-on, no CR needed
        blocking=True,
    )

    matching_calls = [c for c in client.send_command.await_args_list if c.args == ("%",)]
    assert len(matching_calls) == 1
    assert matching_calls[0].kwargs.get("cr") is False


async def test_service_accepts_a_device_target(hass: HomeAssistant) -> None:
    """A call carrying device_id must validate and route.

    Regression: services.yaml declared a mandatory entity target while the
    handler's schema permitted only command + cr. The UI therefore demanded
    a target that the schema would have rejected, making the service
    uncallable from Developer Tools either way.
    """
    from homeassistant.helpers import device_registry as dr

    client = _make_client_mock()
    await _setup_entry(hass, client)
    entry = hass.config_entries.async_entries(DOMAIN)[0]

    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "target-test")},
        name="Lumagen",
    )

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SEND_RAW_COMMAND,
        {ATTR_COMMAND: "ZQI55", "device_id": device.id},
        blocking=True,
    )

    assert [c for c in client.send_command.await_args_list if c.args == ("ZQI55",)]


async def test_service_accepts_an_entity_target(hass: HomeAssistant) -> None:
    """An entity target must validate and still reach the client.

    Back-compat: the old services.yaml declared an entity target, so an
    automation built against it may still pass entity_id. With a single
    configured Lumagen both the resolved-device path and the no-target
    fallback land on the same entry, so what this pins is that the schema
    accepts the key at all rather than rejecting the call outright.
    """
    client = _make_client_mock()
    await _setup_entry(hass, client)

    # Any entity this integration created will do.
    entity_id = next(
        state.entity_id
        for state in hass.states.async_all()
        if state.entity_id.endswith("_power_on") or ".lumagen" in state.entity_id
    )

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SEND_RAW_COMMAND,
        {ATTR_COMMAND: "ZQI56", "entity_id": entity_id},
        blocking=True,
    )

    assert [c for c in client.send_command.await_args_list if c.args == ("ZQI56",)]


async def test_service_rejects_unknown_device_target(hass: HomeAssistant) -> None:
    """A target that matches no loaded Lumagen must fail loudly."""
    client = _make_client_mock()
    await _setup_entry(hass, client)

    with pytest.raises(ServiceValidationError, match="None of the targeted devices"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SEND_RAW_COMMAND,
            {ATTR_COMMAND: "ZQS00", "device_id": "does-not-exist"},
            blocking=True,
        )


async def test_service_rejects_empty_command(hass: HomeAssistant) -> None:
    client = _make_client_mock()
    await _setup_entry(hass, client)

    # voluptuous's vol.Length raises MultipleInvalid; HA also accepts
    # ServiceValidationError. Either is fine — the point is the call
    # must not silently succeed and must not write to the client.
    import voluptuous as vol

    with pytest.raises((vol.Invalid, ServiceValidationError)):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SEND_RAW_COMMAND,
            {ATTR_COMMAND: ""},
            blocking=True,
        )


async def test_service_unloaded_when_last_entry_unloaded(hass: HomeAssistant) -> None:
    """When the last config entry goes away, the service is removed too."""
    client = _make_client_mock()
    await _setup_entry(hass, client)
    assert hass.services.has_service(DOMAIN, SERVICE_SEND_RAW_COMMAND)

    # Unload the entry.
    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    assert await hass.config_entries.async_unload(entries[0].entry_id)
    await hass.async_block_till_done()

    assert not hass.services.has_service(DOMAIN, SERVICE_SEND_RAW_COMMAND)


async def test_service_validation_error_when_no_entries_loaded(hass: HomeAssistant) -> None:
    """If the service is somehow called with no loaded entries, fail loudly.

    This is mostly defensive — HA only registers the service while at
    least one entry is loaded. But we register it idempotently and unload
    it in async_unload_entry, so during a brief teardown window a queued
    service call could land. Make sure that doesn't silently no-op.
    """
    # Manually register the service without loading an entry.
    from custom_components.lumagen import _async_register_services

    _async_register_services(hass)
    assert hass.services.has_service(DOMAIN, SERVICE_SEND_RAW_COMMAND)

    with pytest.raises(ServiceValidationError, match="No Lumagen config entries"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SEND_RAW_COMMAND,
            {ATTR_COMMAND: "ZQS00"},
            blocking=True,
        )


async def test_initial_setup_queries_phase1_state(hass: HomeAssistant) -> None:
    """Coordinator setup kicks off sharpness/game-mode/auto-aspect queries."""
    client = _make_client_mock()
    await _setup_entry(hass, client)

    client.query_sharpness.assert_awaited()
    client.query_game_mode.assert_awaited()
    client.query_auto_aspect.assert_awaited()
    client.query_input_labels.assert_awaited()


@pytest.fixture(autouse=True)
def _silence_config_entries_warnings():
    """Avoid sporadic `unawaited coroutine` warnings from the platform forwarders.

    We're poking at HA internals to set up entries quickly; some of the
    background discovery flows aren't fully shut down between tests. Not
    a real bug, but the warnings clutter the output.
    """
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        yield


class _NewDevice:
    """A device as Home Assistant 2026.12 shapes it; the old set is deprecated."""

    config_entry_id = "entry-a"

    @property
    def config_entries(self) -> set[str]:
        raise AssertionError("deprecated in Home Assistant 2026.12; must not be read")


class _OldDevice:
    """A device as releases before 2026.12 shape it."""

    config_entries = frozenset({"entry-a", "entry-b"})


def test_device_entry_ids_uses_config_entry_id_where_available() -> None:
    from custom_components.lumagen import _device_entry_ids

    assert _device_entry_ids(_NewDevice()) == {"entry-a"}
    assert _device_entry_ids(_OldDevice()) == {"entry-a", "entry-b"}

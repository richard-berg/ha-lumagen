"""Tests for the firmware update entity."""

from __future__ import annotations

import dataclasses
import itertools
from unittest.mock import AsyncMock, patch

import pytest
from aiolumagen.firmware import (
    RELEASES_URL,
    UpdatePhase,
    UpdatePlan,
    UpdateProgress,
    UpdateResult,
)
from homeassistant.components.update import (
    DATA_COMPONENT,
    UpdateDeviceClass,
    UpdateEntityFeature,
)
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.lumagen.const import DOMAIN
from custom_components.lumagen.update import LumagenFirmwareUpdateEntity

from .firmware_helpers import make_client, notification_messages, release_page, setup_entry

INSTALL_PATH = "custom_components.lumagen.firmware.async_install_firmware"


def _entity_id(hass: HomeAssistant) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        "update", DOMAIN, "test_lumagen_firmware_update"
    )
    assert entity_id is not None
    return entity_id


def _entity(hass: HomeAssistant) -> LumagenFirmwareUpdateEntity:
    entity = hass.data[DATA_COMPONENT].get_entity(_entity_id(hass))
    assert isinstance(entity, LumagenFirmwareUpdateEntity)
    return entity


async def test_beta_channel_offers_newest_release(hass: HomeAssistant) -> None:
    await setup_entry(hass, make_client(firmware="030225"))
    state = hass.states.get(_entity_id(hass))
    assert state is not None
    assert state.state == STATE_ON
    assert state.attributes["installed_version"] == "030225"
    assert state.attributes["latest_version"] == "030326"
    assert state.attributes["release_url"] == RELEASES_URL
    assert state.attributes["release_summary"] == "Invented beta note about widgets."


async def test_production_channel_offers_newest_production(hass: HomeAssistant) -> None:
    await setup_entry(
        hass, make_client(firmware="030225"), options={"firmware_channel": "production"}
    )
    state = hass.states.get(_entity_id(hass))
    assert state is not None
    assert state.attributes["latest_version"] == "030225"
    assert state.state == STATE_OFF


@pytest.mark.parametrize(
    ("installed", "channel", "expected"),
    [
        # 101524 is numerically bigger than 030225/030326 but a year older.
        ("101524", "beta", STATE_ON),
        ("120325", "beta", STATE_ON),
        ("030326", "beta", STATE_OFF),
        # Production is older than what's installed: never offered as an update.
        ("030326", "production", STATE_OFF),
    ],
)
async def test_state_uses_chronological_order(
    hass: HomeAssistant, installed: str, channel: str, expected: str
) -> None:
    await setup_entry(hass, make_client(firmware=installed), options={"firmware_channel": channel})
    state = hass.states.get(_entity_id(hass))
    assert state is not None
    assert state.state == expected


async def test_version_is_newer_fails_closed(hass: HomeAssistant) -> None:
    await setup_entry(hass, make_client())
    entity = _entity(hass)
    assert entity.version_is_newer("030225", "101524") is True
    assert entity.version_is_newer("030326", "120325") is True
    assert entity.version_is_newer("030225", "030326") is False
    assert entity.version_is_newer("030326", "030326") is False
    assert entity.version_is_newer("garbage", "030225") is False
    assert entity.version_is_newer("030326", "") is False
    assert entity.version_is_newer("133125", "030225") is False


async def test_static_attributes(hass: HomeAssistant) -> None:
    await setup_entry(hass, make_client())
    entity = _entity(hass)
    assert entity.supported_features == (
        UpdateEntityFeature.INSTALL
        | UpdateEntityFeature.PROGRESS
        | UpdateEntityFeature.RELEASE_NOTES
    )
    assert entity.device_class is UpdateDeviceClass.FIRMWARE
    assert entity.entity_category is EntityCategory.CONFIG
    assert entity.unique_id == "test_lumagen_firmware_update"


async def test_release_summary_capped_and_notes_markdown(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    long_note = "Invented " + "x" * 400
    aioclient_mock.clear_requests()
    aioclient_mock.get(RELEASES_URL, text=release_page(("030326", "Beta", "042826", long_note, 5)))
    await setup_entry(hass, make_client())
    entity = _entity(hass)
    assert entity.release_summary is not None
    assert len(entity.release_summary) == 255
    notes = await entity.async_release_notes()
    assert notes is not None
    assert "**Beta 030326**" in notes
    assert "Posted 2026-04-28" in notes
    assert long_note in notes
    assert "~5 minutes" in notes


async def test_availability_gating_during_install(hass: HomeAssistant) -> None:
    await setup_entry(hass, make_client())
    coordinator = hass.config_entries.async_entries(DOMAIN)[0].runtime_data
    update_id = _entity_id(hass)
    ent_reg = er.async_get(hass)
    sensor_id = ent_reg.async_get_entity_id("sensor", DOMAIN, "test_lumagen_firmware")
    connected_id = ent_reg.async_get_entity_id(
        "binary_sensor", DOMAIN, "test_lumagen_serial_connected"
    )
    button_id = ent_reg.async_get_entity_id("button", DOMAIN, "test_lumagen_power_on")
    others = [sensor_id, connected_id, button_id]
    assert all(others)

    coordinator.async_set_firmware_update_active(True)
    await hass.async_block_till_done()
    assert hass.states.get(update_id).state != STATE_UNAVAILABLE
    for entity_id in others:
        assert hass.states.get(entity_id).state == STATE_UNAVAILABLE, entity_id

    coordinator.async_set_firmware_update_active(False)
    await hass.async_block_till_done()
    for entity_id in others:
        assert hass.states.get(entity_id).state != STATE_UNAVAILABLE, entity_id


async def test_failed_release_check_keeps_latest_version(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    await setup_entry(hass, make_client())
    coordinator = hass.config_entries.async_entries(DOMAIN)[0].runtime_data
    aioclient_mock.clear_requests()
    aioclient_mock.get(RELEASES_URL, status=503)
    await coordinator.release_coordinator.async_refresh()
    await hass.async_block_till_done()
    assert not coordinator.release_coordinator.last_update_success

    state = hass.states.get(_entity_id(hass))
    assert state.state == STATE_ON
    assert state.attributes["latest_version"] == "030326"


async def test_no_release_known_leaves_latest_unset(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.clear_requests()
    aioclient_mock.get(RELEASES_URL, status=500)
    await setup_entry(hass, make_client())
    state = hass.states.get(_entity_id(hass))
    # Available, but with nothing to offer.
    assert state.state != STATE_UNAVAILABLE
    assert state.attributes["latest_version"] is None


async def test_device_sw_version_follows_firmware(hass: HomeAssistant) -> None:
    client = make_client(firmware="030225")
    await setup_entry(hass, client)
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    coordinator = entry.runtime_data

    def _device() -> dr.DeviceEntry:
        # Not async_get_device(identifiers=...): newer Home Assistant
        # deprecates it, and older releases lack its replacements.
        (device,) = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
        return device

    assert _device().sw_version == "030225"

    new_state = dataclasses.replace(client.state, firmware="030326")
    client.state = new_state
    coordinator.async_set_updated_data(new_state)
    await hass.async_block_till_done()

    assert _device().sw_version == "030326"
    assert hass.states.get(_entity_id(hass)).state == STATE_OFF


async def test_install_service_runs_orchestrator(hass: HomeAssistant) -> None:
    await setup_entry(hass, make_client(firmware="030225"))
    result = UpdateResult(
        plan=UpdatePlan(), written=("section0",), promoted=True, powered_down=True
    )
    install = AsyncMock(return_value=(result, "firmware_power_left_off"))
    with patch(INSTALL_PATH, install):
        await hass.services.async_call(
            "update", "install", {"entity_id": _entity_id(hass)}, blocking=True
        )
    install.assert_awaited_once()
    listing = install.await_args.args[2]
    assert listing.revision.mmddyy == "030326"
    state = hass.states.get(_entity_id(hass))
    assert state.attributes["in_progress"] is False
    assert state.attributes["update_percentage"] is None
    # The expected power-off is presented as the normal end of an update.
    (message,) = notification_messages(hass)
    assert "030326" in message
    assert "expected" in message
    assert "left off" in message


async def test_install_refuses_downgrade(hass: HomeAssistant) -> None:
    await setup_entry(
        hass, make_client(firmware="030326"), options={"firmware_channel": "production"}
    )
    install = AsyncMock()
    with (
        patch(INSTALL_PATH, install),
        pytest.raises(HomeAssistantError) as err,
    ):
        # HA's own guard only refuses latest == installed, so this reaches us.
        await hass.services.async_call(
            "update", "install", {"entity_id": _entity_id(hass)}, blocking=True
        )
    assert err.value.translation_key == "firmware_not_newer"
    install.assert_not_awaited()


async def test_install_refuses_without_listing(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.clear_requests()
    aioclient_mock.get(RELEASES_URL, status=500)
    await setup_entry(hass, make_client())
    install = AsyncMock()
    with patch(INSTALL_PATH, install), pytest.raises(HomeAssistantError) as err:
        await _entity(hass).async_install(None, False)
    assert err.value.translation_key == "firmware_no_release"
    install.assert_not_awaited()


async def test_progress_callback_writes_only_on_change(hass: HomeAssistant) -> None:
    await setup_entry(hass, make_client())
    entity = _entity(hass)
    with patch.object(entity, "async_write_ha_state") as write:

        def _push(phase: UpdatePhase, done: int, total: int) -> None:
            entity._on_progress(
                UpdateProgress(phase=phase, message="m", bytes_done=done, bytes_total=total)
            )

        _push(UpdatePhase.PLANNING, 0, 0)
        assert entity.update_percentage is None
        _push(UpdatePhase.WRITING, 0, 100)
        assert entity.update_percentage == 0
        _push(UpdatePhase.WRITING, 0, 100)  # unchanged: no write
        _push(UpdatePhase.WRITING, 50, 100)
        assert entity.update_percentage == 50
        _push(UpdatePhase.WRITING, 100, 100)
        assert entity.update_percentage == 100
        _push(UpdatePhase.VERIFYING, 10, 100)
        assert entity.update_percentage is None
    assert write.call_count == 5


def _two_section_run() -> list[UpdateProgress]:
    """A run_update-shaped event stream writing section 1 then section 0.

    Each erase and write carries its own per-phase byte fraction (restarting at
    zero) alongside a whole-run ``overall``, the way the library reports them.
    """
    events = [
        UpdateProgress(phase=UpdatePhase.PREFLIGHT, message="m", overall=0.0),
        UpdateProgress(phase=UpdatePhase.PLANNING, message="m", overall=0.0),
        UpdateProgress(phase=UpdatePhase.RATE_CHANGE, message="m", overall=0.01),
    ]
    # (section, phase, units, overall at start, overall at end)
    spans = [
        ("section1", UpdatePhase.ERASING, 50, 0.01, 0.10),
        ("section1", UpdatePhase.WRITING, 772, 0.10, 0.60),
        ("section0", UpdatePhase.ERASING, 10, 0.62, 0.66),
        ("section0", UpdatePhase.WRITING, 112, 0.66, 0.95),
    ]
    for section, phase, units, start, end in spans:
        for done in range(units + 1):
            events.append(
                UpdateProgress(
                    phase=phase,
                    message="m",
                    section=section,
                    bytes_done=done * 4096,
                    bytes_total=units * 4096,
                    overall=start + (end - start) * done / units,
                )
            )
        if section == "section1" and phase is UpdatePhase.WRITING:
            events.append(
                UpdateProgress(
                    phase=UpdatePhase.VERIFYING, message="m", section=section, overall=0.61
                )
            )
            events.append(
                UpdateProgress(
                    phase=UpdatePhase.COMMITTING, message="m", section=section, overall=0.62
                )
            )
    events += [
        UpdateProgress(phase=UpdatePhase.VERIFYING, message="m", section="section0", overall=0.96),
        UpdateProgress(phase=UpdatePhase.PROMOTING, message="m", section="section0", overall=0.98),
        UpdateProgress(phase=UpdatePhase.DONE, message="update complete", overall=1.0),
    ]
    return events


def _times_reaching_100(series: list[int | None]) -> int:
    """How many separate times the bar arrives at 100%."""
    return sum(1 for a, b in itertools.pairwise([None, *series]) if b == 100 and a != 100)


async def test_progress_uses_overall_for_one_bar(hass: HomeAssistant) -> None:
    """Two sections' erase and write make one rising bar, not four 0-100 runs."""
    await setup_entry(hass, make_client())
    entity = _entity(hass)
    events = _two_section_run()
    series: list[int | None] = []
    with patch.object(entity, "async_write_ha_state") as write:
        for progress in events:
            entity._on_progress(progress)
            series.append(entity.update_percentage)

    assert None not in series
    assert all(b >= a for a, b in itertools.pairwise(series)), "bar went backwards"
    assert series[0] == 0
    assert series[-1] == 100
    # The per-phase fraction hit 100% four times; the bar only reaches it once,
    # and only on DONE.
    assert _times_reaching_100(series) == 1
    assert series.index(100) == len(series) - 1
    # Writes stay bounded by distinct percentages plus phase changes, well
    # under one per block (~950 events here).
    assert write.call_count <= 101 + 12
    assert write.call_count < len(events) / 5


async def test_progress_without_overall_falls_back_per_phase(hass: HomeAssistant) -> None:
    """With no ``overall`` the bar follows each erase/write phase, as before."""
    await setup_entry(hass, make_client())
    entity = _entity(hass)
    series: list[int | None] = []
    with patch.object(entity, "async_write_ha_state"):
        for progress in _two_section_run():
            entity._on_progress(dataclasses.replace(progress, overall=None))
            series.append(entity.update_percentage)

    # Only erase/write report a percentage, and it restarts per phase.
    assert series[:3] == [None, None, None]
    assert series[-1] is None
    assert _times_reaching_100(series) == 4
    assert any(b is not None and a is not None and b < a for a, b in itertools.pairwise(series))

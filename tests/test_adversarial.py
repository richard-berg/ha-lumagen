"""Deterministic adversarial sequences across the HA/library boundary."""

from __future__ import annotations

import asyncio
import dataclasses
import random
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiolumagen import HdrGammaMode, LumagenCommandError, LumagenState, SharpnessSensitivity
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.lumagen.const import CONF_URL, DOMAIN
from custom_components.lumagen.media_player import LumagenMediaPlayer
from custom_components.lumagen.number import NUMBERS, LumagenNumber
from custom_components.lumagen.select import SELECTS, LumagenSelect

from .firmware_helpers import make_client, setup_entry


@pytest.mark.parametrize(
    "labels",
    [
        {1: "Input 2"},
        {1: "Input 2", 2: "Input 3"},
        {1: "Input 2", 2: "TV", 3: "TV"},
    ],
)
async def test_source_labels_cannot_hide_or_misroute_an_input(labels: dict[int, str]) -> None:
    entity = LumagenMediaPlayer.__new__(LumagenMediaPlayer)
    entity.coordinator = MagicMock()
    entity.coordinator.data = LumagenState(input_labels=labels)
    entity.coordinator.client.set_input = AsyncMock()
    mapping = entity._source_map()
    assert len(mapping) == 8
    assert set(mapping.values()) == set(range(1, 9))
    for label, number in mapping.items():
        await entity.async_select_source(label)
        entity.coordinator.client.set_input.assert_awaited_with(number)


def test_source_label_fuzz_preserves_every_input() -> None:
    rng = random.Random(20261102)
    entity = LumagenMediaPlayer.__new__(LumagenMediaPlayer)
    entity.coordinator = MagicMock()
    choices = ["", "TV", " TV ", *(f"Input {n}" for n in range(1, 9))]
    for _ in range(2000):
        labels = {n: rng.choice(choices) for n in range(1, 9)}
        entity.coordinator.data = LumagenState(input_labels=labels)
        mapping = entity._source_map()
        assert len(mapping) == 8, labels
        assert set(mapping.values()) == set(range(1, 9)), labels


@pytest.mark.parametrize("entity_id", ["sensor.deleted_lumagen", "sensor.unrelated"])
async def test_invalid_entity_target_never_falls_back_to_first_device(
    hass: HomeAssistant, entity_id: str
) -> None:
    client = make_client()
    await setup_entry(hass, client)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "send_raw_command",
            {"command": "%", "entity_id": entity_id},
            blocking=True,
        )
    client.send_command.assert_not_awaited()


async def test_area_target_cannot_silently_command_an_unrelated_device(
    hass: HomeAssistant,
) -> None:
    client = make_client()
    await setup_entry(hass, client)
    with pytest.raises(ServiceValidationError, match="Area targets"):
        await hass.services.async_call(
            DOMAIN,
            "send_raw_command",
            {"command": "%", "area_id": "some-other-room"},
            blocking=True,
        )
    client.send_command.assert_not_awaited()


@pytest.mark.parametrize("targeted", [False, True])
async def test_ambiguous_multiple_devices_never_receive_a_partial_command(
    hass: HomeAssistant, targeted: bool
) -> None:
    first_client, second_client = make_client(), make_client()
    first = await setup_entry(hass, first_client)
    second = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_URL: "socket://127.0.0.1:2"},
        unique_id="second_lumagen",
        title="Second Lumagen",
    )
    second.add_to_hass(hass)
    with patch(
        "custom_components.lumagen.coordinator.create_lumagen_client",
        AsyncMock(return_value=second_client),
    ):
        assert await hass.config_entries.async_setup(second.entry_id)
        await hass.async_block_till_done()
    registry = dr.async_get(hass)
    first_device = dr.async_entries_for_config_entry(registry, first.entry_id)[0]
    second_device = dr.async_entries_for_config_entry(registry, second.entry_id)[0]
    data: dict[str, object] = {"command": "%"}
    if targeted:
        data["device_id"] = [first_device.id, second_device.id]
    with pytest.raises(ServiceValidationError, match="exactly one"):
        await hass.services.async_call(DOMAIN, "send_raw_command", data, blocking=True)
    first_client.send_command.assert_not_awaited()
    second_client.send_command.assert_not_awaited()

    await hass.services.async_call(
        DOMAIN,
        "send_raw_command",
        {"command": "%", "device_id": second_device.id},
        blocking=True,
    )
    first_client.send_command.assert_not_awaited()
    second_client.send_command.assert_awaited_once_with("%", cr=False, refresh=False)


async def test_overlapping_sharpness_changes_preserve_each_other(hass: HomeAssistant) -> None:
    client = make_client()
    entered, release = asyncio.Event(), asyncio.Event()

    async def write(**_kwargs: object) -> None:
        entered.set()
        await release.wait()

    client.set_sharpness = AsyncMock(side_effect=write)
    entry = await setup_entry(hass, client)
    coordinator = entry.runtime_data
    first = asyncio.create_task(
        coordinator.async_set_sharpness(sensitivity=SharpnessSensitivity.HIGH)
    )
    await entered.wait()
    second = asyncio.create_task(coordinator.async_set_sharpness(level=7))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second)
    assert client.set_sharpness.await_args_list[-1].kwargs == {
        "enabled": False,
        "level": 7,
        "sensitivity": SharpnessSensitivity.HIGH,
    }


async def test_sharpness_changes_do_not_merge_with_stale_readback(hass: HomeAssistant) -> None:
    client = make_client()
    client.state = LumagenState(
        model="RadiancePro",
        sharpness_enabled=True,
        sharpness_level=2,
        sharpness_sensitivity=SharpnessSensitivity.NORMAL,
    )
    client.set_sharpness = AsyncMock()
    entry = await setup_entry(hass, client)
    await entry.runtime_data.async_set_sharpness(sensitivity=SharpnessSensitivity.HIGH)
    await entry.runtime_data.async_set_sharpness(level=7)
    assert client.set_sharpness.await_args_list[-1].kwargs["sensitivity"] == "H"


async def test_equal_fresh_sharpness_report_rejects_pending_write(hass: HomeAssistant) -> None:
    client = make_client()
    client.state = LumagenState(
        model="RadiancePro",
        sharpness_enabled=True,
        sharpness_level=2,
        sharpness_sensitivity=SharpnessSensitivity.NORMAL,
    )
    client.set_sharpness = AsyncMock()
    coordinator = (await setup_entry(hass, client)).runtime_data
    await coordinator.async_set_sharpness(sensitivity=SharpnessSensitivity.HIGH)
    # Equal-valued reports advance freshness without a state-change callback.
    client.state = dataclasses.replace(client.state, sharpness_report_generation=1)
    await coordinator.async_set_sharpness(level=7)
    assert client.set_sharpness.await_args_list[-1].kwargs["sensitivity"] == "N"


async def test_sharpness_report_during_write_is_not_overridden_by_optimism(
    hass: HomeAssistant,
) -> None:
    client = make_client()
    client.state = LumagenState(
        sharpness_enabled=True,
        sharpness_level=2,
        sharpness_sensitivity=SharpnessSensitivity.NORMAL,
    )

    async def write(**_kwargs: object) -> None:
        client.state = dataclasses.replace(
            client.state,
            sharpness_report_generation=client.state.sharpness_report_generation + 1,
        )

    client.set_sharpness = AsyncMock(side_effect=write)
    coordinator = (await setup_entry(hass, client)).runtime_data
    await coordinator.async_set_sharpness(sensitivity=SharpnessSensitivity.HIGH)
    assert coordinator.effective_sharpness() == (True, 2, SharpnessSensitivity.NORMAL)


async def test_overlapping_hdr_changes_preserve_each_other(hass: HomeAssistant) -> None:
    client = make_client()
    entered, release = asyncio.Event(), asyncio.Event()

    async def write(**_kwargs: object) -> None:
        entered.set()
        await release.wait()

    client.set_hdr_intensity_mapping = AsyncMock(side_effect=write)
    coordinator = (await setup_entry(hass, client)).runtime_data
    number = LumagenNumber(coordinator, next(d for d in NUMBERS if d.key == "hdr_mapping_max_nits"))
    select = LumagenSelect(coordinator, next(d for d in SELECTS if d.key == "hdr_gamma_mode"))
    number.async_write_ha_state = MagicMock()
    select.async_write_ha_state = MagicMock()
    first = asyncio.create_task(select.async_select_option("hdr"))
    await entered.wait()
    second = asyncio.create_task(number.async_set_native_value(1500))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second)
    assert client.set_hdr_intensity_mapping.await_args_list[-1].kwargs == {
        "display_max_nits": 1500,
        "gamma_mode": HdrGammaMode.HDR,
    }


async def test_inflight_remote_sequence_stops_at_firmware_gate(hass: HomeAssistant) -> None:
    from custom_components.lumagen.remote import LumagenRemote

    client = make_client()
    coordinator = (await setup_entry(hass, client)).runtime_data
    entity = LumagenRemote(coordinator)

    async def write(_command: str) -> None:
        coordinator.async_set_firmware_update_active(True)

    client.send_command.side_effect = write
    with pytest.raises(HomeAssistantError) as err:
        await entity.async_send_command(["menu", "down", "ok"], delay_secs=0)
    assert err.value.translation_key == "firmware_update_active"
    assert client.send_command.await_count == 1


async def test_direct_entity_action_cannot_bypass_firmware_gate(hass: HomeAssistant) -> None:
    client = make_client()
    coordinator = (await setup_entry(hass, client)).runtime_data
    coordinator.async_set_firmware_update_active(True)
    entity = LumagenMediaPlayer(coordinator)
    with pytest.raises(HomeAssistantError) as err:
        await entity.async_turn_off()
    assert err.value.translation_key == "firmware_update_active"
    client.standby.assert_not_awaited()


async def test_repeated_outages_and_reloads_do_not_leak_subscriptions(
    hass: HomeAssistant,
) -> None:
    client = make_client()
    callbacks: set[Callable[..., None]] = set()

    def subscribe(callback: Callable[..., None]) -> Callable[[], None]:
        callbacks.add(callback)
        return lambda: callbacks.discard(callback)

    client.subscribe.side_effect = subscribe
    entry = await setup_entry(hass, client)
    original_entities = set(hass.states.async_entity_ids())
    registry = er.async_get(hass)
    player_id = registry.async_get_entity_id("media_player", DOMAIN, "test_lumagen_media_player")
    link_id = registry.async_get_entity_id("binary_sensor", DOMAIN, "test_lumagen_serial_connected")
    assert player_id is not None and link_id is not None
    with patch(
        "custom_components.lumagen.coordinator.create_lumagen_client",
        AsyncMock(return_value=client),
    ):
        for _ in range(12):
            assert len(callbacks) == 1
            for available in (False, True):
                client.available = available
                for callback in tuple(callbacks):
                    callback(client.state, ())
                await hass.async_block_till_done()
                player_state = hass.states.get(player_id)
                link_state = hass.states.get(link_id)
                assert player_state is not None and link_state is not None
                assert (player_state.state != STATE_UNAVAILABLE) is available
                assert link_state.state == ("on" if available else "off")
            assert await hass.config_entries.async_reload(entry.entry_id)
            await hass.async_block_till_done()
            assert set(hass.states.async_entity_ids()) == original_entities
        assert await hass.config_entries.async_unload(entry.entry_id)
    assert not callbacks


async def test_invalid_osd_message_cannot_change_sticky_block_character(
    hass: HomeAssistant,
) -> None:
    client = make_client()
    client.set_osd_block_char = AsyncMock()
    client.show_message = AsyncMock(side_effect=LumagenCommandError("invalid message"))
    await setup_entry(hass, client)
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            DOMAIN,
            "send_osd_message",
            {"message": "wrapped", "line1": "verbatim", "block_char": "#"},
            blocking=True,
        )
    assert err.value.translation_key == "invalid_command"
    client.set_osd_block_char.assert_not_awaited()
    client.show_message.assert_not_awaited()


async def test_overlapping_osd_messages_keep_their_own_block_character(
    hass: HomeAssistant,
) -> None:
    client = make_client()
    entered, release = asyncio.Event(), asyncio.Event()
    writes: list[str] = []

    async def block(char: str) -> None:
        writes.append(char)
        entered.set()
        await release.wait()

    async def message(text: str, **_kwargs: object) -> None:
        writes.append(text)

    client.set_osd_block_char = AsyncMock(side_effect=block)
    client.show_message = AsyncMock(side_effect=message)
    await setup_entry(hass, client)

    async def send(char: str) -> None:
        await hass.services.async_call(
            DOMAIN,
            "send_osd_message",
            {"message": f"bar {char}", "block_char": char},
            blocking=True,
        )

    first = asyncio.create_task(send("#"))
    await entered.wait()
    second = asyncio.create_task(send("@"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second)
    assert writes == ["#", "bar #", "@", "bar @"]


async def test_invalid_library_command_is_a_translated_user_error(hass: HomeAssistant) -> None:
    client = make_client()
    client.send_command.side_effect = LumagenCommandError("ASCII required")
    await setup_entry(hass, client)
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            DOMAIN, "send_raw_command", {"command": "\u2603"}, blocking=True
        )
    assert err.value.translation_key == "invalid_command"
    assert err.value.translation_placeholders == {"error": "ASCII required"}


async def test_repeated_cancelled_power_waits_remove_their_listeners(hass: HomeAssistant) -> None:
    client = make_client(power_on=False)
    coordinator = (await setup_entry(hass, client)).runtime_data
    listeners = len(coordinator._listeners)
    queried = asyncio.Event()

    async def query() -> None:
        queried.set()

    client.query_power.side_effect = query
    for _ in range(100):
        queried.clear()
        task = asyncio.create_task(coordinator.async_wait_for_power(True, 10))
        await queried.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(coordinator._listeners) == listeners


@pytest.mark.parametrize("kind", ["sharpness", "hdr"])
async def test_queued_compound_action_rechecks_firmware_ownership(
    hass: HomeAssistant, kind: str
) -> None:
    client = make_client()
    coordinator = (await setup_entry(hass, client)).runtime_data
    entered, release = asyncio.Event(), asyncio.Event()

    async def write(**_kwargs: object) -> None:
        entered.set()
        await release.wait()

    client.set_sharpness = AsyncMock(side_effect=write)
    client.set_hdr_intensity_mapping = AsyncMock(side_effect=write)
    operation = (
        coordinator.async_set_sharpness
        if kind == "sharpness"
        else coordinator.async_set_hdr_mapping
    )
    first = asyncio.create_task(operation())
    await entered.wait()
    second = asyncio.create_task(operation())
    await asyncio.sleep(0)
    coordinator.async_set_firmware_update_active(True)
    release.set()
    await first
    with pytest.raises(HomeAssistantError) as err:
        await second
    assert err.value.translation_key == "firmware_update_active"
    coordinator.async_set_firmware_update_active(False)

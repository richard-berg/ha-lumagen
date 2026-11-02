"""Commands sent while the link is down fail with a clear, translated error.

Regression: a ``LumagenConnectionError`` escaped entity actions and services
unhandled, so Home Assistant showed the user only "Unknown error". Seen on
hardware in the window after the bridge closed the connection.
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiolumagen import LumagenConnectionError, LumagenState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.lumagen.button import LumagenButton
from custom_components.lumagen.const import CONF_URL, DOMAIN
from custom_components.lumagen.media_player import LumagenMediaPlayer
from custom_components.lumagen.number import LumagenNumber
from custom_components.lumagen.remote import LumagenRemote
from custom_components.lumagen.select import LumagenSelect
from custom_components.lumagen.switch import LumagenSwitch

CLIENT_FACTORY = "custom_components.lumagen.coordinator.create_lumagen_client"
DOWN = LumagenConnectionError("Transport is not connected")


def _down_client() -> MagicMock:
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
    for name in (
        "send_command",
        "power_on",
        "standby",
        "restart_input",
        "set_game_mode",
        "query_full_status",
    ):
        setattr(client, name, AsyncMock(side_effect=DOWN))
    client.connected = True
    client.available = True
    client.subscribe = MagicMock(return_value=lambda: None)
    client.state = LumagenState(model="RadiancePro", firmware="030225", power_on=True)
    return client


async def _setup(hass: HomeAssistant) -> er.EntityRegistry:
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_URL: "socket://127.0.0.1:1"}, unique_id="test_lumagen"
    )
    entry.add_to_hass(hass)
    with patch(CLIENT_FACTORY, new=AsyncMock(return_value=_down_client())):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return er.async_get(hass)


def _entity_id(registry: er.EntityRegistry, platform: str, key: str) -> str:
    entity_id = registry.async_get_entity_id(platform, DOMAIN, f"test_lumagen_{key}")
    assert entity_id is not None, key
    return entity_id


@pytest.mark.parametrize(
    ("platform", "key", "service", "data"),
    [
        ("button", "query_status", "press", {}),
        ("media_player", "media_player", "turn_on", {}),
        ("remote", "remote", "send_command", {"command": "menu"}),
        ("switch", "game_mode", "turn_on", {}),
    ],
)
async def test_entity_action_on_a_down_link_raises_a_translated_error(
    hass: HomeAssistant, platform: str, key: str, service: str, data: dict[str, str]
) -> None:
    registry = await _setup(hass)
    with pytest.raises(HomeAssistantError) as caught:
        await hass.services.async_call(
            platform,
            service,
            {"entity_id": _entity_id(registry, platform, key), **data},
            blocking=True,
        )
    assert caught.value.translation_domain == DOMAIN
    assert caught.value.translation_key == "not_connected"
    assert "Transport is not connected" in str(caught.value)


@pytest.mark.parametrize(
    ("service", "data"),
    [
        ("send_raw_command", {"command": "ZQS01"}),
        ("restart_input", {}),
    ],
)
async def test_service_on_a_down_link_raises_a_translated_error(
    hass: HomeAssistant, service: str, data: dict[str, str]
) -> None:
    await _setup(hass)
    with pytest.raises(HomeAssistantError) as caught:
        await hass.services.async_call(DOMAIN, service, data, blocking=True)
    assert caught.value.translation_key == "not_connected"


_ACTIONS = {
    "async_press",
    "async_select_option",
    "async_set_native_value",
    "async_turn_on",
    "async_turn_off",
    "async_select_source",
    "async_send_command",
}


@pytest.mark.parametrize(
    "entity_class",
    [
        LumagenButton,
        LumagenMediaPlayer,
        LumagenNumber,
        LumagenRemote,
        LumagenSelect,
        LumagenSwitch,
    ],
)
def test_every_entity_action_translates_connection_errors(entity_class: type) -> None:
    """A new action added without the wrapper would bring "Unknown error" back."""
    actions = _ACTIONS & set(vars(entity_class))
    assert actions
    for name in actions:
        method = vars(entity_class)[name]
        assert inspect.iscoroutinefunction(method)
        assert hasattr(method, "__wrapped__"), f"{entity_class.__name__}.{name}"

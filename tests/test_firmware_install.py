"""Tests for the firmware install orchestrator (custom_components.lumagen.firmware).

The firmware session is patched out: these test the HA-side sequence —
download, cross-check, auto power-on and settle, client pause/resume, power
restore and exception mapping — never a real transfer.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Awaitable, Callable, Iterator
from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiolumagen import (
    LumagenConnectionError,
    LumagenFirmwareAbortError,
    LumagenFirmwareError,
)
from aiolumagen.firmware import (
    SESSION_BAUD,
    FirmwareBundle,
    FirmwareRevision,
    ReleaseChannel,
    ReleaseListing,
    UpdatePlan,
    UpdateResult,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)
from yarl import URL

from custom_components.lumagen import firmware
from custom_components.lumagen.const import DOMAIN, FIRMWARE_POWER_ON_SETTLE
from custom_components.lumagen.coordinator import LumagenCoordinator

from .firmware_helpers import HASS_URL, make_client, notification_messages, setup_entry, updater_zip

PROMOTED = UpdateResult(
    plan=UpdatePlan(),
    written=("section1", "section0"),
    promoted=True,
    powered_down=True,
    notes=("synthetic note",),
)
# The plan found nothing to write: no reboot owed, so no power-down.
NOTHING_WRITTEN = UpdateResult(plan=UpdatePlan(), written=(), promoted=False, powered_down=False)


@dataclass
class Rig:
    hass: HomeAssistant
    client: MagicMock
    coordinator: LumagenCoordinator
    listing: ReleaseListing
    session: MagicMock
    session_cm: MagicMock
    session_factory: MagicMock
    settle: AsyncMock
    calls: list[str] = field(default_factory=list)
    # What the device does: does it answer power-on, and does it report
    # standby after a post-update restart?
    answers_power_on: bool = True
    reports_standby_after_update: bool = True
    active_during_session: list[bool] = field(default_factory=list)
    # Runs inside run_update, i.e. while the session holds the serial proxy.
    during_session: Callable[[], Awaitable[None]] | None = None

    def set_power(self, on: bool) -> None:
        self.client.state = dataclasses.replace(self.client.state, power_on=on)
        self.coordinator.async_set_updated_data(self.client.state)

    async def install(self, **kwargs: Any) -> tuple[UpdateResult, str]:
        return await firmware.async_install_firmware(
            self.hass, self.coordinator, self.listing, progress=lambda _p: None, **kwargs
        )


@pytest.fixture
def fast_timeouts() -> Iterator[None]:
    with (
        patch.object(firmware, "FIRMWARE_POWER_ON_TIMEOUT", 0.2),
        patch.object(firmware, "FIRMWARE_POST_UPDATE_STANDBY_TIMEOUT", 0.2),
        patch("custom_components.lumagen.coordinator.FIRMWARE_POWER_QUERY_INTERVAL", 0.01),
    ):
        yield


async def _rig(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    *,
    power_on: bool,
    result: UpdateResult | None = PROMOTED,
    run_exc: BaseException | None = None,
    enter_exc: BaseException | None = None,
    bundle_release: str = "030326",
    zip_bytes: bytes | None = None,
    zip_status: int = 200,
    register_zip: bool = True,
) -> Rig:
    client = make_client(firmware="030225", power_on=power_on)
    await setup_entry(hass, client)
    coordinator: LumagenCoordinator = hass.config_entries.async_entries(DOMAIN)[0].runtime_data
    assert coordinator.release_coordinator is not None
    listing = coordinator.release_coordinator.latest_for(ReleaseChannel.BETA)
    assert listing is not None and listing.revision.mmddyy == "030326"
    if register_zip:
        aioclient_mock.get(
            listing.url,
            status=zip_status,
            content=zip_bytes if zip_bytes is not None else updater_zip(),
        )

    session = MagicMock()
    session_cm = MagicMock()
    session_cm.__aexit__ = AsyncMock(return_value=False)
    session_factory = MagicMock(return_value=session_cm)
    rig = Rig(
        hass=hass,
        client=client,
        coordinator=coordinator,
        listing=listing,
        session=session,
        session_cm=session_cm,
        session_factory=session_factory,
        settle=AsyncMock(),
    )

    async def _enter() -> MagicMock:
        rig.calls.append("session")
        if enter_exc is not None:
            raise enter_exc
        return session

    async def _run(*_args: Any, **_kwargs: Any) -> UpdateResult:
        rig.active_during_session.append(coordinator.firmware_update_active)
        if rig.during_session is not None:
            await rig.during_session()
        if run_exc is not None:
            raise run_exc
        assert result is not None
        return result

    session_cm.__aenter__ = AsyncMock(side_effect=_enter)
    session.run_update = AsyncMock(side_effect=_run)

    async def _power_on() -> None:
        rig.calls.append("power_on")
        if rig.answers_power_on:
            rig.set_power(True)

    async def _standby() -> None:
        rig.calls.append("standby")
        rig.set_power(False)

    async def _stop() -> None:
        rig.calls.append("stop")

    async def _start() -> None:
        rig.calls.append("start")
        # The handshake after a Z97 sees the unit in standby.
        if (
            rig.reports_standby_after_update
            and result is not None
            and result.powered_down
            and run_exc is None
            and enter_exc is None
        ):
            rig.set_power(False)

    client.power_on.side_effect = _power_on
    client.standby.side_effect = _standby
    client.stop.side_effect = _stop
    client.start.side_effect = _start

    bundle = FirmwareBundle(
        release=FirmwareRevision.parse(bundle_release), source_name="radiance_pro030326.exe"
    )
    _use(patch.object(firmware, "FirmwareSession", session_factory))
    _use(patch.object(firmware, "extract_images", MagicMock(return_value=bundle)))
    _use(patch.object(firmware, "_async_settle", rig.settle))
    return rig


@pytest.fixture(autouse=True)
def _patches() -> Iterator[ExitStack]:
    """Patches owned by this module, undone after each test.

    Not ``patch.stopall()``: that would also stop the patchers
    pytest-homeassistant-custom-component started itself (socket blocking,
    the HTTP server stub).
    """
    with ExitStack() as stack:
        _STACK.append(stack)
        yield stack
        _STACK.pop()


_STACK: list[ExitStack] = []


def _use(p: Any) -> Any:
    return _STACK[-1].enter_context(p)


# ---------------------------------------------------------------------------
# Happy paths and the four power-restore branches
# ---------------------------------------------------------------------------


async def test_on_success_powered_down_repowers(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, fast_timeouts: None
) -> None:
    rig = await _rig(hass, aioclient_mock, power_on=True)
    reload = _use(patch.object(hass.config_entries, "async_schedule_reload"))

    result, power = await rig.install()

    assert result is PROMOTED
    assert power == firmware.POWER_RESTORED
    assert rig.calls == ["stop", "session", "start", "power_on"]
    # The entry URL verbatim, never a rebuilt esphome:// URL.
    rig.session_factory.assert_called_once_with(HASS_URL, baudrate=SESSION_BAUD)
    kwargs = rig.session.run_update.await_args.kwargs
    assert kwargs["baudrate"] == 230400
    # Library defaults: promote, and let the plan choose the sections.
    assert "promote" not in kwargs
    assert "only" not in kwargs
    assert rig.active_during_session == [True]
    assert rig.coordinator.firmware_update_active is False
    assert not rig.coordinator.firmware_lock.locked()
    rig.settle.assert_not_awaited()
    reload.assert_not_called()


async def test_firmware_session_is_pinned_to_the_selected_device(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, fast_timeouts: None
) -> None:
    rig = await _rig(hass, aioclient_mock, power_on=True)
    rig.client.state = dataclasses.replace(rig.client.state, model_number="1018", serial="003830")
    await rig.install()
    kwargs = rig.session.run_update.await_args.kwargs
    assert kwargs["expected_model_number"] == "1018"
    assert kwargs["expected_serial"] == "003830"


async def test_off_success_powered_down_left_off(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, fast_timeouts: None
) -> None:
    rig = await _rig(hass, aioclient_mock, power_on=False)

    _result, power = await rig.install()

    assert power == firmware.POWER_LEFT_OFF
    # Auto power-on happens before the client stops; nothing after the update.
    assert rig.calls == ["power_on", "stop", "session", "start"]
    rig.settle.assert_awaited_once_with(FIRMWARE_POWER_ON_SETTLE)
    assert FIRMWARE_POWER_ON_SETTLE == 15.0


async def test_off_success_not_powered_down_returns_to_standby(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, fast_timeouts: None
) -> None:
    rig = await _rig(hass, aioclient_mock, power_on=False, result=NOTHING_WRITTEN)

    _result, power = await rig.install()

    assert power == firmware.POWER_STANDBY
    assert rig.calls == ["power_on", "stop", "session", "start", "standby"]


async def test_on_success_not_powered_down_leaves_power(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, fast_timeouts: None
) -> None:
    rig = await _rig(hass, aioclient_mock, power_on=True, result=NOTHING_WRITTEN)
    _result, power = await rig.install()
    assert power == firmware.POWER_UNCHANGED
    assert rig.calls == ["stop", "session", "start"]


async def test_repower_timeout_is_not_an_install_failure(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    fast_timeouts: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = await _rig(hass, aioclient_mock, power_on=True)
    rig.reports_standby_after_update = False  # standby never reported

    result, power = await rig.install()

    assert result is PROMOTED
    assert power == firmware.POWER_RESTORE_FAILED
    assert "could not be powered back on: timed out" in caplog.text
    assert "power_on" not in rig.calls
    await firmware.async_notify_install_complete(hass, rig.coordinator, "030326", result, power)
    (message,) = notification_messages(hass)
    assert "power state could not be confirmed" in message


async def test_repower_when_unit_never_comes_back_on(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, fast_timeouts: None
) -> None:
    rig = await _rig(hass, aioclient_mock, power_on=True)
    rig.answers_power_on = False

    _result, power = await rig.install()

    assert power == firmware.POWER_RESTORE_FAILED
    assert rig.calls[-1] == "power_on"


async def test_unconfirmed_power_down_cannot_claim_standby_was_restored(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, fast_timeouts: None
) -> None:
    rig = await _rig(hass, aioclient_mock, power_on=False)
    rig.reports_standby_after_update = False
    result, power = await rig.install()
    assert result is PROMOTED
    assert power == firmware.POWER_RESTORE_FAILED
    await firmware.async_notify_install_complete(hass, rig.coordinator, "030326", result, power)
    (message,) = notification_messages(hass)
    assert "power state could not be confirmed" in message


# ---------------------------------------------------------------------------
# Before the device is touched
# ---------------------------------------------------------------------------


async def test_power_on_timeout_aborts_before_firmware(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, fast_timeouts: None
) -> None:
    rig = await _rig(hass, aioclient_mock, power_on=False)
    rig.answers_power_on = False

    with pytest.raises(HomeAssistantError) as err:
        await rig.install()

    assert err.value.translation_key == "firmware_power_on_timeout"
    rig.session_factory.assert_not_called()
    assert "stop" not in rig.calls
    # Best-effort return to where the user left it.
    assert rig.calls == ["power_on", "standby"]
    rig.settle.assert_not_awaited()
    assert rig.coordinator.firmware_update_active is False


async def test_release_mismatch_rejected_before_device(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, fast_timeouts: None
) -> None:
    rig = await _rig(hass, aioclient_mock, power_on=False, bundle_release="120325")

    with pytest.raises(HomeAssistantError) as err:
        await rig.install()

    assert err.value.translation_key == "firmware_image_invalid"
    assert "release mismatch" in str(err.value.__cause__)
    assert rig.calls == []


async def test_zip_entry_name_mismatch_rejected(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, fast_timeouts: None
) -> None:
    rig = await _rig(
        hass, aioclient_mock, power_on=True, zip_bytes=updater_zip("radiance_pro120325.exe")
    )
    with pytest.raises(HomeAssistantError) as err:
        await rig.install()
    assert err.value.translation_key == "firmware_image_invalid"
    assert rig.calls == []


async def test_unusable_zip_rejected(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, fast_timeouts: None
) -> None:
    rig = await _rig(hass, aioclient_mock, power_on=True, zip_bytes=b"not a zip")
    with pytest.raises(HomeAssistantError) as err:
        await rig.install()
    assert err.value.translation_key == "firmware_image_invalid"
    assert rig.calls == []


async def test_download_404(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, fast_timeouts: None
) -> None:
    rig = await _rig(hass, aioclient_mock, power_on=False, zip_status=404)
    with pytest.raises(HomeAssistantError) as err:
        await rig.install()
    assert err.value.translation_key == "firmware_download_failed"
    assert rig.calls == []


async def test_download_oversize(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, fast_timeouts: None
) -> None:
    rig = await _rig(hass, aioclient_mock, power_on=True)
    with (
        patch.object(firmware, "MAX_UPDATER_ZIP_BYTES", 16),
        pytest.raises(HomeAssistantError) as err,
    ):
        await rig.install()
    assert err.value.translation_key == "firmware_download_failed"
    assert rig.calls == []


async def test_lock_held_refuses(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, fast_timeouts: None
) -> None:
    rig = await _rig(hass, aioclient_mock, power_on=True)
    async with rig.coordinator.firmware_lock:
        with pytest.raises(HomeAssistantError) as err:
            await rig.install()
    assert err.value.translation_key == "firmware_update_in_progress"
    assert rig.calls == []


async def test_power_unknown_refuses(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, fast_timeouts: None
) -> None:
    rig = await _rig(hass, aioclient_mock, power_on=True)
    rig.client.state = dataclasses.replace(rig.client.state, power_on=None)
    with pytest.raises(HomeAssistantError) as err:
        await rig.install()
    assert err.value.translation_key == "firmware_power_unknown"
    assert rig.calls == []


# ---------------------------------------------------------------------------
# Session failures: mapping, restart, and power handling
# ---------------------------------------------------------------------------


async def test_abort_error_restores_standby(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, fast_timeouts: None
) -> None:
    rig = await _rig(
        hass, aioclient_mock, power_on=False, run_exc=LumagenFirmwareAbortError("refused")
    )
    with pytest.raises(HomeAssistantError) as err:
        await rig.install()
    assert err.value.translation_key == "firmware_aborted"
    assert rig.calls == ["power_on", "stop", "session", "start", "standby"]
    assert rig.coordinator.firmware_update_active is False
    assert not rig.coordinator.firmware_lock.locked()


async def test_unconfirmable_error_leaves_power_and_notifies(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, fast_timeouts: None
) -> None:
    rig = await _rig(
        hass,
        aioclient_mock,
        power_on=False,
        run_exc=LumagenFirmwareError("verify failed; retry before power-cycling"),
    )
    with pytest.raises(HomeAssistantError) as err:
        await rig.install()
    assert err.value.translation_key == "firmware_failed"
    assert err.value.translation_placeholders == {
        "error": "verify failed; retry before power-cycling"
    }
    # Auto-powered, but live firmware may have changed: don't power-cycle it.
    assert "standby" not in rig.calls
    assert rig.calls[-1] == "start"
    (message,) = notification_messages(hass)
    assert "verify failed; retry before power-cycling" in message


async def test_connection_error_on_enter(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, fast_timeouts: None
) -> None:
    rig = await _rig(
        hass, aioclient_mock, power_on=False, enter_exc=LumagenConnectionError("proxy busy")
    )
    with pytest.raises(HomeAssistantError) as err:
        await rig.install()
    assert err.value.translation_key == "firmware_connection_failed"
    # Never entered, so firmware is provably untouched: back to standby.
    assert rig.calls == ["power_on", "stop", "session", "start", "standby"]
    rig.session.run_update.assert_not_awaited()


async def test_unexpected_error_propagates_after_cleanup(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, fast_timeouts: None
) -> None:
    rig = await _rig(hass, aioclient_mock, power_on=True, run_exc=RuntimeError("boom"))
    with pytest.raises(RuntimeError, match="boom"):
        await rig.install()
    assert rig.calls == ["stop", "session", "start"]
    assert rig.coordinator.firmware_update_active is False


async def test_cancellation_resumes_client_without_power_actions(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, fast_timeouts: None
) -> None:
    rig = await _rig(hass, aioclient_mock, power_on=False, run_exc=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await rig.install()
    # HA shutting down: hand the link back, touch nothing else.
    assert rig.calls == ["power_on", "stop", "session", "start"]
    assert rig.coordinator.firmware_update_active is False


async def test_resume_failure_schedules_reload(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, fast_timeouts: None
) -> None:
    rig = await _rig(hass, aioclient_mock, power_on=True)

    async def _start_fails() -> None:
        rig.calls.append("start")
        raise LumagenConnectionError("unit powering down")

    rig.client.start.side_effect = _start_fails
    reload = _use(patch.object(hass.config_entries, "async_schedule_reload"))

    _result, power = await rig.install()

    reload.assert_called_once_with(rig.coordinator.config_entry.entry_id)
    assert rig.calls == ["stop", "session", "start"]  # no power actions after it
    assert power == firmware.POWER_RESTORE_FAILED
    assert rig.coordinator.firmware_update_active is False


async def test_pending_options_reload_runs_after_install(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, fast_timeouts: None
) -> None:
    rig = await _rig(hass, aioclient_mock, power_on=True, result=NOTHING_WRITTEN)
    reload = _use(patch.object(hass.config_entries, "async_schedule_reload"))
    rig.coordinator.reload_pending = True

    await rig.install()

    reload.assert_called_once_with(rig.coordinator.config_entry.entry_id)
    assert rig.coordinator.reload_pending is False


# ---------------------------------------------------------------------------
# Follow-up review fixes
# ---------------------------------------------------------------------------


async def _shutdown_stuck_entry(rig: Rig) -> None:
    """A refused unload leaves the entry FAILED_UNLOAD, which HA's test teardown
    can't unload; stop its coordinators' timers so none linger."""
    await rig.coordinator.async_shutdown()
    assert rig.coordinator.release_coordinator is not None
    await rig.coordinator.release_coordinator.async_shutdown()


async def test_unload_refused_mid_install_and_client_left_stopped(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    fast_timeouts: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = await _rig(hass, aioclient_mock, power_on=True)
    entry = rig.coordinator.config_entry
    unloaded: list[bool] = []

    async def _unload() -> None:
        unloaded.append(await hass.config_entries.async_unload(entry.entry_id))

    rig.during_session = _unload
    reload = _use(patch.object(hass.config_entries, "async_schedule_reload"))

    result, power = await rig.install()

    assert unloaded == [False]
    assert entry.state is ConfigEntryState.FAILED_UNLOAD
    assert "firmware update is in progress" in caplog.text
    # The orphaned client is not restarted and no reload races the proxy.
    assert rig.calls == ["stop", "session"]
    reload.assert_not_called()
    assert result is PROMOTED
    # It was on and Z97 powered it down; nothing can repower it now.
    assert power == firmware.POWER_RESTORE_FAILED
    assert "leaving its client stopped" in caplog.text
    assert rig.coordinator.firmware_update_active is False
    assert not rig.coordinator.firmware_lock.locked()
    await _shutdown_stuck_entry(rig)


async def test_unload_refused_while_downloading(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, fast_timeouts: None
) -> None:
    rig = await _rig(hass, aioclient_mock, power_on=True, register_zip=False)
    entry = rig.coordinator.config_entry
    unloaded: list[bool] = []

    async def _download(method: str, url: URL, data: Any) -> AiohttpClientMockResponse:
        unloaded.append(await hass.config_entries.async_unload(entry.entry_id))
        return AiohttpClientMockResponse(method, url, response=updater_zip())

    aioclient_mock.get(rig.listing.url, side_effect=_download)
    reload = _use(patch.object(hass.config_entries, "async_schedule_reload"))

    with pytest.raises(HomeAssistantError) as err:
        await rig.install()

    assert unloaded == [False]
    assert entry.state is ConfigEntryState.FAILED_UNLOAD
    assert err.value.translation_key == "firmware_aborted"
    # Aborted before any device action: no pause, no session, no power.
    assert rig.calls == []
    rig.session_factory.assert_not_called()
    assert rig.active_during_session == []
    reload.assert_not_called()
    assert rig.coordinator.firmware_update_active is False
    assert not rig.coordinator.firmware_lock.locked()
    await _shutdown_stuck_entry(rig)


async def test_entry_removed_while_downloading_aborts(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, fast_timeouts: None
) -> None:
    rig = await _rig(hass, aioclient_mock, power_on=False, register_zip=False)
    entry_id = rig.coordinator.config_entry.entry_id

    async def _download(method: str, url: URL, data: Any) -> AiohttpClientMockResponse:
        await hass.config_entries.async_remove(entry_id)
        return AiohttpClientMockResponse(method, url, response=updater_zip())

    aioclient_mock.get(rig.listing.url, side_effect=_download)

    with pytest.raises(HomeAssistantError) as err:
        await rig.install()

    assert err.value.translation_key == "firmware_aborted"
    # Not even the auto power-on for the standby unit.
    assert rig.calls == []
    rig.session_factory.assert_not_called()
    assert not rig.coordinator.firmware_lock.locked()


async def test_entry_removed_mid_install_leaves_client_stopped(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, fast_timeouts: None
) -> None:
    rig = await _rig(hass, aioclient_mock, power_on=False, result=NOTHING_WRITTEN)
    entry_id = rig.coordinator.config_entry.entry_id

    async def _remove() -> None:
        await hass.config_entries.async_remove(entry_id)

    rig.during_session = _remove
    reload = _use(patch.object(hass.config_entries, "async_schedule_reload"))

    _result, power = await rig.install()

    assert hass.config_entries.async_get_entry(entry_id) is None
    assert rig.calls == ["power_on", "stop", "session"]
    assert power == firmware.POWER_UNCHANGED
    reload.assert_not_called()


async def test_unload_allowed_when_no_install_runs(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    rig = await _rig(hass, aioclient_mock, power_on=True)
    assert await hass.config_entries.async_unload(rig.coordinator.config_entry.entry_id)
    assert rig.coordinator.config_entry.state is ConfigEntryState.NOT_LOADED


async def test_power_state_sampled_after_download(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, fast_timeouts: None
) -> None:
    """The user switches the unit on while the zip downloads: restore *on*."""
    rig = await _rig(hass, aioclient_mock, power_on=False, register_zip=False)

    async def _download(method: str, url: URL, data: Any) -> AiohttpClientMockResponse:
        rig.set_power(True)
        return AiohttpClientMockResponse(method, url, response=updater_zip())

    aioclient_mock.get(rig.listing.url, side_effect=_download)

    _result, power = await rig.install()

    # No auto power-on (it's already on), and it's powered back on afterwards.
    assert rig.calls == ["stop", "session", "start", "power_on"]
    assert power == firmware.POWER_RESTORED
    rig.settle.assert_not_awaited()


async def test_log_result_failure_does_not_skip_cleanup(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    fast_timeouts: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = await _rig(hass, aioclient_mock, power_on=True, result=NOTHING_WRITTEN)
    _use(patch.object(firmware, "_log_result", MagicMock(side_effect=RuntimeError("log boom"))))

    result, power = await rig.install()

    assert result is NOTHING_WRITTEN
    assert power == firmware.POWER_UNCHANGED
    assert rig.calls == ["stop", "session", "start"]
    assert rig.coordinator.firmware_update_active is False
    assert not rig.coordinator.firmware_lock.locked()
    assert "Could not log the Lumagen firmware session result" in caplog.text


async def test_failure_notification_error_does_not_mask_mapped_error(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    fast_timeouts: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = await _rig(
        hass, aioclient_mock, power_on=True, run_exc=LumagenFirmwareError("verify failed")
    )
    _use(patch.object(firmware, "async_notify", AsyncMock(side_effect=RuntimeError("i18n"))))

    with pytest.raises(HomeAssistantError) as err:
        await rig.install()

    assert err.value.translation_key == "firmware_failed"
    assert isinstance(err.value.__cause__, LumagenFirmwareError)
    assert rig.calls == ["stop", "session", "start"]
    assert rig.coordinator.firmware_update_active is False
    assert "Could not post the firmware-failure notification" in caplog.text


async def test_cancel_during_settle_returns_auto_powered_unit_to_standby(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, fast_timeouts: None
) -> None:
    rig = await _rig(hass, aioclient_mock, power_on=False)
    settling = asyncio.Event()

    async def _settle(_seconds: float) -> None:
        settling.set()
        await asyncio.Event().wait()

    rig.settle.side_effect = _settle
    task = hass.async_create_task(rig.install())
    await settling.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert rig.calls == ["power_on", "standby"]
    rig.session_factory.assert_not_called()
    assert rig.coordinator.firmware_update_active is False
    assert not rig.coordinator.firmware_lock.locked()


async def test_cancel_during_power_on_wait_returns_to_standby(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, fast_timeouts: None
) -> None:
    rig = await _rig(hass, aioclient_mock, power_on=False)
    rig.answers_power_on = False  # so the wait for "on" is in progress
    waiting = asyncio.Event()

    async def _query_power() -> None:
        waiting.set()

    rig.client.query_power.side_effect = _query_power
    _use(patch.object(firmware, "FIRMWARE_POWER_ON_TIMEOUT", 30.0))
    task = hass.async_create_task(rig.install())
    await waiting.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert rig.calls == ["power_on", "standby"]
    rig.settle.assert_not_awaited()


async def test_cancel_standby_is_bounded_and_does_not_swallow_cancel(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    fast_timeouts: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = await _rig(hass, aioclient_mock, power_on=False)
    settling = asyncio.Event()

    async def _settle(_seconds: float) -> None:
        settling.set()
        await asyncio.Event().wait()

    async def _standby_hangs() -> None:
        rig.calls.append("standby")
        await asyncio.Event().wait()

    rig.settle.side_effect = _settle
    rig.client.standby.side_effect = _standby_hangs
    _use(patch.object(firmware, "FIRMWARE_CANCEL_STANDBY_TIMEOUT", 0.05))
    task = hass.async_create_task(rig.install())
    await settling.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert rig.calls == ["power_on", "standby"]
    # The bound expired: the reason must say so rather than being blank.
    assert "could not return the Lumagen to standby: timed out" in caplog.text


async def test_firmware_gate_covers_power_on_and_settle(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, fast_timeouts: None
) -> None:
    rig = await _rig(hass, aioclient_mock, power_on=False, result=NOTHING_WRITTEN)
    observed: list[bool] = []

    async def settle(_seconds: float) -> None:
        observed.append(rig.coordinator.firmware_update_active)
        with pytest.raises(HomeAssistantError) as err:
            await hass.services.async_call(
                DOMAIN, "send_raw_command", {"command": "$"}, blocking=True
            )
        assert err.value.translation_key == "firmware_update_active"

    rig.settle.side_effect = settle
    await rig.install()
    assert observed == [True]
    assert rig.coordinator.firmware_update_active is False


@pytest.mark.parametrize("failure_stage", ["download", "power"])
async def test_deferred_options_reload_survives_pre_session_failure(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    fast_timeouts: None,
    failure_stage: str,
) -> None:
    rig = await _rig(
        hass,
        aioclient_mock,
        power_on=False,
        zip_status=503 if failure_stage == "download" else 200,
    )
    rig.answers_power_on = False
    rig.coordinator.reload_pending = True
    reload = _use(patch.object(hass.config_entries, "async_schedule_reload"))
    with pytest.raises(HomeAssistantError):
        await rig.install()
    reload.assert_called_once_with(rig.coordinator.config_entry.entry_id)
    assert not rig.coordinator.reload_pending
    assert not rig.coordinator.firmware_update_active
    assert not rig.coordinator.firmware_lock.locked()
    rig.session_factory.assert_not_called()


async def test_deferred_options_reload_survives_pre_session_cancellation(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, fast_timeouts: None
) -> None:
    rig = await _rig(hass, aioclient_mock, power_on=False)
    reload = _use(patch.object(hass.config_entries, "async_schedule_reload"))

    async def settle(_seconds: float) -> None:
        rig.coordinator.reload_pending = True
        raise asyncio.CancelledError

    rig.settle.side_effect = settle
    with pytest.raises(asyncio.CancelledError):
        await rig.install()
    reload.assert_called_once_with(rig.coordinator.config_entry.entry_id)
    assert not rig.coordinator.firmware_update_active
    assert not rig.coordinator.firmware_lock.locked()


async def test_unload_during_power_settle_cannot_start_a_firmware_session(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, fast_timeouts: None
) -> None:
    rig = await _rig(hass, aioclient_mock, power_on=False)

    async def settle(_seconds: float) -> None:
        assert not await hass.config_entries.async_unload(rig.coordinator.config_entry.entry_id)

    rig.settle.side_effect = settle
    with pytest.raises(HomeAssistantError) as err:
        await rig.install()
    assert err.value.translation_key == "firmware_aborted"
    rig.session_factory.assert_not_called()
    assert "stop" in rig.calls
    assert "start" not in rig.calls
    await _shutdown_stuck_entry(rig)


async def test_rejected_second_installer_cannot_lift_first_installers_gate(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, fast_timeouts: None
) -> None:
    rig = await _rig(hass, aioclient_mock, power_on=True, result=NOTHING_WRITTEN)

    async def concurrent_install() -> None:
        with pytest.raises(HomeAssistantError) as err:
            await rig.install()
        assert err.value.translation_key == "firmware_update_in_progress"
        assert rig.coordinator.firmware_update_active
        assert rig.coordinator.firmware_lock.locked()

    rig.during_session = concurrent_install
    await rig.install()
    assert rig.session.run_update.await_count == 1
    assert not rig.coordinator.firmware_update_active


@pytest.mark.parametrize("reason", ["options", "resume_failure"])
async def test_real_deferred_reload_waits_for_install_service_cleanup(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    fast_timeouts: None,
    reason: str,
) -> None:
    rig = await _rig(hass, aioclient_mock, power_on=True, result=NOTHING_WRITTEN)
    entry = rig.coordinator.config_entry

    async def change_options() -> None:
        hass.config_entries.async_update_entry(entry, options={"poll_interval": 30})
        async with asyncio.timeout(1):
            while not rig.coordinator.reload_pending:
                await asyncio.sleep(0)

    if reason == "options":
        rig.during_session = change_options
    else:
        rig.client.start.side_effect = [LumagenConnectionError("rebooting"), None]
    update_id = next(state.entity_id for state in hass.states.async_all("update"))
    factory = _use(
        patch(
            "custom_components.lumagen.coordinator.create_lumagen_client",
            AsyncMock(return_value=rig.client),
        )
    )
    await hass.services.async_call("update", "install", {"entity_id": update_id}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is not rig.coordinator
    assert factory.await_count == 1
    state = hass.states.get(update_id)
    assert state is not None
    assert state.attributes["in_progress"] is False
    assert state.attributes["update_percentage"] is None


# --- Download redirect policy ----------------------------------------------

CDN_URL = "https://static1.squarespace.com/static/synthetic/t/abc/radiance_pro030326.zip"


def _redirect(aioclient_mock: AiohttpClientMocker, url: str, location: str) -> None:
    aioclient_mock.get(url, status=302, headers={"Location": location})


def _requested(aioclient_mock: AiohttpClientMocker) -> list[str]:
    return [str(call[1]) for call in aioclient_mock.mock_calls]


async def test_download_follows_redirect_to_allowed_cdn(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, fast_timeouts: None
) -> None:
    rig = await _rig(hass, aioclient_mock, power_on=True, register_zip=False)
    _redirect(aioclient_mock, rig.listing.url, CDN_URL)
    aioclient_mock.get(CDN_URL, content=updater_zip())

    result, _power = await rig.install()

    assert result is PROMOTED
    assert _requested(aioclient_mock)[-2:] == [rig.listing.url, CDN_URL]


@pytest.mark.parametrize(
    "location",
    [
        pytest.param("https://evil.example.com/radiance_pro030326.zip", id="foreign-host"),
        pytest.param("http://static1.squarespace.com/radiance_pro030326.zip", id="plain-http"),
        pytest.param(
            "https://someone.squarespace.com/radiance_pro030326.zip", id="other-subdomain"
        ),
    ],
)
async def test_download_redirect_to_disallowed_target_rejected(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, fast_timeouts: None, location: str
) -> None:
    rig = await _rig(hass, aioclient_mock, power_on=False, register_zip=False)
    _redirect(aioclient_mock, rig.listing.url, location)
    aioclient_mock.get(location, content=updater_zip())

    with pytest.raises(HomeAssistantError) as err:
        await rig.install()

    assert err.value.translation_key == "firmware_download_failed"
    assert location not in _requested(aioclient_mock)
    assert rig.calls == []


async def test_download_redirect_loop_is_bounded(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, fast_timeouts: None
) -> None:
    rig = await _rig(hass, aioclient_mock, power_on=True, register_zip=False)
    _redirect(aioclient_mock, rig.listing.url, rig.listing.url)

    with pytest.raises(HomeAssistantError) as err:
        await rig.install()

    assert err.value.translation_key == "firmware_download_failed"
    assert "redirects" in str(err.value.translation_placeholders)
    hops = [u for u in _requested(aioclient_mock) if u == rig.listing.url]
    assert len(hops) == firmware.FIRMWARE_DOWNLOAD_MAX_REDIRECTS + 1
    assert rig.calls == []


async def test_download_redirect_without_location_rejected(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, fast_timeouts: None
) -> None:
    rig = await _rig(hass, aioclient_mock, power_on=True, register_zip=False)
    aioclient_mock.get(rig.listing.url, status=302)
    with pytest.raises(HomeAssistantError) as err:
        await rig.install()
    assert err.value.translation_key == "firmware_download_failed"
    assert rig.calls == []


async def test_download_size_cap_applies_after_redirect(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, fast_timeouts: None
) -> None:
    rig = await _rig(hass, aioclient_mock, power_on=True, register_zip=False)
    _redirect(aioclient_mock, rig.listing.url, CDN_URL)
    aioclient_mock.get(CDN_URL, content=updater_zip())
    with (
        patch.object(firmware, "MAX_UPDATER_ZIP_BYTES", 16),
        pytest.raises(HomeAssistantError) as err,
    ):
        await rig.install()
    assert err.value.translation_key == "firmware_download_failed"
    assert rig.calls == []


async def test_listed_url_on_disallowed_host_rejected(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, fast_timeouts: None
) -> None:
    rig = await _rig(hass, aioclient_mock, power_on=True, register_zip=False)
    rig.listing = dataclasses.replace(rig.listing, url="https://evil.example.com/x.zip")
    aioclient_mock.get(rig.listing.url, content=updater_zip())
    with pytest.raises(HomeAssistantError) as err:
        await rig.install()
    assert err.value.translation_key == "firmware_download_failed"
    assert rig.listing.url not in _requested(aioclient_mock)
    assert rig.calls == []

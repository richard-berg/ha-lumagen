"""Firmware-update orchestration: download, power handling, session, restore.

Every Lumagen-shaped operation — unpacking the zip, parsing the updater,
deciding what to write, the transfer itself — is a call into
:mod:`aiolumagen.firmware`. This module owns only the Home Assistant half:
HTTP, the order things happen in, the client pause/resume around the
session, power handling, and how outcomes are presented.

The sequence, and why it's in this order:

1. Download and unpack *before* touching the device, so a bad download or a
   mismatched archive costs nothing.
2. If the unit is in standby, power it on and let it settle — the updater
   refuses standby, and a unit that has only just reported "on" may still be
   coming up.
3. Gate the other entities, stop the client (``serial_proxy`` serves one
   subscriber), run the session on the entry's own URL, verbatim.
4. Restart the client, restore power to how the user left it, lift the gate.
   A reload, when needed, is scheduled last so it can't tear down the update
   entity while this is still running.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import aiohttp
from aiohttp import hdrs
from aiolumagen import (
    LumagenConnectionError,
    LumagenError,
    LumagenFirmwareAbortError,
    LumagenFirmwareError,
    LumagenFirmwareImageError,
)
from aiolumagen.firmware import (
    SESSION_BAUD,
    FirmwareBundle,
    FirmwareRevision,
    FirmwareSession,
    ProgressCallback,
    ReleaseListing,
    UpdateResult,
    extract_images,
    extract_updater_zip,
)
from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.translation import async_get_translations
from yarl import URL

from .const import (
    CONF_URL,
    DOMAIN,
    FIRMWARE_CANCEL_STANDBY_TIMEOUT,
    FIRMWARE_DOWNLOAD_HOSTS,
    FIRMWARE_DOWNLOAD_MAX_REDIRECTS,
    FIRMWARE_POST_UPDATE_STANDBY_TIMEOUT,
    FIRMWARE_POWER_ON_SETTLE,
    FIRMWARE_POWER_ON_TIMEOUT,
    FIRMWARE_UPDATE_BAUDRATE,
    FIRMWARE_USER_AGENT,
    MAX_UPDATER_ZIP_BYTES,
    UPDATER_DOWNLOAD_TIMEOUT,
)
from .coordinator import LumagenCoordinator
from .release_coordinator import async_read_capped

_LOGGER = logging.getLogger(__name__)

_REVISION_IN_NAME = re.compile(r"(\d{6})")

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

# How the unit's power was left, as a translation key for the notification.
POWER_RESTORED = "firmware_power_restored"
POWER_LEFT_OFF = "firmware_power_left_off"
POWER_STANDBY = "firmware_power_standby"
POWER_UNCHANGED = "firmware_power_unchanged"
POWER_RESTORE_FAILED = "firmware_power_restore_failed"


def _error(key: str, err: BaseException | None = None) -> HomeAssistantError:
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders={"error": str(err) if err is not None else ""},
    )


def notification_id(coordinator: LumagenCoordinator) -> str:
    return f"{DOMAIN}_{coordinator.config_entry.entry_id}_firmware"


async def async_notify(
    hass: HomeAssistant,
    coordinator: LumagenCoordinator,
    key: str,
    placeholders: dict[str, Any] | None = None,
) -> None:
    """Post a persistent notification whose text lives in strings.json.

    Messages are stored under ``exceptions`` so the same translation machinery
    serves both raised errors and notifications. A missing translation falls
    back to the key rather than failing — a notification is never worth an
    exception on the cleanup path.
    """
    translations = await async_get_translations(hass, hass.config.language, "exceptions", [DOMAIN])

    def _render(name: str) -> str:
        template = translations.get(f"component.{DOMAIN}.exceptions.{name}.message", name)
        try:
            return template.format(**(placeholders or {}))
        except KeyError, IndexError, ValueError:
            return template

    persistent_notification.async_create(
        hass,
        _render(key),
        title=_render("firmware_notification_title"),
        notification_id=notification_id(coordinator),
    )


async def async_power_text(hass: HomeAssistant, key: str) -> str:
    """Render one of the POWER_* sentences for use as a placeholder."""
    translations = await async_get_translations(hass, hass.config.language, "exceptions", [DOMAIN])
    return translations.get(f"component.{DOMAIN}.exceptions.{key}.message", key)


def _check_download_url(url: URL) -> None:
    """Refuse anything but https on one of FIRMWARE_DOWNLOAD_HOSTS."""
    host = (url.host or "").lower()
    if url.scheme != "https" or host not in FIRMWARE_DOWNLOAD_HOSTS:
        _LOGGER.warning("Refusing to download Lumagen firmware from %s", url)
        raise _error(
            "firmware_download_failed",
            ValueError(f"refusing to download from {url}: not https on an allowed host"),
        )


async def _async_download(hass: HomeAssistant, url_text: str) -> bytes:
    """Fetch the updater zip, following redirects only to allowed hosts.

    Redirects are followed by hand rather than by aiohttp, so every hop's
    target is checked *before* it's requested, the hop count is bounded, and
    the size cap applies to the bytes of whichever response finally carries
    the body. One deadline covers the whole chain.

    :raises HomeAssistantError: ``firmware_download_failed``.
    """
    session = async_get_clientsession(hass)
    url = URL(url_text)
    try:
        async with asyncio.timeout(UPDATER_DOWNLOAD_TIMEOUT):
            for _hop in range(FIRMWARE_DOWNLOAD_MAX_REDIRECTS + 1):
                _check_download_url(url)
                async with session.get(
                    url,
                    headers={"User-Agent": FIRMWARE_USER_AGENT},
                    allow_redirects=False,
                ) as resp:
                    if resp.status in _REDIRECT_STATUSES:
                        location = resp.headers.get(hdrs.LOCATION)
                        if not location:
                            raise _error(
                                "firmware_download_failed",
                                ValueError(f"redirect from {url} has no Location"),
                            )
                        url = url.join(URL(location))
                        continue
                    resp.raise_for_status()
                    return await async_read_capped(resp, MAX_UPDATER_ZIP_BYTES)
    except (aiohttp.ClientError, TimeoutError, ValueError) as err:
        # ValueError: a Location yarl can't parse.
        raise _error("firmware_download_failed", err) from err
    raise _error(
        "firmware_download_failed",
        ValueError(f"more than {FIRMWARE_DOWNLOAD_MAX_REDIRECTS} redirects"),
    )


async def async_fetch_bundle(hass: HomeAssistant, listing: ReleaseListing) -> FirmwareBundle:
    """Download ``listing``'s zip, unpack it, and cross-check the release.

    In memory throughout: the zip is ~3 MB and nothing needs to touch disk.
    Runs entirely before any device action.

    :raises HomeAssistantError: the download failed or was oversize.
    :raises LumagenFirmwareImageError: the archive or updater is unusable, or
        the listing, the zip entry and the extracted bundle disagree about
        which release this is.
    """
    data = await _async_download(hass, listing.url)
    if len(data) > MAX_UPDATER_ZIP_BYTES:
        raise _error(
            "firmware_download_failed",
            ValueError(f"download exceeds {MAX_UPDATER_ZIP_BYTES} bytes"),
        )

    def _unpack() -> tuple[str, FirmwareBundle]:
        # Not load_updater(bytes): that drops the filename, and the filename
        # is the only place bundle.release comes from.
        name, exe = extract_updater_zip(data)
        return name, extract_images(exe, source_name=name)

    name, bundle = await hass.async_add_executor_job(_unpack)

    # Three independent statements of which release this is must agree.
    match = _REVISION_IN_NAME.search(name)
    from_entry = FirmwareRevision.parse(match.group(1)) if match else None
    if (
        from_entry is None
        or bundle.release is None
        or not (listing.revision == from_entry == bundle.release)
    ):
        raise LumagenFirmwareImageError(
            f"release mismatch: listing {listing.revision.mmddyy}, zip entry {name!r}, "
            f"bundle {bundle.release.mmddyy if bundle.release else None}"
        )
    return bundle


async def _async_auto_power_on(coordinator: LumagenCoordinator) -> None:
    """Bring a standby unit up and let it settle before any firmware command."""
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        try:
            await coordinator.client.power_on()
            await coordinator.async_wait_for_power(True, FIRMWARE_POWER_ON_TIMEOUT)
        except (TimeoutError, LumagenError) as err:
            # It may yet come on; put it back where the user left it.
            try:
                await coordinator.client.standby()
            except LumagenError as standby_err:
                _LOGGER.debug("Standby after a failed power-on also failed: %s", standby_err)
            raise _error("firmware_power_on_timeout", err) from err
        _LOGGER.info(
            "Lumagen reported power-on %.1fs after the request; settling %.0fs before updating",
            loop.time() - started,
            FIRMWARE_POWER_ON_SETTLE,
        )
        await _async_settle(FIRMWARE_POWER_ON_SETTLE)
    except asyncio.CancelledError:
        # Cancelled (HA stopping) after we switched the unit on but before the
        # session started, so firmware is untouched: put it back in standby.
        await _async_standby_after_cancel(coordinator)
        raise


async def _async_standby_after_cancel(coordinator: LumagenCoordinator) -> None:
    """Best-effort, bounded standby while a cancellation is propagating.

    Swallows everything except a further CancelledError, which the caller's
    re-raise would propagate anyway — cancellation is never suppressed here.
    """
    try:
        async with asyncio.timeout(FIRMWARE_CANCEL_STANDBY_TIMEOUT):
            await coordinator.client.standby()
    except Exception as err:  # cleanup must not replace the cancellation
        _LOGGER.warning(
            "Firmware install cancelled; could not return the Lumagen to standby: %s",
            str(err) or "timed out",
        )
    else:
        _LOGGER.info("Firmware install cancelled; returned the Lumagen to standby")


async def _async_settle(seconds: float) -> None:
    """Separate so tests can observe the settle without patching asyncio."""
    await asyncio.sleep(seconds)


def _log_result(result: UpdateResult) -> None:
    plan = result.plan
    _LOGGER.info(
        "Lumagen firmware session finished: written=%s promoted=%s powered_down=%s "
        "writes_section1=%s flush_mode=%s flush_calls=%d flush_retries=%d",
        ",".join(result.written) or "nothing",
        result.promoted,
        result.powered_down,
        plan.writes_section1,
        result.flush_mode,
        result.flush_calls,
        result.flush_retries,
    )
    _LOGGER.info("Lumagen firmware plan:\n%s", plan.describe())
    for note in result.notes:
        _LOGGER.info("Lumagen firmware note: %s", note)


async def _async_restore_power(
    coordinator: LumagenCoordinator,
    *,
    result: UpdateResult | None,
    failure: BaseException | None,
    touched_device: bool,
    was_on: bool,
    auto_powered: bool,
) -> str:
    """Return the unit to the power state the user left it in.

    Never raises: this runs on the cleanup path and must not mask the
    session's outcome. Returns one of the POWER_* keys.
    """
    client = coordinator.client
    try:
        if result is not None:
            if result.powered_down and was_on:
                # The snapshot still says "on" from before the session, so wait
                # for a fresh standby report before asking for power-on —
                # otherwise the request could land mid power-down and be lost.
                try:
                    await coordinator.async_wait_for_power(
                        False, FIRMWARE_POST_UPDATE_STANDBY_TIMEOUT
                    )
                    await client.power_on()
                    await coordinator.async_wait_for_power(True, FIRMWARE_POWER_ON_TIMEOUT)
                except (TimeoutError, LumagenError) as err:
                    _LOGGER.warning(
                        "Firmware updated, but the Lumagen could not be powered back on: %s",
                        str(err) or "timed out",
                    )
                    return POWER_RESTORE_FAILED
                return POWER_RESTORED
            if result.powered_down:
                await coordinator.async_wait_for_power(False, FIRMWARE_POST_UPDATE_STANDBY_TIMEOUT)
                return POWER_LEFT_OFF
            if auto_powered:
                # Nothing needed a reboot (e.g. the plan found nothing to
                # write), but we turned the unit on, so turn it back off.
                await client.standby()
                await coordinator.async_wait_for_power(False, FIRMWARE_POST_UPDATE_STANDBY_TIMEOUT)
                return POWER_STANDBY
            return POWER_UNCHANGED

        # Failure. Only return an auto-powered unit to standby when live
        # firmware is provably unchanged. After anything else a standby/on
        # cycle is a reboot, and the library's advice after a partial update is
        # to retry before power-cycling.
        provably_unchanged = not touched_device or isinstance(
            failure, (LumagenFirmwareAbortError, LumagenFirmwareImageError)
        )
        if auto_powered and provably_unchanged:
            await client.standby()
            await coordinator.async_wait_for_power(False, FIRMWARE_POST_UPDATE_STANDBY_TIMEOUT)
            return POWER_STANDBY
    except (TimeoutError, LumagenError) as err:
        _LOGGER.warning("Could not restore the Lumagen's power state: %s", str(err) or "timed out")
        return POWER_RESTORE_FAILED
    return POWER_UNCHANGED


async def _async_finish(
    hass: HomeAssistant,
    coordinator: LumagenCoordinator,
    *,
    result: UpdateResult | None,
    failure: BaseException | None,
    cancelled: bool,
    touched_device: bool,
    was_on: bool,
    auto_powered: bool,
) -> str:
    """Resume the client, restore power, lift the gate, reload if needed.

    The gate stays up through the power restore so the other entities don't
    flicker through the reboot. Never raises.

    If the entry stopped being LOADED during the install — an unload, reload,
    disable or delete that ``async_unload_entry`` refused, or one that got past
    it — the client is left stopped and no reload is scheduled. Restarting it
    would reopen the serial link for an entry the user asked to go away, and a
    reload can't recover a refused unload anyway.
    """
    power = POWER_UNCHANGED
    resumed = False
    entry = coordinator.config_entry
    entry_loaded = (
        hass.config_entries.async_get_entry(entry.entry_id) is entry
        and entry.state is ConfigEntryState.LOADED
    )
    try:
        if entry_loaded:
            resumed = await coordinator.async_resume_client()
        else:
            _LOGGER.warning(
                "The Lumagen config entry %r was unloaded, disabled or removed during the "
                "firmware update (state: %s); leaving its client stopped. Restart Home "
                "Assistant to bring the integration back",
                entry.title,
                entry.state,
            )
        if cancelled:
            # HA is shutting down: no power actions, just hand the link back.
            pass
        elif resumed:
            power = await _async_restore_power(
                coordinator,
                result=result,
                failure=failure,
                touched_device=touched_device,
                was_on=was_on,
                auto_powered=auto_powered,
            )
        elif result is not None and result.powered_down:
            power = POWER_RESTORE_FAILED
    except Exception:
        _LOGGER.exception("Unexpected error cleaning up after the Lumagen firmware session")
    finally:
        coordinator.async_set_firmware_update_active(False)
        if entry_loaded and not resumed:
            coordinator.reload_pending = True
    return power


async def async_notify_install_complete(
    hass: HomeAssistant,
    coordinator: LumagenCoordinator,
    version: str,
    result: UpdateResult,
    power: str,
) -> None:
    """Tell the user a real install finished, and where power was left.

    The Z97 power-off is presented as the expected end of an update, not a
    fault. An unconfirmed power restore is its own notification: the install
    succeeded, but the user must check which power state the unit reached.
    """
    if power == POWER_RESTORE_FAILED:
        await async_notify(hass, coordinator, "firmware_repower_failed", {"version": version})
        return
    if not result.powered_down:
        return
    await async_notify(
        hass,
        coordinator,
        "firmware_update_complete_notification",
        {"version": version, "power": await async_power_text(hass, power)},
    )


def _map_error(err: Exception) -> HomeAssistantError | None:
    """Translate a library error into what the user sees. Subclasses first."""
    if isinstance(err, LumagenFirmwareImageError):
        return _error("firmware_image_invalid", err)
    if isinstance(err, LumagenFirmwareAbortError):
        return _error("firmware_aborted", err)
    if isinstance(err, LumagenConnectionError):
        return _error("firmware_connection_failed", err)
    if isinstance(err, LumagenFirmwareError):
        return _error("firmware_failed", err)
    return None


async def async_install_firmware(
    hass: HomeAssistant,
    coordinator: LumagenCoordinator,
    listing: ReleaseListing,
    *,
    progress: ProgressCallback,
) -> tuple[UpdateResult, str]:
    """Run an exclusive install, including cleanup of failed preparation."""
    try:
        return await _async_install_firmware(hass, coordinator, listing, progress=progress)
    finally:
        # A concurrent caller rejected by the lock must not clean up its owner.
        if not coordinator.firmware_lock.locked():
            if coordinator.firmware_update_active:
                coordinator.async_set_firmware_update_active(False)
            entry = coordinator.config_entry
            if (
                coordinator.reload_pending
                and hass.config_entries.async_get_entry(entry.entry_id) is entry
                and entry.state is ConfigEntryState.LOADED
            ):
                coordinator.reload_pending = False
                hass.config_entries.async_schedule_reload(entry.entry_id)


async def _async_install_firmware(
    hass: HomeAssistant,
    coordinator: LumagenCoordinator,
    listing: ReleaseListing,
    *,
    progress: ProgressCallback,
) -> tuple[UpdateResult, str]:
    """Download ``listing`` and write it to the Lumagen.

    Returns the session's result and how power was left (a POWER_* key).

    :raises HomeAssistantError: with a translation key naming what went wrong.
        Download and archive failures happen before the device is touched.
    """
    if coordinator.firmware_lock.locked():
        raise _error("firmware_update_in_progress")
    async with coordinator.firmware_lock:
        try:
            bundle = await async_fetch_bundle(hass, listing)
        except LumagenFirmwareImageError as err:
            raise _error("firmware_image_invalid", err) from err

        # An unload, reload, disable or delete during the download was refused
        # by async_unload_entry, but HA has still marked the entry FAILED_UNLOAD
        # (or removed it). Nothing has touched the device yet, so stop here
        # rather than flash behind an entry the user asked to go away.
        entry = coordinator.config_entry
        if (
            hass.config_entries.async_get_entry(entry.entry_id) is not entry
            or entry.state is not ConfigEntryState.LOADED
        ):
            _LOGGER.warning(
                "The Lumagen config entry %r was unloaded, disabled or removed during the "
                "firmware download (state: %s); not starting the update",
                entry.title,
                entry.state,
            )
            raise _error(
                "firmware_aborted",
                RuntimeError(f"the integration was unloaded during the download ({entry.state})"),
            )

        # Recorded after the download (which can take minutes, during which the
        # user may switch the unit on or off) and before any power action: this
        # is what gets restored.
        selected_state = coordinator.client.state
        was_on = selected_state.power_on
        if was_on is None:
            raise _error("firmware_power_unknown")

        coordinator.async_set_firmware_update_active(True)
        auto_powered = False
        if not was_on:
            await _async_auto_power_on(coordinator)
            auto_powered = True

        touched_device = False
        result: UpdateResult | None = None
        try:
            await coordinator.async_pause_client()
            if (
                hass.config_entries.async_get_entry(entry.entry_id) is not entry
                or entry.state is not ConfigEntryState.LOADED
            ):
                raise LumagenFirmwareAbortError(
                    f"the integration was unloaded during power-on ({entry.state})"
                )
            # The entry's own URL, verbatim. Inside HA that's
            # esphome-hass://esphome/<entry>?port_name=Lumagen, which carries no
            # PSK; building an esphome:// URL here would reintroduce the
            # percent-encoding hazard for no benefit.
            async with FirmwareSession(
                coordinator.config_entry.data[CONF_URL], baudrate=SESSION_BAUD
            ) as session:
                touched_device = True
                result = await session.run_update(
                    bundle,
                    baudrate=FIRMWARE_UPDATE_BAUDRATE,
                    progress=progress,
                    expected_model_number=selected_state.model_number,
                    expected_serial=selected_state.serial,
                )
        except asyncio.CancelledError:
            await _async_finish(
                hass,
                coordinator,
                result=None,
                failure=None,
                cancelled=True,
                touched_device=touched_device,
                was_on=was_on,
                auto_powered=auto_powered,
            )
            raise
        except Exception as err:
            _LOGGER.warning("Lumagen firmware update failed: %s", err)
            await _async_finish(
                hass,
                coordinator,
                result=None,
                failure=err,
                cancelled=False,
                touched_device=touched_device,
                was_on=was_on,
                auto_powered=auto_powered,
            )
            mapped = _map_error(err)
            if mapped is None:
                raise
            if mapped.translation_key == "firmware_failed":
                # An outcome the library couldn't confirm deserves more than a
                # toast; the message says whether to power-cycle. A failure to
                # post it must not replace the error the user needs to see.
                try:
                    await async_notify(
                        hass, coordinator, "firmware_failed_notification", {"error": str(err)}
                    )
                except Exception:
                    _LOGGER.exception("Could not post the firmware-failure notification")
            raise mapped from err

        try:
            _log_result(result)
        except Exception:
            # Diagnostics only; must not skip the cleanup below.
            _LOGGER.exception("Could not log the Lumagen firmware session result")
        power = await _async_finish(
            hass,
            coordinator,
            result=result,
            failure=None,
            cancelled=False,
            touched_device=touched_device,
            was_on=was_on,
            auto_powered=auto_powered,
        )
        return result, power

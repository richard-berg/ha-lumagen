"""The Lumagen Radiance Pro integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from aiolumagen import INPUT_LABEL_MAX_LENGTH
from aiolumagen.commands import osd_block_char_command, osd_message_command
from homeassistant.const import ATTR_AREA_ID, ATTR_DEVICE_ID, ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util.hass_dict import HassKey

from . import coordinator as _coordinator
from .const import (
    ATTR_BLOCK_CHAR,
    ATTR_CENTER,
    ATTR_COMMAND,
    ATTR_CR,
    ATTR_DURATION,
    ATTR_INPUT,
    ATTR_LABEL,
    ATTR_LINE1,
    ATTR_LINE2,
    ATTR_MEMORY,
    ATTR_MESSAGE,
    CONF_POLL_INTERVAL,
    CONF_URL,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    INPUT_LABEL_MEMORIES,
    MAX_ADDRESSABLE_INPUT,
    OSD_DURATION_MAX,
    OSD_DURATION_MIN,
    PLATFORMS,
    SERVICE_RESTART_INPUT,
    SERVICE_SEND_OSD_MESSAGE,
    SERVICE_SEND_RAW_COMMAND,
    SERVICE_SET_INPUT_LABEL,
)
from .coordinator import LumagenConfigEntry, LumagenCoordinator, lumagen_command
from .release_coordinator import LumagenReleaseCoordinator

_LOGGER = logging.getLogger(__name__)

_ANNOUNCED_OUTAGES: HassKey[set[str]] = HassKey(f"{DOMAIN}_announced_outages")
"""Entries whose device is unreachable and has already been reported so.

Every setup attempt builds a new client, and a new client would otherwise
report the outage again: once per setup retry, about 45 warnings an hour at
Home Assistant's retry backoff. Setup failing, or an unload with the device
unreachable, marks the entry; setup finding the device answering clears it.
Process-lifetime only, so the first attempt after a restart still says why.
"""


# Optional routing for the rare multi-Lumagen setup. These must be declared on
# every schema even though they're optional: a plain vol.Schema rejects
# undeclared keys, so without them any call carrying a target would fail
# validation before reaching the handler.
_TARGET_FIELDS: dict[Any, Any] = {
    vol.Optional(ATTR_DEVICE_ID): vol.All(cv.ensure_list, [cv.string]),
    vol.Optional(ATTR_ENTITY_ID): vol.All(cv.ensure_list, [cv.string]),
    vol.Optional(ATTR_AREA_ID): vol.All(cv.ensure_list, [cv.string]),
}

_SEND_RAW_COMMAND_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_COMMAND): vol.All(cv.string, vol.Length(min=1, max=64)),
        vol.Optional(ATTR_CR, default=False): cv.boolean,
        **_TARGET_FIELDS,
    }
)

# Lengths aren't capped here. The library trims each row to the device's field
# width and validates the rest, so duplicating those limits in the schema would
# create a second place to keep in step with the hardware.
_SEND_OSD_MESSAGE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_MESSAGE): cv.string,
        vol.Optional(ATTR_LINE1): cv.string,
        vol.Optional(ATTR_LINE2): cv.string,
        vol.Optional(ATTR_DURATION, default=3): vol.All(
            vol.Coerce(int), vol.Range(min=OSD_DURATION_MIN, max=OSD_DURATION_MAX)
        ),
        vol.Optional(ATTR_CENTER, default=False): cv.boolean,
        vol.Optional(ATTR_BLOCK_CHAR): vol.All(cv.string, vol.Length(min=1, max=1)),
        **_TARGET_FIELDS,
    }
)

_SET_INPUT_LABEL_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_INPUT): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=MAX_ADDRESSABLE_INPUT)
        ),
        # Length is capped here as well as in the library, because the UI
        # selector needs a number to enforce and a rejected label is a poor way
        # to discover the limit.
        vol.Required(ATTR_LABEL): vol.All(cv.string, vol.Length(max=INPUT_LABEL_MAX_LENGTH)),
        vol.Optional(ATTR_MEMORY, default="ALL"): vol.In(INPUT_LABEL_MEMORIES),
        **_TARGET_FIELDS,
    }
)

_RESTART_INPUT_SCHEMA = vol.Schema(
    {
        # Omitted means every input, matching the client default.
        vol.Optional(ATTR_INPUT): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=MAX_ADDRESSABLE_INPUT)
        ),
        **_TARGET_FIELDS,
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: LumagenConfigEntry) -> bool:
    """Set up a Lumagen from a config entry."""
    poll_interval = entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
    announced = hass.data.setdefault(_ANNOUNCED_OUTAGES, set())
    client = await _coordinator.create_lumagen_client(
        entry.data[CONF_URL],
        poll_interval=poll_interval,
        outage_announced=entry.entry_id in announced,
    )
    lumagen_coordinator = LumagenCoordinator(hass, entry, client)
    try:
        await lumagen_coordinator.async_config_entry_first_refresh()
    except ConfigEntryNotReady:
        announced.add(entry.entry_id)
        raise
    _note_outage(hass, entry, client.available)
    # Deliberately not refreshed here: lumagen.com must never delay or fail
    # setup. The update entity triggers the first check when it's added.
    lumagen_coordinator.release_coordinator = LumagenReleaseCoordinator(hass, entry)
    entry.runtime_data = lumagen_coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _async_register_services(hass)
    # The poll interval is baked into the client at construction, so a change
    # to it only takes effect on reload.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: LumagenConfigEntry) -> None:
    """Reload the entry so a new poll interval or firmware channel takes effect.

    Not while a firmware update runs: the reload would tear down the update
    entity mid-flash. The install schedules the reload itself when it ends.
    """
    coordinator = entry.runtime_data
    if coordinator.firmware_update_active or coordinator.firmware_lock.locked():
        coordinator.reload_pending = True
        _LOGGER.info("Options changed during a firmware update; reloading once it finishes")
        return
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: LumagenConfigEntry) -> bool:
    """Unload a config entry — stop the client and drop the platforms.

    Refused while a firmware install holds the lock (downloading, powering on,
    or flashing): unloading would stop nothing — the install runs on in its
    own task — but a reload would start a new client that competes with the
    session for the serial proxy mid-flash. HA marks a refused unload as
    failed until restart; the install sees that and leaves the client stopped
    when it finishes (see firmware._async_finish).
    """
    if entry.runtime_data.firmware_lock.locked():
        _LOGGER.warning(
            "Not unloading %r: a firmware update is in progress. Its client will stay "
            "stopped once the update finishes; restart Home Assistant then to complete "
            "the unload",
            entry.title,
        )
        return False
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    # Read before stopping: a stopped client never reports available.
    _note_outage(hass, entry, entry.runtime_data.client.available)
    await entry.runtime_data.async_shutdown()
    # Tear down the service when the last config entry goes away. HA's
    # service registry doesn't support per-entry services natively, so we
    # use a single domain-level service that dispatches to whichever
    # config entry is loaded.
    if not hass.config_entries.async_loaded_entries(DOMAIN):
        for service in _DOMAIN_SERVICES:
            hass.services.async_remove(DOMAIN, service)
    return True


def _note_outage(hass: HomeAssistant, entry: LumagenConfigEntry, available: bool) -> None:
    """Record whether the entry's device is in an outage that has been reported.

    An unavailable client has reported it: either it announced the outage, or
    its own startup handshake failed and said so (unless it was built quiet
    because the outage was already announced).
    """
    announced = hass.data.setdefault(_ANNOUNCED_OUTAGES, set())
    if available:
        announced.discard(entry.entry_id)
    else:
        announced.add(entry.entry_id)


_DOMAIN_SERVICES = (
    SERVICE_SEND_RAW_COMMAND,
    SERVICE_SEND_OSD_MESSAGE,
    SERVICE_SET_INPUT_LABEL,
    SERVICE_RESTART_INPUT,
)


def _async_register_services(hass: HomeAssistant) -> None:
    """Idempotently register the domain-level send_raw_command service.

    The service is keyed off the integration domain. With multiple Lumagens,
    a target must resolve to exactly one loaded entry.
    """
    # Checks every service rather than one as a proxy, so adding a service to
    # _DOMAIN_SERVICES can't be silently skipped on a second config entry.
    if all(hass.services.has_service(DOMAIN, name) for name in _DOMAIN_SERVICES):
        return

    @lumagen_command
    async def _handle_send_raw_command(call: ServiceCall) -> None:
        command: str = call.data[ATTR_COMMAND]
        cr: bool = call.data[ATTR_CR]
        coordinator = _coordinator_for(hass, call)
        _LOGGER.debug("send_raw_command -> command=%r cr=%s", command, cr)
        # refresh=False — the caller is doing protocol-level work and
        # almost certainly doesn't want a follow-up status query muddying
        # the response stream they're watching.
        await coordinator.client.send_command(command, cr=cr, refresh=False)

    @lumagen_command
    async def _handle_send_osd_message(call: ServiceCall) -> None:
        coordinator = _coordinator_for(hass, call)
        block_char: str | None = call.data.get(ATTR_BLOCK_CHAR)
        message = call.data.get(ATTR_MESSAGE)
        options = {
            "line1": call.data.get(ATTR_LINE1),
            "line2": call.data.get(ATTR_LINE2),
            "duration": call.data[ATTR_DURATION],
            "center": call.data[ATTR_CENTER],
        }
        # Validate both commands before changing the device's sticky setting.
        osd_message_command(
            text=message,
            line1=call.data.get(ATTR_LINE1),
            line2=call.data.get(ATTR_LINE2),
            duration=call.data[ATTR_DURATION],
            center=call.data[ATTR_CENTER],
        )
        if block_char is not None:
            osd_block_char_command(block_char)
        async with coordinator.osd_lock:
            coordinator.async_check_command_allowed()
            if block_char is not None:
                await coordinator.client.set_osd_block_char(block_char)
            coordinator.async_check_command_allowed()
            await coordinator.client.show_message(message, **options)

    @lumagen_command
    async def _handle_set_input_label(call: ServiceCall) -> None:
        coordinator = _coordinator_for(hass, call)
        await coordinator.client.set_input_label(
            call.data[ATTR_INPUT],
            call.data[ATTR_LABEL],
            memory=call.data[ATTR_MEMORY],
        )

    @lumagen_command
    async def _handle_restart_input(call: ServiceCall) -> None:
        coordinator = _coordinator_for(hass, call)
        await coordinator.client.restart_input(call.data.get(ATTR_INPUT, "all"))

    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_RAW_COMMAND,
        _handle_send_raw_command,
        schema=_SEND_RAW_COMMAND_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_OSD_MESSAGE,
        _handle_send_osd_message,
        schema=_SEND_OSD_MESSAGE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_INPUT_LABEL,
        _handle_set_input_label,
        schema=_SET_INPUT_LABEL_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RESTART_INPUT,
        _handle_restart_input,
        schema=_RESTART_INPUT_SCHEMA,
    )


def _coordinator_for(hass: HomeAssistant, call: ServiceCall) -> LumagenCoordinator:
    """Resolve the coordinator a service call should act on.

    Shared by every service so targeting behaves identically across them:
    optional with one Lumagen, honoured when given, and loud when a target
    matches nothing (see :func:`_resolve_target_entry`).
    """
    loaded_entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not loaded_entries:
        raise ServiceValidationError("No Lumagen config entries are loaded; cannot send command.")
    target_entry = _resolve_target_entry(hass, call, loaded_entries)
    coordinator: LumagenCoordinator = target_entry.runtime_data
    if coordinator.firmware_update_active:
        # The client is stopped and the serial link belongs to the session.
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="firmware_update_active"
        )
    return coordinator


def _resolve_target_entry(
    hass: HomeAssistant,
    call: ServiceCall,
    loaded_entries: list[LumagenConfigEntry],
) -> LumagenConfigEntry:
    """Pick the config entry the service call should route to.

    Targeting is optional — with a single Lumagen (the overwhelmingly
    common case) the call needs no target at all and lands on the only
    loaded entry. ``device_id`` and ``entity_id`` are both honored for
    multi-Lumagen setups; an entity is resolved to its device first.

    Raises if a device/entity target is given that doesn't match any
    loaded Lumagen entry, so a misrouted automation fails loudly instead
    of quietly commanding the wrong unit. Area targets and ambiguous
    multi-device calls are refused rather than silently partially applied.
    """
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    device_ids: list[str] = list(call.data.get(ATTR_DEVICE_ID) or [])
    entity_ids: list[str] = list(call.data.get(ATTR_ENTITY_ID) or [])
    if call.data.get(ATTR_AREA_ID):
        raise ServiceValidationError(
            "Area targets are not supported; select exactly one Lumagen device or entity."
        )

    # Resolve any entity targets to their owning device.
    if entity_ids:
        ent_reg = er.async_get(hass)
        for entity_id in entity_ids:
            entity = ent_reg.async_get(entity_id)
            if entity is not None and entity.device_id is not None:
                device_ids.append(entity.device_id)

    if not device_ids:
        if entity_ids:
            raise ServiceValidationError(
                f"None of the targeted entities have a device linked to a loaded Lumagen "
                f"config entry: {entity_ids!r}"
            )
        if len(loaded_entries) != 1:
            raise ServiceValidationError(
                "Multiple Lumagens are loaded; target exactly one device or entity."
            )
        return loaded_entries[0]

    dev_reg = dr.async_get(hass)
    matched_entry_ids: set[str] = set()
    for device_id in device_ids:
        device = dev_reg.async_get(device_id)
        if device is None:
            continue
        for entry in loaded_entries:
            if entry.entry_id in _device_entry_ids(device):
                matched_entry_ids.add(entry.entry_id)
    if len(matched_entry_ids) > 1:
        raise ServiceValidationError("Target exactly one Lumagen device or entity per call.")
    if matched_entry_ids:
        return next(entry for entry in loaded_entries if entry.entry_id in matched_entry_ids)
    raise ServiceValidationError(
        f"None of the targeted devices are linked to a loaded Lumagen config entry: {device_ids!r}"
    )


def _device_entry_ids(device: Any) -> set[str]:
    """The config entries a device belongs to.

    Home Assistant 2026.12 gives a device a single ``config_entry_id`` and
    deprecates ``config_entries`` (removed in 2027.10); older releases only
    have the set.
    """
    if (entry_id := getattr(device, "config_entry_id", None)) is not None:
        return {entry_id}
    return set(device.config_entries)

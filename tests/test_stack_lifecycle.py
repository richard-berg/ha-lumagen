"""Exercise HA with the real library and a fragmented local TCP device."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from dataclasses import dataclass, field

import pytest
from aiolumagen import LumagenClient, LumagenTransport
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.lumagen.const import DOMAIN

from .firmware_helpers import setup_entry

_QUERY = re.compile(rb"ZQ([ISO]\d\d|S1[A-D])")
_REPORT = b"1,059,2160,0,0,178,178,-,0,000e,0,0,059,2160,237,2,0,p,P,01,01,178,178,A,1,0,2"


@dataclass
class Device:
    writers: set[asyncio.StreamWriter] = field(default_factory=set)
    tasks: set[asyncio.Task[None]] = field(default_factory=set)
    received: bytearray = field(default_factory=bytearray)
    connections: int = 0
    peak_connections: int = 0
    silent: bool = False
    url: str = ""

    def accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(self.serve(reader, writer))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.writers.add(writer)
        self.connections += 1
        self.peak_connections = max(self.peak_connections, len(self.writers))
        pending = b""
        try:
            while data := await reader.read(1024):
                self.received.extend(data)
                pending += data
                while match := _QUERY.search(pending):
                    code = match[1]
                    end = match.end()
                    if code.startswith(b"S1"):
                        if len(pending) == end:
                            break
                        number = int(pending[end : end + 1]) + 1
                        payload = f"Source {number}".encode()
                        end += 1
                    else:
                        payload = {
                            b"S01": b"RadiancePro,030326,1018,003830",
                            b"S02": b"1",
                            b"I25": _REPORT,
                            b"I30": b"Y4N",
                        }.get(code, b"")
                    pending = pending[end:]
                    if not self.silent:
                        reply = b"!" + code + b"," + payload + b"\r\n"
                        for offset in range(0, len(reply), 3):
                            writer.write(reply[offset : offset + 3])
                            await writer.drain()
                            await asyncio.sleep(0)
                pending = pending[-6:]
        except ConnectionError:
            pass
        finally:
            self.writers.discard(writer)
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()


@pytest.fixture
async def device(socket_enabled: None) -> AsyncIterator[Device]:
    device = Device()
    server = await asyncio.start_server(device.accept, "127.0.0.1", 0)
    device.url = f"socket://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    try:
        yield device
    finally:
        server.close()
        await server.wait_closed()
        for writer in tuple(device.writers):
            writer.close()
        if device.tasks:
            await asyncio.gather(*device.tasks)


async def _until(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(8):
        while not condition():
            await asyncio.sleep(0.01)


async def test_real_stack_recovers_without_duplicate_owners_or_unsolicited_writes(
    hass: HomeAssistant, device: Device
) -> None:
    client = LumagenClient(
        LumagenTransport(device.url),
        power_poll_interval=0.05,
        status_poll_interval=0.05,
        stale_timeout=0.3,
    )
    entry = await setup_entry(hass, client, url=device.url)
    coordinator = entry.runtime_data
    observed: list[bool] = []
    unsubscribe = coordinator.async_add_listener(lambda: observed.append(client.available))
    registry = er.async_get(hass)
    player_id = registry.async_get_entity_id("media_player", DOMAIN, "test_lumagen_media_player")
    assert player_id is not None
    original_entities = set(hass.states.async_entity_ids())
    try:
        state = hass.states.get(player_id)
        assert state is not None
        assert state.state == "on"
        assert state.attributes["source"] == "Source 1"
        assert state.attributes["source_list"] == [f"Source {n}" for n in range(1, 9)]
        assert coordinator.data.model_number == "1018"
        assert coordinator.data.source_refresh_hz == 59.94
        for _ in range(3):
            previous_connections = device.connections
            observed.clear()
            for writer in tuple(device.writers):
                writer.close()
            await _until(
                lambda previous=previous_connections: (
                    device.connections > previous
                    and client.available
                    and False in observed
                    and observed[-1] is True
                )
            )
            await hass.async_block_till_done()
            assert set(hass.states.async_entity_ids()) == original_entities
        assert device.peak_connections == 1
        # Startup/reconnect must not power on, switch inputs, or change settings.
        assert re.fullmatch(rb"(?:ZE2|ZQ(?:[ISO]\d\d|S1[A-D][0-7]))*", bytes(device.received))
    finally:
        unsubscribe()
        await hass.config_entries.async_unload(entry.entry_id)
    await _until(lambda: not device.writers and not device.tasks)


async def test_real_stack_noise_cannot_keep_dead_device_available(
    hass: HomeAssistant, device: Device
) -> None:
    client = LumagenClient(
        LumagenTransport(device.url),
        power_poll_interval=0.05,
        status_poll_interval=0.05,
        stale_timeout=0.3,
    )
    entry = await setup_entry(hass, client, url=device.url)
    identity = (entry.runtime_data.data.model, entry.runtime_data.data.firmware)
    device.silent = True
    done = asyncio.Event()

    async def noise() -> None:
        while not done.is_set():
            for writer in tuple(device.writers):
                with suppress(ConnectionError):
                    writer.write(b"\xffnoise\r\n!I25,damaged\r\n!I30,garbage\r\n")
                    await writer.drain()
            await asyncio.sleep(0.01)

    task = asyncio.create_task(noise())
    try:
        async with asyncio.timeout(1):
            while client.available:
                await asyncio.sleep(0.01)
        assert (entry.runtime_data.data.model, entry.runtime_data.data.firmware) == identity
        done.set()
        await task
        device.silent = False
        await _until(lambda: client.available)
        assert (entry.runtime_data.data.model, entry.runtime_data.data.firmware) == identity
    finally:
        done.set()
        await task
        await hass.config_entries.async_unload(entry.entry_id)

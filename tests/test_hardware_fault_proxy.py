"""Tests for hardware/fault_proxy.py against a fake Lumagen on a TCP socket.

The proxy is test equipment: if it misreports what Home Assistant sent, or a
fault doesn't do what it says, every hardware result built on it is wrong.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

_PATH = Path(__file__).parent.parent / "hardware" / "fault_proxy.py"
_SPEC = importlib.util.spec_from_file_location("fault_proxy", _PATH)
assert _SPEC and _SPEC.loader
fault_proxy = importlib.util.module_from_spec(_SPEC)
sys.modules["fault_proxy"] = fault_proxy
_SPEC.loader.exec_module(fault_proxy)

S01 = b"!S01,RadiancePro,030225,1018,003830\r\n"


class FakeLumagen:
    """Answers ZQS01, however it arrives; records everything it receives."""

    def __init__(self) -> None:
        self.received = b""
        self.writers: list[asyncio.StreamWriter] = []

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.writers.append(writer)
        pending = b""
        while data := await reader.read(1024):
            self.received += data
            pending += data
            while (i := pending.find(b"ZQS01")) >= 0:
                writer.write(S01)
                pending = pending[i + 5 :]
            pending = pending[-4:]
            await writer.drain()


class Rig:
    def __init__(self, proxy, port: int, device: FakeLumagen, tmp: Path) -> None:
        self.proxy = proxy
        self.port = port
        self.device = device
        self.log = tmp / "proxy.jsonl"

    async def connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await asyncio.open_connection("127.0.0.1", self.port)

    async def ctl(self, line: str) -> str:
        return await self.proxy.command(line)

    def events(self, kind: str) -> list[dict]:
        lines = self.log.read_text().splitlines()
        return [e for e in map(json.loads, lines) if e["kind"] == kind]


async def _read_line(reader: asyncio.StreamReader, timeout: float = 1.0) -> bytes:
    return await asyncio.wait_for(reader.readuntil(b"\n"), timeout)


async def _drain(reader: asyncio.StreamReader) -> None:
    with contextlib.suppress(TimeoutError):
        while await asyncio.wait_for(reader.read(4096), 0.2):
            pass


async def _silent(reader: asyncio.StreamReader, timeout: float = 0.4) -> bool:
    try:
        await asyncio.wait_for(reader.read(1), timeout)
    except TimeoutError:
        return True
    return False


@pytest.fixture
async def rig(socket_enabled: None, tmp_path: Path) -> AsyncIterator[Rig]:
    # The proxy is TCP to the bone; this is the only test file that needs it.
    device = FakeLumagen()
    device_server = await asyncio.start_server(device.handle, "127.0.0.1", 0)
    device_port = device_server.sockets[0].getsockname()[1]
    proxy = fault_proxy.FaultProxy(
        f"socket://127.0.0.1:{device_port}",
        9600,
        fault_proxy.Recorder(tmp_path / "proxy.jsonl"),
        seed=1,
    )
    server = await asyncio.start_server(proxy.handle_client, "127.0.0.1", 0)
    tasks = [
        asyncio.create_task(proxy.run_upstream()),
        asyncio.create_task(proxy.run_down_sender()),
    ]
    async with asyncio.timeout(2):
        await proxy._upstream_ready.wait()
    yield Rig(proxy, server.sockets[0].getsockname()[1], device, tmp_path)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    for writer in device.writers:
        writer.close()
    server.close()
    device_server.close()


async def test_relays_both_ways_and_queries_are_not_writes(rig: Rig) -> None:
    reader, writer = await rig.connect()
    writer.write(b"ZE2")
    await writer.drain()
    await asyncio.sleep(0.05)
    writer.write(b"ZQS01ZQI25")
    await writer.drain()
    assert await _read_line(reader) == S01
    assert rig.device.received == b"ZE2ZQS01ZQI25"
    assert rig.proxy.counters.writes_unsolicited == 0
    writer.close()


async def test_state_changing_command_is_unsolicited_unless_expected(rig: Rig) -> None:
    _, writer = await rig.connect()
    writer.write(b"i4")
    await writer.drain()
    await asyncio.sleep(0.3)
    assert rig.proxy.counters.writes_unsolicited == 1
    assert rig.events("unsolicited")[0]["command"] == "i4"

    assert await rig.ctl("expect-write 5 exerciser input 5") == "ok"
    writer.write(b"ZY551\r")
    await writer.drain()
    await asyncio.sleep(0.3)
    assert rig.proxy.counters.writes_unsolicited == 1
    assert rig.proxy.counters.writes_expected == 1
    assert rig.events("write")[0]["expected"] == "exerciser input 5"
    writer.close()


async def test_query_split_across_reads_is_still_a_query(rig: Rig) -> None:
    reader, writer = await rig.connect()
    for part in (b"Z", b"QS", b"01"):
        writer.write(part)
        await writer.drain()
        await asyncio.sleep(0.05)
    assert await _read_line(reader) == S01
    await asyncio.sleep(0.3)
    assert rig.proxy.counters.writes_unsolicited == 0
    writer.close()


async def test_drop_down_silences_device_until_it_expires(rig: Rig) -> None:
    reader, writer = await rig.connect()
    assert await rig.ctl("drop-down on 0.5") == "ok"
    writer.write(b"ZQS01")
    await writer.drain()
    assert await _silent(reader)
    await asyncio.sleep(0.3)
    writer.write(b"ZQS01")
    await writer.drain()
    assert await _read_line(reader) == S01
    writer.close()


async def test_line_faults(rig: Rig) -> None:
    reader, writer = await rig.connect()
    await rig.ctl("dup 1")
    writer.write(b"ZQS01")
    await writer.drain()
    assert [await _read_line(reader), await _read_line(reader)] == [S01, S01]

    await rig.ctl("clear")
    await rig.ctl("corrupt 1")
    writer.write(b"ZQS01")
    await writer.drain()
    damaged = await asyncio.wait_for(reader.read(len(S01)), 1)
    assert damaged != S01[: len(damaged)]

    await rig.ctl("clear")
    await _drain(reader)
    await rig.ctl("echo on")
    writer.write(b"ZQS01")
    await writer.drain()
    assert await _read_line(reader) == b"ZQS01" + S01

    await rig.ctl("clear")
    await rig.ctl("eol lf")
    writer.write(b"ZQS01")
    await writer.drain()
    assert await _read_line(reader) == S01.replace(b"\r\n", b"\n")
    writer.close()


async def test_inject_and_latency(rig: Rig) -> None:
    reader, writer = await rig.connect()
    assert await rig.ctl("inject unknown_code") == "ok"
    assert await _read_line(reader) == b"!Z99,1,2,3\r\n"
    assert await rig.ctl(r"inject !I25,1\r\n") == "ok"
    assert await _read_line(reader) == b"!I25,1\r\n"

    assert await rig.ctl("latency-down 300") == "ok"
    writer.write(b"ZQS01")
    await writer.drain()
    assert await _silent(reader, 0.2)
    assert await _read_line(reader) == S01
    writer.close()


async def test_one_client_at_a_time(rig: Rig) -> None:
    _, first = await rig.connect()
    await asyncio.sleep(0.05)
    second_reader, _ = await rig.connect()
    assert await asyncio.wait_for(second_reader.read(1), 1) == b""
    assert rig.proxy.counters.concurrent_refused == 1
    first.close()


async def test_refuse_drops_the_client_and_new_ones(rig: Rig) -> None:
    reader, _ = await rig.connect()
    await asyncio.sleep(0.05)
    assert await rig.ctl("refuse 0.5") == "ok"
    assert await asyncio.wait_for(reader.read(1), 1) == b""
    late_reader, _ = await rig.connect()
    assert await asyncio.wait_for(late_reader.read(1), 1) == b""
    await asyncio.sleep(0.6)
    reader, writer = await rig.connect()
    writer.write(b"ZQS01")
    await writer.drain()
    assert await _read_line(reader) == S01
    writer.close()


async def test_upstream_off_drops_client_then_recovers(rig: Rig) -> None:
    reader, _ = await rig.connect()
    await asyncio.sleep(0.05)
    assert await rig.ctl("upstream-off 0.5") == "ok"
    assert await asyncio.wait_for(reader.read(1), 1) == b""
    await asyncio.sleep(0.05)
    assert not rig.proxy.status()["upstream_open"]
    async with asyncio.timeout(3):
        await rig.proxy._upstream_ready.wait()
    reader, writer = await rig.connect()
    writer.write(b"ZQS01")
    await writer.drain()
    assert await _read_line(reader) == S01
    writer.close()


async def test_control_errors_and_status(rig: Rig) -> None:
    assert (await rig.ctl("bogus 1")).startswith("error")
    assert (await rig.ctl("latency-up")).startswith("error")
    assert await rig.ctl("mark some free text") == "ok"
    assert rig.events("mark")[0]["label"] == "some free text"
    assert await rig.ctl("noise 0.5 60") == "ok"
    status = json.loads(await rig.ctl("status"))
    assert status["faults"] == {"noise": 0.5}
    await rig.ctl("clear")
    assert json.loads(await rig.ctl("status"))["faults"] == {}

#!/usr/bin/env python3
"""Fault-injecting proxy between Home Assistant and a Lumagen's serial port.

The proxy owns the real port (any serialx URL) and serves it on a TCP port.
Point a Lumagen config entry at ``socket://127.0.0.1:14999`` and every byte
passes through here, where it is logged, audited and, on command, damaged.

Two jobs:

* **Audit.** Every command Home Assistant sends is classified. Queries
  (``ZQ...``) and the handshake (``ZE2``) are read-only; anything else changes
  the device. A state-changing command outside an announced window
  (``expect-write``) is counted as unsolicited: the integration must never
  change the device on its own, not on start, reconnect or recovery.
* **Faults.** Latency, silence in either direction, bit flips, noise, malformed
  or duplicated or truncated lines, byte-at-a-time delivery, stalls, echo,
  other line endings, dropped and refused connections, and the device side
  going away.

Control it with line commands on the control port, for example
``nc 127.0.0.1 14998``, then ``help``. ``lumagen_probe.py scenarios`` drives it
automatically.

Only one client at a time, like a serial port or an ESPHome serial proxy: a
second connection is closed at once and counted, since the integration should
never open two.

Usage::

    fault_proxy.py /dev/ttyUSB0 [--listen 127.0.0.1:14999]
        [--control 127.0.0.1:14998] [--log fault_proxy.jsonl]
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import random
import re
import shlex
import socket
import struct
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import serialx

_LOGGER = logging.getLogger("fault_proxy")

# A query (ZQ + three characters, or the six-character ZQS1<memory><input>
# label query) or the ZE2 handshake. Everything else changes the device.
_READ_ONLY = re.compile(r"ZQS1[A-Z][0-9]|ZQ[A-Z0-9]{3}|ZE2")
# What a read-only command can look like before all of it has arrived.
_PARTIAL = re.compile(r"Z|ZQ|ZQ[A-Z0-9]{1,2}|ZQS1|ZQS1[A-Z]|ZE")
_PARTIAL_HOLD = 0.2
_ALIASES = {
    "latency_up": "latency_up_ms",
    "latency_down": "latency_down_ms",
    "jitter": "jitter_ms",
    "split": "split_ms",
}
_IDLE_FLUSH = 0.1


@dataclass
class Faults:
    """Faults currently applied. ``down`` is device to Home Assistant."""

    latency_up_ms: int = 0
    latency_down_ms: int = 0
    jitter_ms: int = 0
    drop_up: bool = False
    drop_down: bool = False
    corrupt: float = 0.0
    noise: float = 0.0
    dup: float = 0.0
    truncate: float = 0.0
    split_ms: int = 0
    echo: bool = False
    eol: str = "crlf"
    stall: bool = False

    def active(self) -> dict[str, Any]:
        default = Faults()
        return {k: v for k, v in asdict(self).items() if getattr(default, k) != v}


@dataclass
class Counters:
    clients_accepted: int = 0
    clients_refused: int = 0
    concurrent_refused: int = 0
    bytes_up: int = 0
    bytes_down: int = 0
    lines_down: int = 0
    writes_expected: int = 0
    writes_unsolicited: int = 0
    upstream_opens: int = 0
    upstream_failures: int = 0
    unsolicited: list[dict[str, Any]] = field(default_factory=list)


class Recorder:
    """Append-only JSONL log of traffic, faults and marks."""

    def __init__(self, path: Path | None) -> None:
        self._file = path.open("a", buffering=1, encoding="utf-8") if path else None

    def write(self, kind: str, **fields: Any) -> None:
        record = {"t": datetime.now(UTC).isoformat(timespec="milliseconds"), "kind": kind}
        record.update(fields)
        if self._file:
            self._file.write(json.dumps(record) + "\n")
        if kind in {"fault", "clear", "mark", "unsolicited", "client", "upstream"}:
            _LOGGER.info("%s %s", kind, fields)


class FaultProxy:
    """Relay one client to the device, applying :class:`Faults`."""

    def __init__(self, url: str, baudrate: int, recorder: Recorder, seed: int | None) -> None:
        self.url = url
        self.baudrate = baudrate
        self.rec = recorder
        self.faults = Faults()
        self.counters = Counters()
        self.rng = random.Random(seed)
        self._serial_reader: asyncio.StreamReader | None = None
        self._serial_writer: asyncio.StreamWriter | None = None
        self._upstream_ready = asyncio.Event()
        self._upstream_off_until = 0.0
        self._refuse_until = 0.0
        self._client: asyncio.StreamWriter | None = None
        self._down_queue: asyncio.Queue[tuple[float, bytes]] = asyncio.Queue()
        self._stall_released = asyncio.Event()
        self._stall_released.set()
        self._expect_until = 0.0
        self._expect_label = ""
        self._last_up = b""
        self._echo_pending = b""
        self._timed: dict[str, asyncio.TimerHandle] = {}
        self._chaos: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    # ---- upstream (the real device) ----

    async def run_upstream(self) -> None:
        """Keep the device port open, reopening it whenever it goes away."""
        while True:
            now = time.monotonic()
            if now < self._upstream_off_until:
                await asyncio.sleep(self._upstream_off_until - now)
                continue
            try:
                reader, writer = await serialx.open_serial_connection(
                    self.url, baudrate=self.baudrate
                )
            except Exception as err:  # any open failure means retry
                self.counters.upstream_failures += 1
                self.rec.write("upstream", state="open_failed", error=repr(err))
                await asyncio.sleep(2)
                continue
            self.counters.upstream_opens += 1
            self._serial_reader, self._serial_writer = reader, writer
            self._upstream_ready.set()
            self.rec.write("upstream", state="open")
            try:
                await self._pump_down(reader)
            finally:
                self._upstream_ready.clear()
                self._serial_reader = self._serial_writer = None
                with contextlib.suppress(Exception):
                    writer.close()
                self.rec.write("upstream", state="closed")
                # A device that vanishes takes the client's link with it, as a
                # serial port that disappears would.
                await self._drop_client("upstream closed", rst=False)

    def upstream_off(self, seconds: float) -> None:
        self._upstream_off_until = time.monotonic() + seconds
        if self._serial_writer is not None:
            self._serial_writer.close()

    async def _pump_down(self, reader: asyncio.StreamReader) -> None:
        pending = b""
        while True:
            try:
                if pending:
                    chunk = await asyncio.wait_for(reader.read(4096), _IDLE_FLUSH)
                else:
                    chunk = await reader.read(4096)
            except TimeoutError:
                # A partial line with nothing behind it: pass it on as is.
                self._emit_down(pending, complete=False)
                pending = b""
                continue
            except Exception as err:
                self.rec.write("upstream", state="read_failed", error=repr(err))
                return
            if not chunk:
                return
            self.counters.bytes_down += len(chunk)
            pending += chunk
            *lines, pending = pending.split(b"\n")
            for line in lines:
                self._emit_down(line + b"\n", complete=True)

    def _emit_down(self, line: bytes, *, complete: bool) -> None:
        """Apply line-level faults to one line from the device and queue it."""
        f = self.faults
        self.rec.write("down", data=line.decode("latin-1"))
        if not line:
            return
        if complete:
            self.counters.lines_down += 1
        if f.drop_down or self._client is None:
            return
        out = line
        if f.echo and self._echo_pending and out.lstrip().startswith(b"!"):
            out = self._echo_pending + out
            self._echo_pending = b""
        if complete and f.eol != "crlf":
            body = out.rstrip(b"\r\n")
            out = body + {"lf": b"\n", "cr": b"\r", "lfcr": b"\n\r", "none": b""}[f.eol]
        if f.truncate and self.rng.random() < f.truncate:
            out = out[: self.rng.randrange(1, max(2, len(out)))]
        if f.corrupt:
            data = bytearray(out)
            for i in range(len(data)):
                if self.rng.random() < f.corrupt:
                    data[i] ^= 1 << self.rng.randrange(8)
            out = bytes(data)
        copies = 2 if f.dup and self.rng.random() < f.dup else 1
        for _ in range(copies):
            if f.noise and self.rng.random() < f.noise:
                noise = bytes(self.rng.randrange(256) for _ in range(self.rng.randrange(1, 16)))
                self._queue_down(noise)
            self._queue_down(out)

    def _queue_down(self, data: bytes) -> None:
        jitter = self.rng.uniform(0, self.faults.jitter_ms) if self.faults.jitter_ms else 0
        due = time.monotonic() + (self.faults.latency_down_ms + jitter) / 1000
        self._down_queue.put_nowait((due, data))

    async def run_down_sender(self) -> None:
        """Deliver queued device output to the client, in order, on time."""
        while True:
            due, data = await self._down_queue.get()
            await self._stall_released.wait()
            delay = due - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            writer = self._client
            if writer is None:
                continue
            try:
                if self.faults.split_ms:
                    for byte in data:
                        writer.write(bytes([byte]))
                        await writer.drain()
                        await asyncio.sleep(self.faults.split_ms / 1000)
                else:
                    writer.write(data)
                    await writer.drain()
            except ConnectionError, RuntimeError:
                continue

    # ---- the client (Home Assistant) ----

    async def handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        reason = None
        if self._client is not None:
            self.counters.concurrent_refused += 1
            reason = "another client is connected"
        elif time.monotonic() < self._refuse_until:
            reason = "refusing connections"
        elif not self._upstream_ready.is_set():
            reason = "device port is not open"
        if reason:
            self.counters.clients_refused += 1
            self.rec.write("client", state="refused", peer=str(peer), reason=reason)
            writer.close()
            return
        self.counters.clients_accepted += 1
        self._client = writer
        self.rec.write("client", state="connected", peer=str(peer))
        try:
            await self._pump_up(reader)
        finally:
            if self._client is writer:
                self._client = None
            with contextlib.suppress(Exception):
                writer.close()
            self.rec.write("client", state="disconnected", peer=str(peer))

    async def _pump_up(self, reader: asyncio.StreamReader) -> None:
        held = b""
        while True:
            try:
                chunk = await asyncio.wait_for(reader.read(4096), _PARTIAL_HOLD if held else None)
            except TimeoutError:
                self._audit(held, final=True)
                held = b""
                continue
            except ConnectionError:
                return
            if not chunk:
                if held:
                    self._audit(held, final=True)
                return
            self.counters.bytes_up += len(chunk)
            self.rec.write("up", data=chunk.decode("latin-1"))
            held = self._audit(held + chunk, final=False)
            self._last_up = chunk
            if self.faults.echo:
                self._echo_pending = chunk.rstrip(b"\r")
            if self.faults.drop_up:
                continue
            delay = self.faults.latency_up_ms / 1000
            if delay:
                self._spawn(self._write_up_later(chunk, delay))
            else:
                self._write_up(chunk)

    def _write_up(self, chunk: bytes) -> None:
        if self._serial_writer is not None:
            with contextlib.suppress(Exception):
                self._serial_writer.write(chunk)

    async def _write_up_later(self, chunk: bytes, delay: float) -> None:
        await asyncio.sleep(delay)
        self._write_up(chunk)

    def _audit(self, data: bytes, *, final: bool) -> bytes:
        """Classify commands in ``data``; return a trailing partial command to hold."""
        text = data.decode("latin-1").replace("\r", "").replace("\n", "")
        pos = 0
        while pos < len(text):
            match = _READ_ONLY.match(text, pos)
            if match:
                pos = match.end()
                continue
            rest = text[pos:]
            if not final and _PARTIAL.fullmatch(rest):
                return rest.encode("latin-1")
            self._state_changing(rest)
            break
        return b""

    def _state_changing(self, command: str) -> None:
        if time.monotonic() < self._expect_until:
            self.counters.writes_expected += 1
            self.rec.write("write", command=command, expected=self._expect_label)
            return
        self.counters.writes_unsolicited += 1
        entry = {"t": datetime.now(UTC).isoformat(timespec="seconds"), "command": command}
        self.counters.unsolicited.append(entry)
        del self.counters.unsolicited[:-50]
        self.rec.write("unsolicited", command=command)

    async def _drop_client(self, why: str, *, rst: bool) -> None:
        writer = self._client
        if writer is None:
            return
        self._client = None
        self.rec.write("client", state="dropped", reason=why, rst=rst)
        if rst:
            sock = writer.get_extra_info("socket")
            if sock is not None:
                with contextlib.suppress(OSError):
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            writer.transport.abort()
        else:
            writer.close()

    # ---- control ----

    def _spawn(self, coro: Any) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def set_fault(self, name: str, value: Any, seconds: float | None) -> None:
        if not hasattr(self.faults, name):
            raise ValueError(f"unknown fault {name!r}")
        current = getattr(self.faults, name)
        value = type(current)(value)
        setattr(self.faults, name, value)
        if name == "stall":
            if value:
                self._stall_released.clear()
            else:
                self._stall_released.set()
        self.rec.write("fault", name=name, value=value, seconds=seconds)
        if (old := self._timed.pop(name, None)) is not None:
            old.cancel()
        if seconds:
            default = getattr(Faults(), name)
            self._timed[name] = asyncio.get_running_loop().call_later(
                seconds, self._expire, name, default
            )

    def _expire(self, name: str, default: Any) -> None:
        self._timed.pop(name, None)
        setattr(self.faults, name, default)
        if name == "stall":
            self._stall_released.set()
        self.rec.write("clear", name=name)

    def clear(self) -> None:
        for handle in self._timed.values():
            handle.cancel()
        self._timed.clear()
        self.faults = Faults()
        self._stall_released.set()
        self._refuse_until = 0.0
        self._upstream_off_until = 0.0
        self.rec.write("clear", name="all")

    def status(self) -> dict[str, Any]:
        return {
            "client_connected": self._client is not None,
            "upstream_open": self._upstream_ready.is_set(),
            "faults": self.faults.active(),
            "refusing_for": max(0.0, round(self._refuse_until - time.monotonic(), 1)),
            "upstream_off_for": max(0.0, round(self._upstream_off_until - time.monotonic(), 1)),
            "chaos": self._chaos is not None and not self._chaos.done(),
            "counters": asdict(self.counters),
        }

    async def chaos(self, seconds: float, seed: int | None) -> None:
        """Apply random, time-boxed faults for ``seconds``; then clear."""
        rng = random.Random(seed)
        actions: list[tuple[str, Any]] = [
            ("latency_down_ms", lambda: rng.choice([200, 1000, 4000])),
            ("latency_up_ms", lambda: rng.choice([200, 1000, 4000])),
            ("jitter_ms", lambda: rng.choice([100, 800])),
            ("drop_down", lambda: True),
            ("drop_up", lambda: True),
            ("corrupt", lambda: rng.choice([0.001, 0.01, 0.05])),
            ("noise", lambda: rng.choice([0.05, 0.3])),
            ("dup", lambda: 0.3),
            ("truncate", lambda: rng.choice([0.05, 0.3])),
            ("split_ms", lambda: rng.choice([1, 10])),
            ("echo", lambda: True),
            ("stall", lambda: True),
            ("close", None),
            ("reset", None),
            ("refuse", None),
            ("upstream_off", None),
            ("inject", None),
        ]
        end = time.monotonic() + seconds
        self.rec.write("mark", label=f"chaos start {seconds}s seed={seed}")
        try:
            while time.monotonic() < end:
                await asyncio.sleep(rng.uniform(30, 300))
                name, make = rng.choice(actions)
                hold = rng.uniform(5, 120)
                if make is not None:
                    self.set_fault(name, make(), hold)
                elif name == "close":
                    await self._drop_client("chaos", rst=False)
                elif name == "reset":
                    await self._drop_client("chaos", rst=True)
                elif name == "refuse":
                    self._refuse_until = time.monotonic() + hold
                    self.rec.write("fault", name="refuse", seconds=hold)
                    await self._drop_client("chaos refuse", rst=False)
                elif name == "upstream_off":
                    self.rec.write("fault", name="upstream_off", seconds=hold)
                    self.upstream_off(hold)
                elif name == "inject":
                    self.inject(rng.choice(MALFORMED))
        finally:
            self.clear()
            self.rec.write("mark", label="chaos end")

    def inject(self, data: bytes) -> None:
        self.rec.write("fault", name="inject", data=data.decode("latin-1"))
        self._queue_down(data)

    async def command(self, line: str) -> str:
        args = shlex.split(line)
        if not args:
            return ""
        cmd, rest = args[0].lower(), args[1:]
        if cmd == "help":
            return HELP
        if cmd == "status":
            return json.dumps(self.status())
        if cmd == "clear":
            if self._chaos is not None:
                self._chaos.cancel()
            self.clear()
            return "ok"
        if cmd == "mark":
            self.rec.write("mark", label=" ".join(rest))
            return "ok"
        if cmd == "expect-write":
            self._expect_until = time.monotonic() + float(rest[0])
            self._expect_label = " ".join(rest[1:])
            return "ok"
        if cmd == "close":
            await self._drop_client("control", rst=False)
            return "ok"
        if cmd == "reset":
            await self._drop_client("control", rst=True)
            return "ok"
        if cmd == "refuse":
            self._refuse_until = time.monotonic() + float(rest[0])
            self.rec.write("fault", name="refuse", seconds=float(rest[0]))
            await self._drop_client("refuse", rst=False)
            return "ok"
        if cmd == "upstream-off":
            self.rec.write("fault", name="upstream_off", seconds=float(rest[0]))
            self.upstream_off(float(rest[0]))
            return "ok"
        if cmd == "inject":
            # The raw rest of the line: shlex would eat the backslash escapes.
            text = line.strip().split(None, 1)[1]
            if text in MALFORMED_BY_NAME:
                self.inject(MALFORMED_BY_NAME[text])
            else:
                self.inject(text.encode("latin-1").decode("unicode_escape").encode("latin-1"))
            return "ok"
        if cmd == "chaos":
            if self._chaos is not None:
                self._chaos.cancel()
            seed = int(rest[1]) if len(rest) > 1 else None
            self._chaos = asyncio.get_running_loop().create_task(self.chaos(float(rest[0]), seed))
            return "ok"
        name = cmd.replace("-", "_")
        name = _ALIASES.get(name, name)
        seconds = float(rest[1]) if len(rest) > 1 else None
        if name in {"drop_up", "drop_down", "echo", "stall"}:
            value: Any = rest[0].lower() in {"1", "on", "true", "yes"} if rest else True
        elif rest:
            value = rest[0]
        else:
            return f"error: {cmd} needs a value"
        try:
            self.set_fault(name, value, seconds)
        except ValueError as err:
            return f"error: {err}"
        return "ok"

    async def handle_control(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while line := await reader.readline():
                try:
                    reply = await self.command(line.decode().strip())
                except (IndexError, ValueError) as err:
                    reply = f"error: {err}"
                writer.write((reply + "\n").encode())
                await writer.drain()
        except ConnectionError:
            pass
        finally:
            writer.close()


# Device output that is wrong in ways a real link can produce: cut short,
# merged, overlong, binary, or a code nothing parses.
MALFORMED_BY_NAME: dict[str, bytes] = {
    "empty_s01": b"!S01,\r\n",
    "short_i25": b"!I25,1\r\n",
    "garbage_i25": b"!I25,zz,yy,xx,ww,vv,uu,tt,ss,rr,qq,pp,oo,nn,mm\r\n",
    "unknown_code": b"!Z99,1,2,3\r\n",
    "partial": b"!I2",
    "lone_cr": b"\r",
    "nul": b"\x00\x00\x00\r\n",
    "binary": bytes(range(256)) + b"\r\n",
    "overlong": b"!I25," + b"9," * 5000 + b"\r\n",
    "no_bang": b"S01,RadiancePro,999999,0000,000000\r\n",
    "bad_power": b"!S02,7\r\n",
    "bad_firmware": b"!S01,RadiancePro,,,\r\n",
}
MALFORMED = list(MALFORMED_BY_NAME.values())

HELP = (
    """\
Faults (add a number of seconds to make one temporary, e.g. `drop-down on 60`):
  latency-up MS | latency-down MS | jitter MS
  drop-up on|off | drop-down on|off          silence one direction, link stays up
  corrupt P | noise P | dup P | truncate P   probabilities, device to client
  split MS                                   deliver one byte at a time, MS apart
  echo on|off                                prefix replies with the last command
  eol crlf|lf|cr|lfcr|none                   line endings from the device
  stall on|off                               hold device output, then burst it
Actions:
  close | reset                              drop the client (FIN | RST)
  refuse SECONDS                             drop the client and refuse new ones
  upstream-off SECONDS                       close the device port for a while
  inject NAME|TEXT                           send malformed device output; names:
      """
    + " ".join(MALFORMED_BY_NAME)
    + """
  chaos SECONDS [SEED]                       random time-boxed faults, then clear
Bookkeeping:
  expect-write SECONDS [LABEL]               state-changing commands are expected
  mark TEXT | status | clear | help"""
)


def _host_port(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    return host or "127.0.0.1", int(port)


async def _main(args: argparse.Namespace) -> None:
    proxy = FaultProxy(args.url, args.baudrate, Recorder(args.log), args.seed)
    host, port = _host_port(args.listen)
    chost, cport = _host_port(args.control)
    server = await asyncio.start_server(proxy.handle_client, host, port)
    control = await asyncio.start_server(proxy.handle_control, chost, cport)
    _LOGGER.info("serving %s on %s:%d, control on %s:%d", args.url, host, port, chost, cport)
    async with server, control:
        await asyncio.gather(proxy.run_upstream(), proxy.run_down_sender())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("url", help="device serialx URL, e.g. /dev/ttyUSB0")
    parser.add_argument("--baudrate", type=int, default=9600)
    parser.add_argument("--listen", default="127.0.0.1:14999")
    parser.add_argument("--control", default="127.0.0.1:14998")
    parser.add_argument("--log", type=Path, help="JSONL traffic, fault and audit log")
    parser.add_argument("--seed", type=int, help="seed for probabilistic faults")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_main(args))


if __name__ == "__main__":
    main()

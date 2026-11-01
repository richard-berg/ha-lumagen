#!/usr/bin/env python3
"""Automated checks for the Lumagen integration on a live Home Assistant.

Talks to Home Assistant over its WebSocket API with a long-lived access token
(``--token-file``, default ``../ha-dev/config/.lumagen_test_token``).

Subcommands:

``snapshot``   Every Lumagen entity's state, to compare with the unit's OSD.
``monitor``    Soak monitor. Records outages (the Serial connected sensor
               leaving ``on``), how long each took to recover after its cause
               ended, the entry's state, Lumagen log lines, entity churn, and
               Home Assistant's memory, file and serial handles. Survives Home
               Assistant restarts. Every outage must have a cause: a fault from
               fault_proxy.py (``--proxy-log``), the USB device disappearing
               (``--tty``), an entry reload or restart, or an operator mark
               (``--marks``). Exits non-zero when a check fails.
``exercise``   Closed-loop commands: change a setting, wait for the device to
               confirm it, put it back. Checks that settings written together
               don't reset each other. ``--operator`` asks you to confirm what
               the screen shows.
``scenarios``  The adversarial suite, through fault_proxy.py: applies each
               fault, checks the integration goes down or stays up as it
               should, recovers in time, sends nothing unsolicited, and logs no
               errors.
``mark``       Append an operator mark (for example "pulled the power") to a
               marks file, so the monitor treats the outage as explained.

Needs Python 3.13+ and aiohttp; Home Assistant's own venv has both.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import itertools
import json
import math
import os
import random
import statistics
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiohttp

DOMAIN = "lumagen"
DEFAULT_TOKEN = Path(__file__).resolve().parents[2] / "ha-dev/config/.lumagen_test_token"
LOGGERS = ("custom_components.lumagen", "aiolumagen")
BAD_ENTRY_STATES = {"setup_error", "migration_error", "failed_unload"}
UNKNOWN = {None, "unknown", "unavailable"}

# Defaults follow the integration's 15 s poll: stale after 45 s, and a
# reconnect backoff that caps at 300 s.
DETECT_DEADLINE = 75.0
RECOVERY_DEADLINE = 360.0
UNAVAILABLE_GRACE = 90.0


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def parse_iso(text: str) -> float:
    return datetime.fromisoformat(text).timestamp()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds")


class HAError(Exception):
    """Home Assistant answered a command with an error."""


class Skip(Exception):
    """A step can't run now; not a failure."""


class HA:
    """Minimal Home Assistant WebSocket client."""

    def __init__(self, url: str, token: str) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.session: aiohttp.ClientSession | None = None
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self._id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._subs: dict[int, Callable[[Any], None]] = {}
        self._reader: asyncio.Task[None] | None = None
        self.closed = asyncio.Event()
        self.closed.set()

    async def connect(self) -> None:
        self.session = self.session or aiohttp.ClientSession()
        self.ws = await self.session.ws_connect(f"{self.url}/api/websocket", heartbeat=30)
        await self.ws.receive_json()
        await self.ws.send_json({"type": "auth", "access_token": self.token})
        reply = await self.ws.receive_json()
        if reply.get("type") != "auth_ok":
            raise SystemExit(f"Home Assistant refused the token: {reply}")
        self.closed.clear()
        self._reader = asyncio.create_task(self._read())

    async def close(self) -> None:
        if self.ws is not None:
            await self.ws.close()
        if self._reader is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader
        if self.session is not None:
            await self.session.close()

    async def _read(self) -> None:
        assert self.ws is not None
        try:
            async for msg in self.ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                data = json.loads(msg.data)
                for item in data if isinstance(data, list) else [data]:
                    if item.get("type") == "event" and item["id"] in self._subs:
                        self._subs[item["id"]](item["event"])
                    elif item.get("type") == "result":
                        fut = self._pending.pop(item["id"], None)
                        if fut and not fut.done():
                            if item.get("success"):
                                fut.set_result(item.get("result"))
                            else:
                                fut.set_exception(HAError(item.get("error")))
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("Home Assistant connection closed"))
            self._pending.clear()
            self._subs.clear()
            self.closed.set()

    async def call(self, type_: str, timeout: float = 30, **payload: Any) -> Any:
        if self.ws is None or self.closed.is_set():
            raise ConnectionError("not connected to Home Assistant")
        self._id += 1
        msg_id = self._id
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        await self.ws.send_json({"id": msg_id, "type": type_, **payload})
        return await asyncio.wait_for(fut, timeout)

    async def subscribe(self, callback: Callable[[Any], None], type_: str, **payload: Any) -> None:
        self._id += 1
        msg_id = self._id
        self._subs[msg_id] = callback
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        assert self.ws is not None
        await self.ws.send_json({"id": msg_id, "type": type_, **payload})
        await asyncio.wait_for(fut, 30)

    async def service(
        self, domain: str, service: str, data: dict[str, Any] | None = None, **target: Any
    ) -> Any:
        payload: dict[str, Any] = {"domain": domain, "service": service, "service_data": data or {}}
        if target:
            payload["target"] = target
        return await self.call("call_service", **payload)

    async def reload_entry(self, entry_id: str) -> None:
        assert self.session is not None
        headers = {"Authorization": f"Bearer {self.token}"}
        url = f"{self.url}/api/config/config_entries/entry/{entry_id}/reload"
        async with self.session.post(url, headers=headers) as resp:
            resp.raise_for_status()

    async def entry(self, wanted: str | None) -> dict[str, Any]:
        entries = await self.call("config_entries/get", domain=DOMAIN)
        if wanted:
            matches = [e for e in entries if wanted in (e["entry_id"], e["title"])]
        else:
            matches = [e for e in entries if not e.get("disabled_by")]
        if len(matches) != 1:
            names = [f"{e['title']} ({e['entry_id']}, {e['state']})" for e in entries]
            raise SystemExit(f"Pick one Lumagen entry with --entry; found: {names}")
        return matches[0]

    async def entities(self, entry_id: str) -> dict[str, str]:
        """Map ``<domain>.<key>`` (e.g. ``switch.game_mode``) to entity IDs."""
        found = {}
        for item in await self.call("config/entity_registry/list"):
            if item.get("config_entry_id") != entry_id or item.get("disabled_by"):
                continue
            key = item["unique_id"].split("_", 1)[1]
            found[f"{item['entity_id'].split('.')[0]}.{key}"] = item["entity_id"]
        return found

    async def states(self, entity_ids: set[str]) -> dict[str, dict[str, Any]]:
        return {
            s["entity_id"]: s for s in await self.call("get_states") if s["entity_id"] in entity_ids
        }

    async def lumagen_logs(self) -> dict[tuple[Any, ...], dict[str, Any]]:
        """System log entries from Lumagen code, keyed so counts can be diffed."""
        out = {}
        for item in await self.call("system_log/list"):
            text = " ".join(item.get("message") or [])
            if item.get("name", "").startswith(LOGGERS) or DOMAIN in text.lower():
                key = (item.get("name"), item.get("level"), tuple(item.get("source") or ()), text)
                out[key] = item
        return out


def load_token(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError as err:
        raise SystemExit(f"No token at {path}: create a long-lived access token there") from err


class Journal:
    """JSONL output, echoed to stdout when it matters."""

    def __init__(self, path: Path | None) -> None:
        self._file = path.open("a", buffering=1, encoding="utf-8") if path else None

    def write(self, kind: str, echo: bool = True, **fields: Any) -> None:
        record = {"t": now_iso(), "kind": kind, **fields}
        if self._file:
            self._file.write(json.dumps(record, default=str) + "\n")
        if echo:
            details = " ".join(f"{k}={v}" for k, v in fields.items())
            print(f"{record['t'][11:19]} {kind:<12} {details}", flush=True)


async def proxy_ctl(address: str, command: str) -> str:
    host, _, port = address.rpartition(":")
    reader, writer = await asyncio.open_connection(host or "127.0.0.1", int(port))
    try:
        writer.write((command + "\n").encode())
        await writer.drain()
        return (await asyncio.wait_for(reader.readline(), 10)).decode().strip()
    finally:
        writer.close()


# ---------------------------------------------------------------- analysis


@dataclass
class Window:
    """A period that explains an outage: a fault, a USB removal, a reload, a mark."""

    start: float
    end: float
    label: str


@dataclass
class Outage:
    start: float
    end: float | None = None


INSTANT_FAULTS = {"close", "reset", "inject"}


def proxy_windows(path: Path, since: float) -> list[Window]:
    """The periods fault_proxy.py applied faults, from its log."""
    windows: list[Window] = []
    open_ended: dict[str, Window] = {}
    with contextlib.suppress(OSError):
        for line in path.read_text().splitlines():
            event = json.loads(line)
            if event["kind"] not in {"fault", "clear"}:
                continue
            t = parse_iso(event["t"])
            if t < since:
                continue
            name = event.get("name", "")
            if event["kind"] == "clear":
                for n in list(open_ended) if name == "all" else [name]:
                    if (window := open_ended.pop(n, None)) is not None:
                        window.end = t
                continue
            seconds = event.get("seconds")
            if name in INSTANT_FAULTS:
                windows.append(Window(t, t, f"proxy {name}"))
            elif seconds:
                windows.append(Window(t, t + float(seconds), f"proxy {name}"))
            else:
                window = Window(t, math.inf, f"proxy {name}")
                windows.append(window)
                open_ended[name] = window
    return windows


def marks_windows(path: Path, since: float) -> list[Window]:
    windows = []
    with contextlib.suppress(OSError):
        for line in path.read_text().splitlines():
            event = json.loads(line)
            t = parse_iso(event["t"])
            if t >= since:
                windows.append(Window(t, t + float(event.get("seconds", 0)), event["label"]))
    return windows


def classify_outages(
    outages: list[Outage],
    windows: list[Window],
    now: float,
    *,
    grace: float,
    recovery_deadline: float,
) -> list[dict[str, Any]]:
    """Give each outage a cause and time its recovery from the cause's end.

    A window explains an outage that starts during it or within ``grace``
    seconds after it (detection takes up to a stale timeout plus a poll).
    Recovery is measured from the end of the latest such window and must be
    within ``recovery_deadline``.
    """
    results = []
    for outage in outages:
        end = outage.end if outage.end is not None else now
        causes = [w for w in windows if w.start - 2 <= outage.start <= w.end + grace]
        cause = max(causes, key=lambda w: w.end, default=None)
        result: dict[str, Any] = {
            "start": iso(outage.start),
            "seconds": round(end - outage.start, 1),
            "ongoing": outage.end is None,
            "cause": cause.label if cause else None,
            "ok": True,
        }
        if cause is None:
            result["ok"] = False
            result["problem"] = "nothing explains this outage"
        elif cause.end != math.inf:
            recovery = end - max(cause.end, outage.start)
            result["recovery_after_cause"] = round(recovery, 1)
            if recovery > recovery_deadline:
                result["ok"] = False
                result["problem"] = f"recovered {recovery:.0f}s after its cause ended"
        results.append(result)
    return results


def growth_per_day(samples: list[tuple[float, float]], warmup: float = 3600) -> float | None:
    """Least-squares slope of ``(time, value)`` after a warm-up, per day."""
    if not samples:
        return None
    start = samples[0][0] + warmup
    points = [(t, v) for t, v in samples if t >= start]
    if len(points) < 10 or points[-1][0] - points[0][0] < 3600:
        return None
    slope = statistics.linear_regression([p[0] for p in points], [p[1] for p in points]).slope
    return slope * 86400


# ---------------------------------------------------------------- process


def find_hass_pid() -> int | None:
    """Home Assistant's PID, however it was started (script, debugger, hass)."""
    for pid in filter(str.isdigit, os.listdir("/proc")):
        if int(pid) == os.getpid():
            continue
        with contextlib.suppress(OSError):
            args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            pairs = itertools.pairwise(args)
            if any(a == b"-m" and b == b"homeassistant" for a, b in pairs) or any(
                a.endswith(b"/hass") for a in args[:2]
            ):
                return int(pid)
    return None


def process_sample(pid: int) -> dict[str, Any] | None:
    try:
        status = dict(
            line.split(":", 1) for line in Path(f"/proc/{pid}/status").read_text().splitlines()
        )
        fds = os.listdir(f"/proc/{pid}/fd")
        rss = int(status["VmRSS"].split()[0])
        threads = int(status["Threads"])
    except OSError, KeyError, ValueError:
        return None
    ttys = 0
    for fd in fds:
        with contextlib.suppress(OSError):
            if os.readlink(f"/proc/{pid}/fd/{fd}").startswith("/dev/tty"):
                ttys += 1
    return {"rss_mb": round(rss / 1024, 1), "threads": threads, "fds": len(fds), "ttys": ttys}


# ---------------------------------------------------------------- monitor


@dataclass
class Monitor:
    args: argparse.Namespace
    journal: Journal
    started: float = field(default_factory=time.time)
    entry_id: str = ""
    entity_ids: set[str] = field(default_factory=set)
    health: str = ""
    entity_count: int = 0
    outages: list[Outage] = field(default_factory=list)
    windows: list[Window] = field(default_factory=list)
    ha_down: list[Outage] = field(default_factory=list)
    entry_window: Window | None = None
    usb_window: Window | None = None
    changes: Counter[str] = field(default_factory=Counter)
    unavailable_since: dict[str, float] = field(default_factory=dict)
    unavailable_long: Counter[str] = field(default_factory=Counter)
    entry_states: Counter[str] = field(default_factory=Counter)
    bad_entry_states: list[str] = field(default_factory=list)
    log_counts: dict[tuple[Any, ...], int] = field(default_factory=dict)
    new_logs: Counter[str] = field(default_factory=Counter)
    process: list[dict[str, Any]] = field(default_factory=list)
    problems_seen: list[str] = field(default_factory=list)

    def link_down(self, t: float, why: str) -> None:
        if not self.outages or self.outages[-1].end is not None:
            self.outages.append(Outage(t))
            self.unavailable_since.clear()
            self.journal.write("outage", why=why)

    def link_up(self, t: float) -> None:
        if self.outages and self.outages[-1].end is None:
            self.outages[-1].end = t
            self.journal.write("recovered", seconds=round(t - self.outages[-1].start, 1))

    def on_state(self, event: dict[str, Any]) -> None:
        data = event["data"]
        entity_id = data["entity_id"]
        if entity_id not in self.entity_ids:
            return
        new, old = data.get("new_state"), data.get("old_state")
        state = new["state"] if new else None
        if not old or not new or old["state"] != new["state"]:
            self.changes[entity_id] += 1
        t = time.time()
        if entity_id == self.health:
            if state == "on":
                self.link_up(t)
            else:
                self.link_down(t, f"serial connected {state}")
            return
        if entity_id.startswith("update."):
            return
        linked = not self.outages or self.outages[-1].end is not None
        if state == "unavailable" and linked and self.entry_window is None:
            self.unavailable_since.setdefault(entity_id, t)
        elif state != "unavailable" and entity_id in self.unavailable_since:
            since = self.unavailable_since.pop(entity_id)
            if t - since > UNAVAILABLE_GRACE:
                self.unavailable_long[entity_id] += 1

    def on_entries(self, changes: list[dict[str, Any]]) -> None:
        for change in changes:
            entry = change.get("entry") or {}
            if entry.get("entry_id") != self.entry_id:
                continue
            state = "removed" if change.get("type") == "removed" else entry.get("state")
            self.entry_states[state] += 1
            t = time.time()
            if state in BAD_ENTRY_STATES:
                self.bad_entry_states.append(f"{iso(t)} {state}")
                self.journal.write("entry_state", state=state, reason=entry.get("reason"))
            if state != "loaded" and self.entry_window is None:
                self.entry_window = Window(t, math.inf, f"entry {state}")
                self.windows.append(self.entry_window)
                self.journal.write("entry_state", state=state, echo=state != "loaded")
            elif state == "loaded" and self.entry_window is not None:
                self.entry_window.end = t
                self.entry_window = None
                self.journal.write("entry_state", state=state)

    async def attach(self, ha: HA) -> None:
        await ha.connect()
        entry = await ha.entry(self.args.entry)
        self.entry_id = entry["entry_id"]
        ents = await ha.entities(self.entry_id)
        self.health = ents.get("binary_sensor.serial_connected", "")
        if not self.health:
            raise SystemExit("Enable the Serial connected sensor; the monitor relies on it")
        if self.entity_count and len(ents) != self.entity_count:
            self.problems_seen.append(f"entity count changed {self.entity_count} -> {len(ents)}")
        self.entity_count = len(ents)
        self.entity_ids = set(ents.values())
        await ha.subscribe(self.on_state, "subscribe_events", event_type="state_changed")
        await ha.subscribe(self.on_entries, "config_entries/subscribe")
        health = (await ha.states({self.health})).get(self.health, {}).get("state")
        if health == "on":
            self.link_up(time.time())
        else:
            self.link_down(time.time(), f"serial connected {health} at attach")
        logs = await ha.lumagen_logs()
        if not self.log_counts:
            self.log_counts = {k: v.get("count", 1) for k, v in logs.items()}
        self.journal.write("attached", entry=entry["title"], entities=len(ents), health=health)

    async def poll(self, ha: HA, pid: int | None) -> int | None:
        with contextlib.suppress(ConnectionError, HAError, TimeoutError):
            for key, item in (await ha.lumagen_logs()).items():
                count = item.get("count", 1)
                delta = count - self.log_counts.get(key, 0)
                if delta > 0:
                    level = item.get("level")
                    self.new_logs[f"{level} {key[0]}: {key[3][:160]}"] += delta
                    self.journal.write(
                        "log", level=level, logger=key[0], count=delta, message=key[3][:300]
                    )
                self.log_counts[key] = count
        pid = pid if pid and Path(f"/proc/{pid}").exists() else find_hass_pid()
        if pid and (sample := process_sample(pid)):
            sample["t"] = time.time()
            self.process.append(sample)
            self.journal.write("process", echo=False, **sample)
        return pid

    async def watch_tty(self) -> None:
        """Sample the USB serial device every second; removals explain outages."""
        path = Path(f"/sys/class/tty/{self.args.tty}")
        present = path.exists()
        while True:
            await asyncio.sleep(1)
            now_present = path.exists()
            if now_present == present:
                continue
            present = now_present
            t = time.time()
            self.journal.write("usb", device=self.args.tty, present=present)
            if not present:
                self.usb_window = Window(t, math.inf, f"usb {self.args.tty} removed")
                self.windows.append(self.usb_window)
            elif self.usb_window is not None:
                self.usb_window.end = t
                self.usb_window = None

    def report(self) -> dict[str, Any]:
        args = self.args
        now = time.time()
        windows = list(self.windows)
        if args.proxy_log:
            windows += proxy_windows(args.proxy_log, self.started)
        if args.marks:
            windows += marks_windows(args.marks, self.started)
        windows += [Window(o.start, o.end or now, "Home Assistant down") for o in self.ha_down]
        outages = classify_outages(
            self.outages,
            windows,
            now,
            grace=args.detect_deadline,
            recovery_deadline=args.recovery_deadline,
        )
        elapsed = max(1.0, now - self.started)
        hours = elapsed / 3600
        down = sum((o.end or now) - o.start for o in self.outages)
        churn = {
            e: round(c / hours)
            for e, c in self.changes.items()
            if hours >= 1 and c / hours > args.max_changes_per_hour
        }
        long_unavailable = Counter(self.unavailable_long)
        for entity_id, since in self.unavailable_since.items():
            if now - since > UNAVAILABLE_GRACE:
                long_unavailable[entity_id] += 1
        errors = sum(c for k, c in self.new_logs.items() if k.split()[0] in {"ERROR", "CRITICAL"})
        warnings = sum(c for k, c in self.new_logs.items() if k.split()[0] == "WARNING")
        rss = growth_per_day([(p["t"], p["rss_mb"]) for p in self.process])
        settled = self.process[len(self.process) // 10 :]
        fds = [p["fds"] for p in settled]
        max_ttys = max((p["ttys"] for p in self.process), default=0)
        problems = list(self.problems_seen)
        if bad := [o for o in outages if not o["ok"]]:
            problems.append(f"{len(bad)} outage(s) unexplained or slow to recover")
        if self.bad_entry_states:
            problems.append(f"entry states: {self.bad_entry_states[-3:]}")
        if errors > args.max_errors:
            problems.append(f"{errors} error log line(s)")
        if hours >= 1 and warnings / hours > args.max_warnings_per_hour:
            problems.append(f"{warnings / hours:.1f} warnings/hour")
        if churn:
            problems.append(f"entities changing too often (per hour): {churn}")
        if long_unavailable:
            problems.append(f"unavailable while connected: {dict(long_unavailable)}")
        if rss is not None and rss > args.max_rss_growth:
            problems.append(f"memory grows {rss:.0f} MB/day")
        if fds and max(fds) - min(fds) > args.max_fd_spread:
            problems.append(f"open files ranged {min(fds)}-{max(fds)}")
        if max_ttys > 1:
            problems.append(f"Home Assistant held {max_ttys} serial handles at once")
        return {
            "ok": not problems,
            "problems": problems,
            "hours": round(hours, 2),
            "availability_pct": round(100 * (1 - down / elapsed), 3),
            "outage_count": len(outages),
            "outages": outages,
            "ha_restarts": len(self.ha_down),
            "entry_states": dict(self.entry_states),
            "errors": errors,
            "warnings": warnings,
            "top_logs": self.new_logs.most_common(5),
            "rss_mb": self.process[-1]["rss_mb"] if self.process else None,
            "rss_growth_mb_per_day": None if rss is None else round(rss, 1),
            "open_files": (min(fds), max(fds)) if fds else None,
            "threads": self.process[-1]["threads"] if self.process else None,
        }


async def monitor(args: argparse.Namespace) -> int:
    journal = Journal(args.out)
    mon = Monitor(args, journal)
    ha = HA(args.url, load_token(args.token_file))
    stop_at = time.monotonic() + args.duration if args.duration else math.inf
    pid = args.hass_pid or find_hass_pid()
    tty_task = asyncio.create_task(mon.watch_tty()) if args.tty else None
    last_report = time.monotonic()
    journal.write("start", url=args.url, hass_pid=pid, tty=args.tty)
    try:
        while time.monotonic() < stop_at:
            if ha.closed.is_set():
                try:
                    await mon.attach(ha)
                except (
                    OSError,
                    aiohttp.ClientError,
                    ConnectionError,
                    HAError,
                    TimeoutError,
                ) as err:
                    if not mon.ha_down or mon.ha_down[-1].end is not None:
                        mon.ha_down.append(Outage(time.time()))
                        journal.write("ha_down", error=repr(err))
                    await asyncio.sleep(5)
                    continue
                if mon.ha_down and mon.ha_down[-1].end is None:
                    mon.ha_down[-1].end = time.time()
                    journal.write("ha_back", seconds=round(time.time() - mon.ha_down[-1].start))
            pid = await mon.poll(ha, pid)
            if time.monotonic() - last_report >= args.report_every:
                last_report = time.monotonic()
                report = mon.report()
                journal.write("report", **{k: v for k, v in report.items() if k != "outages"})
            await asyncio.sleep(min(args.interval, max(0.0, stop_at - time.monotonic())))
    except asyncio.CancelledError:
        pass
    finally:
        if tty_task:
            tty_task.cancel()
        report = mon.report()
        journal.write("final", **report)
        await ha.close()
    args.exit_code = 0 if report["ok"] else 1
    return args.exit_code


# ---------------------------------------------------------------- exercise


@dataclass
class Step:
    """One closed-loop change: set it, confirm it, put it back."""

    name: str
    impact: str  # none, subtle, sound, picture, power
    run: Callable[[Exerciser], Awaitable[float]]
    watch: str = ""


class Exerciser:
    def __init__(self, ha: HA, ents: dict[str, str], args: argparse.Namespace) -> None:
        self.ha = ha
        self.ents = ents
        self.args = args
        self.states: dict[str, dict[str, Any]] = {}
        self.changed = asyncio.Event()

    def on_state(self, event: dict[str, Any]) -> None:
        new = event["data"].get("new_state")
        if new and new["entity_id"] in self.ents.values():
            self.states[new["entity_id"]] = new
            self.changed.set()

    def state(self, key: str) -> str | None:
        return self.states.get(self.ents[key], {}).get("state")

    def attr(self, key: str, name: str) -> Any:
        return self.states.get(self.ents[key], {}).get("attributes", {}).get(name)

    async def announce(self, label: str) -> None:
        if self.args.proxy_control:
            await proxy_ctl(self.args.proxy_control, f"expect-write 15 exerciser {label}")

    async def wait_for(self, what: str, predicate: Callable[[], bool], timeout: float) -> float:
        start = time.monotonic()
        while not predicate():
            remaining = timeout - (time.monotonic() - start)
            if remaining <= 0:
                raise AssertionError(f"no {what} within {timeout:.0f}s")
            self.changed.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.changed.wait(), remaining)
        return time.monotonic() - start

    async def set(self, key: str, value: Any) -> float:
        """Set ``key`` and wait until its state reads ``value``."""
        domain = key.split(".")[0]
        entity_id = self.ents[key]
        await self.announce(f"{key}={value}")
        if domain == "switch":
            await self.ha.service("switch", f"turn_{value}", entity_id=entity_id)
        elif domain == "select":
            await self.ha.service("select", "select_option", {"option": value}, entity_id=entity_id)
        elif domain == "number":
            await self.ha.service("number", "set_value", {"value": value}, entity_id=entity_id)

        def reads_value() -> bool:
            current = self.state(key)
            if domain == "number":
                return current not in UNKNOWN and float(current) == float(value)
            return current == value

        return await self.wait_for(f"{key} reading {value}", reads_value, self.args.confirm_timeout)

    async def press(self, key: str) -> float:
        await self.announce(key)
        start = time.monotonic()
        await self.ha.service("button", "press", entity_id=self.ents[key])
        return time.monotonic() - start


async def _change_and_restore(
    ex: Exerciser, change: str, new_value: Callable[[str], Any], keep: list[str]
) -> float:
    """Change one setting; the ``keep`` settings must not move. Then restore it."""
    keys = [change, *keep]
    before = {k: ex.state(k) for k in keys}
    if unknown := [k for k, v in before.items() if v in UNKNOWN]:
        raise Skip(f"{unknown} unknown, so it couldn't be restored")
    latency = await ex.set(change, new_value(before[change]))
    await asyncio.sleep(2)
    moved = {k: (before[k], ex.state(k)) for k in keep if ex.state(k) != before[k]}
    original = float(before[change]) if change.startswith("number.") else before[change]
    await ex.set(change, original)
    if moved:
        raise AssertionError(f"changing {change} also changed {moved}")
    return latency


def _other_option(ex: Exerciser, key: str) -> str:
    options = [o for o in ex.attr(key, "options") or [] if o != ex.state(key)]
    if not options:
        raise Skip(f"{key} has no other option")
    return random.choice(options)


def _neighbour(ex: Exerciser, key: str, current: str) -> float:
    high = ex.attr(key, "max")
    step = ex.attr(key, "step") or 1
    value = float(current)
    return value + step if value + step <= high else value - step


SHARPNESS = ["switch.sharpness_enabled", "number.sharpness_level", "select.sharpness_sensitivity"]
HDR_MAPPING = ["select.hdr_gamma_mode", "number.hdr_mapping_max_nits"]


def _others(group: list[str], key: str) -> list[str]:
    return [k for k in group if k != key]


async def _raw_query(ex: Exerciser) -> float:
    start = time.monotonic()
    await ex.ha.service(
        DOMAIN, "send_raw_command", {"command": "ZQS01"}, entity_id=ex.ents["binary_sensor.power"]
    )
    return time.monotonic() - start


async def _power_cycle(ex: Exerciser) -> float:
    if ex.state("binary_sensor.power") != "on":
        raise Skip("the Lumagen is in standby")
    await ex.press("button.standby")
    latency = await ex.wait_for("standby", lambda: ex.state("binary_sensor.power") == "off", 30)
    await ex.press("button.power_on")
    await ex.wait_for("power on", lambda: ex.state("binary_sensor.power") == "on", 60)
    return latency


def steps() -> list[Step]:
    on_off = {"on": "off", "off": "on"}
    return [
        Step("query status", "none", lambda ex: ex.press("button.query_status")),
        Step("raw ZQS01", "none", _raw_query),
        Step(
            "game mode",
            "subtle",
            lambda ex: _change_and_restore(ex, "switch.game_mode", on_off.get, []),
            "no visible change; processing latency drops, then returns",
        ),
        Step(
            "sharpness on/off keeps level and sensitivity",
            "subtle",
            lambda ex: _change_and_restore(
                ex,
                "switch.sharpness_enabled",
                on_off.get,
                _others(SHARPNESS, "switch.sharpness_enabled"),
            ),
            "edges soften or sharpen, then return",
        ),
        Step(
            "sharpness level keeps on/off and sensitivity",
            "subtle",
            lambda ex: _change_and_restore(
                ex,
                "number.sharpness_level",
                lambda v: _neighbour(ex, "number.sharpness_level", v),
                _others(SHARPNESS, "number.sharpness_level"),
            ),
        ),
        Step(
            "sharpness sensitivity keeps on/off and level",
            "subtle",
            lambda ex: _change_and_restore(
                ex,
                "select.sharpness_sensitivity",
                {"normal": "high", "high": "normal"}.get,
                _others(SHARPNESS, "select.sharpness_sensitivity"),
            ),
        ),
        Step(
            "HDR gamma mode keeps max nits",
            "subtle",
            lambda ex: _change_and_restore(
                ex,
                "select.hdr_gamma_mode",
                lambda v: _other_option(ex, "select.hdr_gamma_mode"),
                _others(HDR_MAPPING, "select.hdr_gamma_mode"),
            ),
            "HDR content only: tone mapping changes, then returns",
        ),
        Step(
            "HDR max nits keeps gamma mode",
            "subtle",
            lambda ex: _change_and_restore(
                ex,
                "number.hdr_mapping_max_nits",
                lambda v: 1000.0 if float(v) != 1000.0 else 2000.0,
                _others(HDR_MAPPING, "number.hdr_mapping_max_nits"),
            ),
        ),
        Step(
            "minimum fan speed",
            "sound",
            lambda ex: _change_and_restore(
                ex, "number.fan_speed", lambda v: _neighbour(ex, "number.fan_speed", v), []
            ),
            "fan noise changes, then returns",
        ),
        Step(
            "subtitle shift",
            "picture",
            lambda ex: _change_and_restore(
                ex,
                "select.subtitle_shift",
                lambda v: _other_option(ex, "select.subtitle_shift"),
                [],
            ),
            "the picture shifts up, then back",
        ),
        Step(
            "memory",
            "picture",
            lambda ex: _change_and_restore(
                ex,
                "select.memory_select",
                lambda v: _other_option(ex, "select.memory_select"),
                ["sensor.current_input"],
            ),
            "the OSD shows the memory change, then back",
        ),
        Step(
            "aspect",
            "picture",
            lambda ex: _change_and_restore(
                ex,
                "select.aspect_select",
                lambda v: _other_option(ex, "select.aspect_select"),
                ["sensor.current_input"],
            ),
            "the aspect changes, then back",
        ),
        Step(
            "standby and power on",
            "power",
            _power_cycle,
            "the Lumagen goes to standby, then powers back on",
        ),
    ]


async def exercise(args: argparse.Namespace) -> int:
    journal = Journal(args.out)
    ha = HA(args.url, load_token(args.token_file))
    await ha.connect()
    entry = await ha.entry(args.entry)
    ents = await ha.entities(entry["entry_id"])
    ex = Exerciser(ha, ents, args)
    await ha.subscribe(ex.on_state, "subscribe_events", event_type="state_changed")
    ex.states = await ha.states(set(ents.values()))
    allowed = {"none", "subtle", *args.allow}
    chosen = [s for s in steps() if s.impact in allowed]
    latencies: dict[str, list[float]] = defaultdict(list)
    failures: list[str] = []
    skipped: Counter[str] = Counter()
    rng = random.Random(args.seed)
    journal.write("start", steps=[s.name for s in chosen], rounds=args.rounds)
    try:
        for round_no in range(1, args.rounds + 1):
            order = chosen[:]
            rng.shuffle(order)
            for step in order:
                if args.operator and step.watch:
                    print(f"\n>>> {step.name}. Watch for: {step.watch}", flush=True)
                try:
                    latency = await step.run(ex)
                except (Skip, KeyError) as err:
                    skipped[f"{step.name}: {err}"] += 1
                    continue
                except (AssertionError, HAError, TimeoutError, ConnectionError) as err:
                    failures.append(f"round {round_no} {step.name}: {err}")
                    journal.write("fail", step=step.name, error=str(err))
                    continue
                latencies[step.name].append(latency)
                journal.write("ok", step=step.name, seconds=round(latency, 2), echo=args.verbose)
                if args.operator and step.watch:
                    answer = await asyncio.to_thread(input, "    Did the screen do that? [y/n] ")
                    journal.write("operator", step=step.name, answer=answer.strip())
                    if answer.strip().lower().startswith("n"):
                        failures.append(f"round {round_no} {step.name}: operator saw otherwise")
                await asyncio.sleep(args.pause)
            if round_no < args.rounds:
                await asyncio.sleep(args.interval)
    except asyncio.CancelledError:
        pass
    unsolicited = None
    if args.proxy_control:
        counters = json.loads(await proxy_ctl(args.proxy_control, "status"))["counters"]
        unsolicited = counters["writes_unsolicited"]
        if unsolicited:
            failures.append(f"the proxy saw {unsolicited} unsolicited write(s)")
    summary = {
        name: {"n": len(v), "median_s": round(statistics.median(v), 2), "max_s": round(max(v), 2)}
        for name, v in latencies.items()
    }
    journal.write(
        "final",
        ok=not failures,
        failures=failures,
        skipped=dict(skipped),
        seconds_to_confirm=summary,
        unsolicited_writes=unsolicited,
    )
    await ha.close()
    args.exit_code = 0 if not failures else 1
    return args.exit_code


# ---------------------------------------------------------------- scenarios


@dataclass
class Scenario:
    name: str
    faults: list[str]
    hold: float
    expect: str  # "up": stays connected; "down": must drop; "either"
    long: bool = False
    repeat: int = 1
    gap: float = 0.0
    identity_unchanged: bool = False
    ha_action: str | None = None


MALFORMED = [
    "empty_s01",
    "short_i25",
    "garbage_i25",
    "unknown_code",
    "partial",
    "lone_cr",
    "nul",
    "binary",
    "overlong",
    "no_bang",
    "bad_power",
    "bad_firmware",
]

SCENARIOS = [
    Scenario("latency 1s", ["latency-down 1000", "latency-up 1000"], 60, "up"),
    Scenario("latency 4s", ["latency-down 4000"], 60, "up"),
    Scenario("jitter", ["latency-down 200", "jitter 800"], 60, "up"),
    Scenario("rare bit flips", ["corrupt 0.001"], 120, "up", identity_unchanged=True),
    Scenario("heavy bit flips", ["corrupt 0.05"], 60, "either"),
    Scenario("line noise", ["noise 0.3"], 60, "up", identity_unchanged=True),
    Scenario("duplicate lines", ["dup 0.5"], 60, "up", identity_unchanged=True),
    Scenario("truncated lines", ["truncate 0.2"], 60, "up"),
    Scenario("byte at a time", ["split 5"], 60, "up", identity_unchanged=True),
    Scenario("command echo", ["echo on"], 60, "up", identity_unchanged=True),
    Scenario("LF line endings", ["eol lf"], 60, "either"),
    Scenario(
        "malformed lines", [f"inject {n}" for n in MALFORMED], 20, "up", identity_unchanged=True
    ),
    Scenario("short stall", ["stall on"], 30, "up"),
    Scenario("long stall", ["stall on"], 90, "down"),
    Scenario("device silent", ["drop-down on"], 120, "down"),
    Scenario("commands lost", ["drop-up on"], 120, "down"),
    Scenario("connection closed", ["close"], 0, "either"),
    Scenario("connection reset", ["reset"], 0, "either"),
    Scenario("connections refused 2 min", ["refuse 120"], 120, "down"),
    Scenario("device port gone 1 min", ["upstream-off 60"], 60, "down"),
    Scenario("flapping link", ["close"], 0, "either", repeat=12, gap=5),
    Scenario("reload during outage", ["refuse 180"], 180, "down", ha_action="reload"),
    Scenario("restart during outage", ["refuse 240"], 240, "down", ha_action="restart"),
    Scenario("reload storm", [], 0, "either", ha_action="reload_storm"),
    Scenario("long outage 20 min", ["refuse 1200"], 1200, "down", long=True),
]


class Health:
    """Tracks the Serial connected sensor and the identity sensors."""

    def __init__(self, ha: HA, ents: dict[str, str]) -> None:
        self.ha = ha
        self.health = ents["binary_sensor.serial_connected"]
        self.identity = [ents[k] for k in ("sensor.model", "sensor.firmware") if k in ents]
        self.current: dict[str, str] = {}
        self.changed = asyncio.Event()

    def on_state(self, event: dict[str, Any]) -> None:
        new = event["data"].get("new_state")
        if new and new["entity_id"] in (self.health, *self.identity):
            self.current[new["entity_id"]] = new["state"]
            self.changed.set()

    async def attach(self) -> None:
        await self.ha.subscribe(self.on_state, "subscribe_events", event_type="state_changed")
        for entity_id, s in (await self.ha.states({self.health, *self.identity})).items():
            self.current[entity_id] = s["state"]

    async def reconnect(self) -> None:
        for _ in range(120):
            try:
                await self.ha.connect()
                await self.attach()
                return
            except OSError, aiohttp.ClientError, ConnectionError, HAError:
                await asyncio.sleep(5)
        raise ConnectionError("Home Assistant did not come back")

    @property
    def up(self) -> bool:
        return self.current.get(self.health) == "on"

    async def wait(self, want_up: bool, timeout: float) -> float | None:
        start = time.monotonic()
        while self.up != want_up:
            if time.monotonic() - start > timeout:
                return None
            if self.ha.closed.is_set():
                await self.reconnect()
                continue
            self.changed.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.changed.wait(), 2)
        return time.monotonic() - start

    async def stays_up(self, seconds: float) -> bool:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if not self.up:
                return False
            self.changed.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.changed.wait(), max(0.1, end - time.monotonic()))
        return self.up

    def identity_values(self) -> dict[str, str | None]:
        return {e: self.current.get(e) for e in self.identity}


async def _ha_action(ha: HA, health: Health, action: str, entry_id: str, journal: Journal) -> None:
    if action == "reload_storm":
        journal.write("ha_action", action="reload x5")
        for _ in range(5):
            with contextlib.suppress(aiohttp.ClientResponseError):
                await ha.reload_entry(entry_id)
            await asyncio.sleep(0.5)
        return
    await health.wait(False, DETECT_DEADLINE)
    if action == "reload":
        journal.write("ha_action", action="reload")
        await ha.reload_entry(entry_id)
    elif action == "restart":
        journal.write("ha_action", action="restart")
        with contextlib.suppress(HAError, ConnectionError, TimeoutError):
            await ha.service("homeassistant", "restart")
        await ha.closed.wait()
        await asyncio.sleep(5)
        await health.reconnect()
    state = (await ha.entry(entry_id))["state"]
    journal.write("ha_action", entry_state=state)
    if state in BAD_ENTRY_STATES:
        raise AssertionError(f"entry {state} after {action} during an outage")


async def run_scenario(
    sc: Scenario,
    ha: HA,
    health: Health,
    entry_id: str,
    ents: dict[str, str],
    args: argparse.Namespace,
    journal: Journal,
) -> dict[str, Any]:
    problems: list[str] = []
    ctl = args.proxy_control
    if await health.wait(True, args.recovery_deadline) is None:
        problems.append("not connected before the scenario")
    counters_before = json.loads(await proxy_ctl(ctl, "status"))["counters"]
    logs_before = {k: v.get("count", 1) for k, v in (await ha.lumagen_logs()).items()}
    identity_before = health.identity_values()
    await proxy_ctl(ctl, f"mark scenario {sc.name}")
    fault_start = time.monotonic()
    for i in range(sc.repeat):
        for command in sc.faults:
            if (reply := await proxy_ctl(ctl, command)) != "ok":
                problems.append(f"proxy refused {command!r}: {reply}")
        if sc.gap and i + 1 < sc.repeat:
            await asyncio.sleep(sc.gap)
    went_down = False
    if sc.ha_action:
        try:
            await _ha_action(ha, health, sc.ha_action, entry_id, journal)
        except AssertionError as err:
            problems.append(str(err))
    hold_left = sc.hold - (time.monotonic() - fault_start)
    if sc.expect == "up":
        if not await health.stays_up(max(0.0, hold_left)):
            problems.append("went unavailable")
    else:
        limit = max(0.0, hold_left) + (args.detect_deadline if sc.expect == "down" else 3)
        went_down = (not health.up) or await health.wait(False, limit) is not None
        if sc.expect == "down" and not went_down:
            problems.append(f"still connected {limit:.0f}s into the fault")
        remaining = sc.hold - (time.monotonic() - fault_start)
        if remaining > 0:
            await asyncio.sleep(remaining)
    await proxy_ctl(ctl, "clear")
    recovery = await health.wait(True, args.recovery_deadline)
    if recovery is None:
        problems.append(f"not connected {args.recovery_deadline:.0f}s after the fault cleared")
    else:
        try:
            await ha.service("button", "press", entity_id=ents["button.query_status"])
            await ha.service(
                DOMAIN, "send_raw_command", {"command": "ZQS01"}, entity_id=health.health
            )
        except (HAError, ConnectionError) as err:
            problems.append(f"commands fail after recovery: {err}")
    await asyncio.sleep(5)
    counters = json.loads(await proxy_ctl(ctl, "status"))["counters"]
    if n := counters["writes_unsolicited"] - counters_before["writes_unsolicited"]:
        problems.append(f"{n} unsolicited write(s): {counters['unsolicited'][-n:]}")
    if n := counters["concurrent_refused"] - counters_before["concurrent_refused"]:
        problems.append(f"tried {n} second connection(s)")
    levels: Counter[str] = Counter()
    for key, item in (await ha.lumagen_logs()).items():
        if (delta := item.get("count", 1) - logs_before.get(key, 0)) > 0:
            levels[item.get("level")] += delta
            if item.get("level") in {"ERROR", "CRITICAL"}:
                problems.append(f"logged {item.get('level')}: {key[3][:200]}")
    if levels["WARNING"] > args.max_warnings * sc.repeat:
        problems.append(f"{levels['WARNING']} warnings")
    if sc.identity_unchanged and (after := health.identity_values()) != identity_before:
        problems.append(f"identity changed: {identity_before} -> {after}")
    return {
        "scenario": sc.name,
        "ok": not problems,
        "went_down": went_down,
        "recovery_s": None if recovery is None else round(recovery, 1),
        "warnings": levels["WARNING"],
        "problems": problems,
    }


async def scenarios(args: argparse.Namespace) -> int:
    journal = Journal(args.out)
    ha = HA(args.url, load_token(args.token_file))
    await ha.connect()
    entry = await ha.entry(args.entry)
    ents = await ha.entities(entry["entry_id"])
    health = Health(ha, ents)
    await health.attach()
    chosen = [s for s in SCENARIOS if args.long or not s.long]
    if args.only:
        chosen = [s for s in SCENARIOS if any(o.lower() in s.name.lower() for o in args.only)]
    results = []
    try:
        for sc in chosen:
            journal.write("scenario", name=sc.name)
            result = await run_scenario(sc, ha, health, entry["entry_id"], ents, args, journal)
            results.append(result)
            journal.write("result", **result)
    except asyncio.CancelledError:
        await proxy_ctl(args.proxy_control, "clear")
    passed = sum(r["ok"] for r in results)
    print(f"\n{passed}/{len(results)} scenarios passed")
    for r in results:
        verdict = "PASS" if r["ok"] else "FAIL"
        details = "; ".join(r["problems"])
        print(f"  {verdict}  {r['scenario']:<28} recovery {r['recovery_s']}s  {details}")
    journal.write("final", passed=passed, total=len(results), echo=False)
    await ha.close()
    args.exit_code = 0 if passed == len(results) else 1
    return args.exit_code


# ---------------------------------------------------------------- snapshot / mark


async def snapshot(args: argparse.Namespace) -> int:
    ha = HA(args.url, load_token(args.token_file))
    await ha.connect()
    entry = await ha.entry(args.entry)
    ents = await ha.entities(entry["entry_id"])
    states = await ha.states(set(ents.values()))
    print(f"{entry['title']} ({entry['state']})")
    for key in sorted(ents):
        s = states.get(ents[key], {})
        print(f"  {key:<42} {s.get('state', '-')!s:<22} {ents[key]}")
    await ha.close()
    return 0


def mark(args: argparse.Namespace) -> int:
    record = {"t": now_iso(), "label": " ".join(args.label), "seconds": args.seconds}
    with args.marks.open("a", encoding="utf-8") as file:
        file.write(json.dumps(record) + "\n")
    print(f"marked {record}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", default="http://127.0.0.1:8123")
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN)
    parser.add_argument("--entry", help="config entry ID or title, if more than one is enabled")
    parser.add_argument("--out", type=Path, help="JSONL journal")
    parser.add_argument("--detect-deadline", type=float, default=DETECT_DEADLINE)
    parser.add_argument("--recovery-deadline", type=float, default=RECOVERY_DEADLINE)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("snapshot")

    p = sub.add_parser("monitor")
    p.add_argument("--duration", type=float, help="seconds; default: until interrupted")
    p.add_argument("--interval", type=float, default=30)
    p.add_argument("--report-every", type=float, default=3600)
    p.add_argument("--tty", help="serial device to watch for removal, e.g. ttyUSB0")
    p.add_argument("--proxy-log", type=Path, help="fault_proxy.py --log file")
    p.add_argument("--marks", type=Path, help="operator marks file")
    p.add_argument("--hass-pid", type=int)
    p.add_argument("--max-errors", type=int, default=0)
    p.add_argument("--max-warnings-per-hour", type=float, default=12)
    p.add_argument("--max-changes-per-hour", type=float, default=720)
    p.add_argument("--max-rss-growth", type=float, default=20, help="MB/day")
    p.add_argument("--max-fd-spread", type=int, default=10)

    p = sub.add_parser("exercise")
    p.add_argument("--rounds", type=int, default=1)
    p.add_argument("--interval", type=float, default=600, help="seconds between rounds")
    p.add_argument("--pause", type=float, default=2, help="seconds between steps")
    p.add_argument(
        "--allow",
        nargs="*",
        default=[],
        choices=["sound", "picture", "power"],
        help="also run steps that change the sound, picture or power",
    )
    p.add_argument("--operator", action="store_true", help="confirm what the screen shows")
    p.add_argument("--confirm-timeout", type=float, default=15)
    p.add_argument("--proxy-control", help="fault_proxy.py control address, to announce writes")
    p.add_argument("--seed", type=int)
    p.add_argument("--verbose", action="store_true")

    p = sub.add_parser("scenarios")
    p.add_argument("--proxy-control", default="127.0.0.1:14998")
    p.add_argument("--only", nargs="*", help="run scenarios whose name contains any of these")
    p.add_argument("--long", action="store_true", help="include long scenarios")
    p.add_argument("--max-warnings", type=int, default=6, help="per scenario")

    p = sub.add_parser("mark")
    p.add_argument("label", nargs="+")
    p.add_argument("--seconds", type=float, default=0, help="how long the fault lasts")
    p.add_argument("--marks", type=Path, required=True)

    args = parser.parse_args()
    args.exit_code = 0
    if args.command == "mark":
        sys.exit(mark(args))
    runner = {
        "snapshot": snapshot,
        "monitor": monitor,
        "exercise": exercise,
        "scenarios": scenarios,
    }
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(runner[args.command](args))
    sys.exit(args.exit_code)


if __name__ == "__main__":
    main()

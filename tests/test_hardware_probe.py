"""Tests for the analysis in hardware/lumagen_probe.py.

The monitor decides whether a months-long soak passed. If it blames the wrong
fault for an outage, or misses a slow recovery, the result is meaningless, so
the logic that decides gets tested like product code.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

_PATH = Path(__file__).parent.parent / "hardware" / "lumagen_probe.py"
_SPEC = importlib.util.spec_from_file_location("lumagen_probe", _PATH)
assert _SPEC and _SPEC.loader
probe = importlib.util.module_from_spec(_SPEC)
sys.modules["lumagen_probe"] = probe
_SPEC.loader.exec_module(probe)

T0 = 1_800_000_000.0


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).isoformat(timespec="milliseconds")


def _write_log(path: Path, events: list[tuple[float, dict]]) -> Path:
    path.write_text("".join(json.dumps({"t": _iso(t), **e}) + "\n" for t, e in events))
    return path


def test_proxy_windows(tmp_path: Path) -> None:
    log = _write_log(
        tmp_path / "proxy.jsonl",
        [
            (T0 - 100, {"kind": "fault", "name": "drop_down", "value": True}),
            (T0 + 10, {"kind": "fault", "name": "drop_down", "value": True, "seconds": 60}),
            (T0 + 100, {"kind": "fault", "name": "corrupt", "value": 0.1}),
            (T0 + 150, {"kind": "clear", "name": "corrupt"}),
            (T0 + 200, {"kind": "fault", "name": "close"}),
            (T0 + 300, {"kind": "fault", "name": "refuse", "seconds": 120}),
            (T0 + 400, {"kind": "fault", "name": "stall", "value": True}),
            (T0 + 500, {"kind": "down", "data": "!S01\r\n"}),
        ],
    )
    windows = [(w.start - T0, w.end - T0, w.label) for w in probe.proxy_windows(log, T0)]
    assert windows == [
        (10, 70, "proxy drop_down"),
        (100, 150, "proxy corrupt"),
        (200, 200, "proxy close"),
        (300, 420, "proxy refuse"),
        (400, math.inf, "proxy stall"),
    ]
    _write_log(
        log,
        [
            (T0 + 1, {"kind": "fault", "name": "stall", "value": True}),
            (T0 + 5, {"kind": "clear", "name": "all"}),
        ],
    )
    assert [(w.start - T0, w.end - T0) for w in probe.proxy_windows(log, T0)] == [(1, 5)]


def _classify(outages, windows, now=T0 + 10_000):
    return probe.classify_outages(
        [probe.Outage(T0 + s, None if e is None else T0 + e) for s, e in outages],
        [
            probe.Window(T0 + s, T0 + e if e != math.inf else math.inf, label)
            for s, e, label in windows
        ],
        now,
        grace=75,
        recovery_deadline=360,
    )


def test_outage_during_a_fault_is_explained_and_timed_from_its_end() -> None:
    (result,) = _classify([(50, 130)], [(0, 120, "proxy drop_down")])
    assert result["ok"]
    assert result["cause"] == "proxy drop_down"
    assert result["recovery_after_cause"] == 10


def test_outage_detected_shortly_after_an_instant_fault_is_explained() -> None:
    (result,) = _classify([(40, 46)], [(0, 0, "proxy close")])
    assert result["ok"]
    assert result["cause"] == "proxy close"


def test_outage_with_no_fault_is_a_failure() -> None:
    (result,) = _classify([(500, 510)], [(0, 120, "proxy drop_down")])
    assert not result["ok"]
    assert result["cause"] is None


def test_slow_recovery_is_a_failure() -> None:
    (result,) = _classify([(50, 120 + 400)], [(0, 120, "proxy refuse")])
    assert not result["ok"]
    assert "recovered 400s" in result["problem"]


def test_ongoing_outage_counts_until_now() -> None:
    (result,) = _classify([(50, None)], [(0, 120, "proxy refuse")], now=T0 + 1000)
    assert result["ongoing"]
    assert not result["ok"]


def test_open_ended_cause_has_no_recovery_deadline() -> None:
    (result,) = _classify([(50, None)], [(0, math.inf, "usb ttyUSB0 removed")])
    assert result["ok"]
    assert "recovery_after_cause" not in result


def test_recovery_is_measured_from_the_latest_overlapping_cause() -> None:
    (result,) = _classify([(10, 200)], [(0, 5, "proxy close"), (0, 180, "Home Assistant down")])
    assert result["cause"] == "Home Assistant down"
    assert result["recovery_after_cause"] == 20


def test_growth_per_day() -> None:
    samples = [(T0 + h * 600, 100 + h * 600 / 3600) for h in range(60)]
    assert probe.growth_per_day(samples) == pytest.approx(24, rel=1e-6)
    assert probe.growth_per_day(samples[:12]) is None
    assert probe.growth_per_day([]) is None


def _monitor() -> probe.Monitor:
    args = argparse.Namespace(
        proxy_log=None,
        marks=None,
        detect_deadline=75,
        recovery_deadline=360,
        max_changes_per_hour=720,
        max_errors=0,
        max_warnings_per_hour=12,
        max_rss_growth=20,
        max_fd_spread=10,
        tty=None,
    )
    mon = probe.Monitor(args, probe.Journal(None))
    mon.entry_id = "entry"
    mon.health = "binary_sensor.lumagen_serial_connected"
    mon.entity_ids = {mon.health, "sensor.lumagen_firmware", "update.lumagen_firmware"}
    return mon


class _Clock:
    """Stands in for the probe's ``time`` module; the real one stays untouched."""

    def __init__(self, start: float) -> None:
        self.now = start

    def time(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock(T0)
    monkeypatch.setattr(probe, "time", fake)
    return fake


def _state(mon: probe.Monitor, entity_id: str, old: str, new: str) -> None:
    mon.on_state(
        {
            "data": {
                "entity_id": entity_id,
                "old_state": {"state": old},
                "new_state": {"state": new, "entity_id": entity_id},
            }
        }
    )


def test_monitor_tracks_outages_from_the_health_sensor(clock: _Clock) -> None:
    mon = _monitor()
    _state(mon, mon.health, "on", "off")
    clock.now += 30
    _state(mon, mon.health, "off", "on")
    assert [(o.start, o.end) for o in mon.outages] == [(T0, T0 + 30)]


def test_brief_unavailability_is_not_a_problem_but_long_is(clock: _Clock) -> None:
    mon = _monitor()
    _state(mon, "sensor.lumagen_firmware", "030225", "unavailable")
    clock.now += 10
    _state(mon, "sensor.lumagen_firmware", "unavailable", "030225")
    assert not mon.unavailable_long
    _state(mon, "sensor.lumagen_firmware", "030225", "unavailable")
    clock.now += 200
    _state(mon, "sensor.lumagen_firmware", "unavailable", "030225")
    assert mon.unavailable_long == {"sensor.lumagen_firmware": 1}
    _state(mon, "update.lumagen_firmware", "off", "unavailable")
    assert "update.lumagen_firmware" not in mon.unavailable_since


def test_entry_reload_explains_the_outage_it_causes(clock: _Clock) -> None:
    mon = _monitor()
    mon.on_entries([{"type": "updated", "entry": {"entry_id": "entry", "state": "not_loaded"}}])
    _state(mon, mon.health, "on", "unavailable")
    _state(mon, "sensor.lumagen_firmware", "030225", "unavailable")
    clock.now += 5
    mon.on_entries([{"type": "updated", "entry": {"entry_id": "entry", "state": "loaded"}}])
    _state(mon, mon.health, "unavailable", "on")
    report = mon.report()
    assert report["ok"], report["problems"]
    assert report["outages"][0]["cause"] == "entry not_loaded"


def test_bad_entry_state_fails_the_report(clock: _Clock) -> None:
    mon = _monitor()
    mon.on_entries([{"type": "updated", "entry": {"entry_id": "entry", "state": "setup_error"}}])
    mon.on_entries([{"type": "updated", "entry": {"entry_id": "other", "state": "setup_error"}}])
    assert len(mon.bad_entry_states) == 1
    assert not mon.report()["ok"]

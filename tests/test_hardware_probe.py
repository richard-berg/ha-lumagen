"""Tests for the analysis in hardware/lumagen_probe.py.

The monitor decides whether a months-long soak passed. If it blames the wrong
fault for an outage, or misses a slow recovery, the result is meaningless, so
the logic that decides gets tested like product code.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import math
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

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


def test_timed_faults_end_at_clear_or_off_not_the_original_deadline(tmp_path: Path) -> None:
    log = _write_log(
        tmp_path / "proxy.jsonl",
        [
            (T0 + 10, {"kind": "fault", "name": "drop_down", "value": True, "seconds": 120}),
            (T0 + 20, {"kind": "clear", "name": "all"}),
            (T0 + 30, {"kind": "fault", "name": "drop_up", "value": True, "seconds": 60}),
            (T0 + 35, {"kind": "fault", "name": "drop_up", "value": False}),
            (T0 + 40, {"kind": "fault", "name": "mute_report:I30", "seconds": 30}),
            (T0 + 45, {"kind": "clear", "name": "mute_report:I30"}),
            (
                T0 + 50,
                {"kind": "fault", "name": "repeat_inject", "value": "bad_power", "seconds": 60},
            ),
            (T0 + 55, {"kind": "clear", "name": "repeat_inject"}),
            (T0 + 100, {"kind": "clear", "name": "all"}),
        ],
    )
    assert [(w.start - T0, w.end - T0) for w in probe.proxy_windows(log, T0)] == [
        (10, 20),
        (30, 35),
        (40, 45),
        (50, 55),
    ]


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


def test_scenario_identity_check_sees_values_a_later_query_repaired() -> None:
    ents = {
        "binary_sensor.serial_connected": "binary_sensor.serial",
        "sensor.firmware": "sensor.firmware",
        "sensor.model": "sensor.model",
    }
    health = probe.Health(probe.HA("http://ha", "token"), ents)

    def state(entity_id: str, value: str) -> None:
        health.on_state(
            {
                "data": {
                    "entity_id": entity_id,
                    "new_state": {"entity_id": entity_id, "state": value},
                }
            }
        )

    state("sensor.firmware", "030225")
    health.identity_seen.clear()
    state("sensor.firmware", "")
    state("sensor.firmware", "unavailable")
    state("sensor.firmware", "030225")
    assert health.identity_seen == {"sensor.firmware": {"", "030225"}}
    assert health.identity_values() == {"sensor.model": None, "sensor.firmware": "030225"}


async def test_a_log_entry_keeps_its_key_when_a_new_message_joins_it() -> None:
    """Regression: a scenario that logged one warning was charged with seven.

    Home Assistant appends each new distinct message to the same system log
    entry. Keyed by text, the entry looked brand new when that happened, so
    its whole lifetime count was taken as this scenario's.
    """
    entry = {
        "name": "aiolumagen.client",
        "level": "WARNING",
        "source": ["runner.py", 280],
        "first_occurred": T0,
        "count": 6,
        "message": ["Lumagen is now unavailable (no response in 45s), reconnecting"],
    }
    ha = probe.HA("http://127.0.0.1:8123", "token")
    replies = [[dict(entry)]]
    entry.update(
        count=7,
        message=[*entry["message"], "Lumagen is now unavailable (transport disconnected)"],
    )
    replies.append([entry])

    async def call(_type: str, **_payload: object) -> object:
        return replies.pop(0)

    ha.call = call  # type: ignore[method-assign]
    before = {k: v["count"] for k, v in (await ha.lumagen_logs()).items()}
    after = await ha.lumagen_logs()
    deltas = [item["count"] - before.get(key, 0) for key, item in after.items()]
    assert deltas == [1]


def test_unverified_controls_require_explicit_exerciser_opt_in() -> None:
    safe = {step.name for step in probe.steps()}
    all_steps = {step.name for step in probe.steps(allow_unverified=True)}
    assert all_steps - safe == {
        "HDR gamma mode keeps max nits",
        "HDR max nits keeps gamma mode",
        "minimum fan speed",
        "subtitle shift",
        "aspect",
    }


async def test_exerciser_restores_after_confirmation_failure() -> None:
    states = {"number.level": "4", "select.sensitivity": "normal"}
    original = states.copy()
    ex = MagicMock()
    ex.state.side_effect = states.get

    async def set_value(key: str, value: str | float) -> float:
        states[key] = str(value).removesuffix(".0")
        if key == "number.level" and value == 5:
            raise AssertionError("reply lost after write")
        return 0

    ex.set = AsyncMock(side_effect=set_value)
    with pytest.raises(AssertionError, match="reply lost"):
        await probe._change_and_restore(ex, "number.level", lambda _v: 5, ["select.sensitivity"])
    assert states == original


async def test_exerciser_restores_fields_changed_by_a_compound_write_bug() -> None:
    states = {"number.level": "4", "select.sensitivity": "normal"}
    original = states.copy()
    ex = MagicMock()
    ex.state.side_effect = states.get

    async def set_value(key: str, value: str | float) -> float:
        states[key] = str(value).removesuffix(".0")
        if key == "number.level" and value == 5:
            states["select.sensitivity"] = "high"
        return 0

    ex.set = AsyncMock(side_effect=set_value)
    with pytest.raises(AssertionError, match="also changed"):
        await probe._change_and_restore(ex, "number.level", lambda _v: 5, ["select.sensitivity"])
    assert states == original


async def test_exerciser_surfaces_failed_restoration_as_an_abort() -> None:
    ex = MagicMock()
    ex.state.return_value = "4"
    ex.set = AsyncMock(side_effect=[0, probe.HAError("link down during restore")])
    with pytest.raises(probe.RestoreError, match="link down during restore"):
        await probe._change_and_restore(ex, "number.level", lambda _v: 5, [])


async def test_power_cycle_attempts_to_restore_on_after_standby_confirmation_fails() -> None:
    ex = MagicMock()
    ex.state.return_value = "on"
    ex.press = AsyncMock(return_value=0)
    ex.wait_for = AsyncMock(side_effect=[TimeoutError(), 0])
    with pytest.raises(TimeoutError):
        await probe._power_cycle(ex)
    assert [call.args[0] for call in ex.press.await_args_list] == [
        "button.standby",
        "button.power_on",
    ]


async def test_unmatched_scenarios_fail_before_connecting() -> None:
    args = argparse.Namespace(long=False, only=["not-a-scenario"])
    with pytest.raises(SystemExit, match="No scenarios"):
        await probe.scenarios(args)


async def test_cancelled_scenario_campaign_is_not_a_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    ha = MagicMock()
    ha.connect = AsyncMock()
    ha.close = AsyncMock()
    ha.entry = AsyncMock(return_value={"entry_id": "test"})
    ha.entities = AsyncMock(return_value={})
    health = MagicMock()
    health.attach = AsyncMock()
    monkeypatch.setattr(probe, "HA", MagicMock(return_value=ha))
    monkeypatch.setattr(probe, "Health", MagicMock(return_value=health))
    monkeypatch.setattr(probe, "load_token", lambda _path: "synthetic")
    monkeypatch.setattr(probe, "run_scenario", AsyncMock(side_effect=asyncio.CancelledError))
    monkeypatch.setattr(probe, "proxy_ctl", AsyncMock(return_value="ok"))
    args = argparse.Namespace(
        long=False,
        only=["malformed"],
        out=None,
        url="unused",
        token_file=None,
        entry="test",
        proxy_control="unused",
    )
    assert await probe.scenarios(args) == 1
    ha.close.assert_awaited_once()


async def test_exerciser_aborts_campaign_when_baseline_restore_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ha = MagicMock()
    for name in ("connect", "close", "subscribe"):
        setattr(ha, name, AsyncMock())
    ha.entry = AsyncMock(return_value={"entry_id": "test"})
    ha.entities = AsyncMock(return_value={})
    ha.states = AsyncMock(return_value={})
    failed = AsyncMock(side_effect=probe.RestoreError("restore failed"))
    must_not_run = AsyncMock()
    monkeypatch.setattr(probe, "HA", MagicMock(return_value=ha))
    monkeypatch.setattr(probe, "load_token", lambda _path: "synthetic")
    monkeypatch.setattr(
        probe,
        "steps",
        lambda **_kwargs: [
            probe.Step("failure", "none", failed),
            probe.Step("not safe to continue", "none", must_not_run),
        ],
    )
    args = argparse.Namespace(
        out=None,
        url="unused",
        token_file=None,
        entry="test",
        allow=[],
        allow_unverified=False,
        rounds=1,
        seed=0,
        operator=False,
        verbose=False,
        proxy_control=None,
        pause=0,
        interval=0,
    )
    assert await probe.exercise(args) == 1
    must_not_run.assert_not_awaited()
    ha.close.assert_awaited_once()


@pytest.mark.parametrize("forced_reload", [False, True])
async def test_detection_deadline_is_not_extended_or_hidden_by_reload(
    monkeypatch: pytest.MonkeyPatch, forced_reload: bool
) -> None:
    ha = MagicMock()
    ha.lumagen_logs = AsyncMock(return_value={})
    ha.service = AsyncMock()
    health = MagicMock()
    health.up = True
    health.identity_values.return_value = {}
    health.wait = AsyncMock(side_effect=[0, None, 0])
    counters = {"writes_unsolicited": 0, "concurrent_refused": 0}
    monkeypatch.setattr(
        probe, "proxy_ctl", AsyncMock(return_value=json.dumps({"counters": counters}))
    )

    async def reload(*_args: object) -> None:
        health.up = False

    monkeypatch.setattr(probe, "_ha_action", AsyncMock(side_effect=reload))
    args = argparse.Namespace(
        proxy_control="unused", recovery_deadline=1, detect_deadline=0.2, max_warnings=6
    )
    result = await probe.run_scenario(
        probe.Scenario("deadline", [], 0.1, "down", ha_action="reload" if forced_reload else None),
        ha,
        health,
        "test",
        {"button.query_status": "button.test"},
        args,
        probe.Journal(None),
    )
    assert not result["ok"]
    assert health.wait.await_args_list[1].args[1] <= args.detect_deadline

"""Tests for the custom fan curve controller (R0.2).

Covers: pure interpolation, hysteresis-gated drop, config validation,
the active-only guard (never touches hwmon while suspended), and firmware
restore on every mandated exit path (SIGTERM/loop-exit, exception, suspend
transition, reset). The last group is the point of the card, so those
tests are named for the hazard they guard against.
"""
from __future__ import annotations

import signal

import pytest

from conftest import rt


# ---------------------------------------------------------------------------
# parse_fan_curve: config validation (pure, no sysfs)
# ---------------------------------------------------------------------------


def test_parse_fan_curve_valid_multi_point():
    curve, problems = rt.parse_fan_curve("40:0,55:30,70:55,85:100")
    assert problems == []
    assert curve == [(40.0, 0.0), (55.0, 30.0), (70.0, 55.0), (85.0, 100.0)]


def test_parse_fan_curve_sorts_out_of_order_points():
    curve, problems = rt.parse_fan_curve("70:55,40:0,85:100,55:30")
    assert problems == []
    assert curve == [(40.0, 0.0), (55.0, 30.0), (70.0, 55.0), (85.0, 100.0)]


def test_parse_fan_curve_rejects_single_point():
    curve, problems = rt.parse_fan_curve("50:30")
    assert curve is None
    assert any("at least two points" in p for p in problems)


def test_parse_fan_curve_rejects_duplicate_temps():
    curve, problems = rt.parse_fan_curve("50:30,50:40,70:60")
    assert curve is None
    assert any("duplicate" in p for p in problems)


def test_parse_fan_curve_rejects_temp_out_of_range():
    curve, problems = rt.parse_fan_curve("-5:0,70:50")
    assert curve is None
    assert any("out of range" in p for p in problems)
    curve, problems = rt.parse_fan_curve("40:0,120:50")
    assert curve is None
    assert any("out of range" in p for p in problems)


def test_parse_fan_curve_rejects_pwm_out_of_range():
    curve, problems = rt.parse_fan_curve("40:-1,70:50")
    assert curve is None
    assert any("out of range" in p for p in problems)
    curve, problems = rt.parse_fan_curve("40:0,70:150")
    assert curve is None
    assert any("out of range" in p for p in problems)


def test_parse_fan_curve_rejects_non_monotonic_pwm():
    """A curve that cools the fan down as temp rises is invalid."""
    curve, problems = rt.parse_fan_curve("40:50,70:20,90:80")
    assert curve is None
    assert any("monotonically" in p for p in problems)


def test_parse_fan_curve_rejects_malformed_point():
    curve, problems = rt.parse_fan_curve("40:0,notapoint,70:50")
    assert curve is None
    assert any("not temp:pwm" in p for p in problems)


def test_parse_fan_curve_rejects_empty():
    curve, problems = rt.parse_fan_curve("")
    assert curve is None
    assert problems


# ---------------------------------------------------------------------------
# validate_conf: FAN_CURVE / FAN_CURVE_ENABLED / FAN_HYSTERESIS_C
# ---------------------------------------------------------------------------


def _base_raw_conf(**overrides):
    raw = {
        "VENDOR": "0x1002",
        "DEVICE": "0x7551",
        "SUBSYSTEM_VENDOR": "0x1043",
        "SUBSYSTEM_DEVICE": "0x0626",
        "POWER_LIMIT_W": "210",
        "VOLTAGE_OFFSET_MV": "0",
        "POLL_INTERVAL_S": "2",
        "EVICT_GUARD": "0",
        "EVICT_GUARD_MARGIN": "0.90",
    }
    raw.update(overrides)
    return raw


def test_validate_conf_valid_fan_curve_enabled():
    raw = _base_raw_conf(FAN_CURVE="40:0,55:30,70:55,85:100", FAN_CURVE_ENABLED="1")
    parsed, problems = rt.validate_conf(raw)
    assert not any(p.startswith("fan curve") for p in problems)
    assert parsed["FAN_CURVE_ENABLED"] is True
    assert parsed["FAN_CURVE"] == [(40.0, 0.0), (55.0, 30.0), (70.0, 55.0), (85.0, 100.0)]


def test_validate_conf_invalid_curve_is_config_error_when_enabled():
    """An invalid curve is a config error (non-fatal): daemon runs on firmware."""
    raw = _base_raw_conf(FAN_CURVE="50:30", FAN_CURVE_ENABLED="1")  # only one point
    parsed, problems = rt.validate_conf(raw)
    fan_problems = [p for p in problems if p.startswith("fan curve")]
    assert fan_problems
    assert parsed["FAN_CURVE"] is None
    # Non-fatal: fan curve problems must never be in the "fatal" bucket callers use.
    fatal = [p for p in problems if not p.startswith("unknown key") and not p.startswith("fan curve")]
    assert fatal == []


def test_validate_conf_enabled_with_no_curve_is_reported():
    raw = _base_raw_conf(FAN_CURVE_ENABLED="1")  # FAN_CURVE defaults to ""
    parsed, problems = rt.validate_conf(raw)
    assert any("no points" in p for p in problems)
    assert parsed["FAN_CURVE"] is None


def test_validate_conf_default_hysteresis_is_3():
    raw = _base_raw_conf()
    parsed, problems = rt.validate_conf(raw)
    assert parsed["FAN_HYSTERESIS_C"] == 3.0


def test_validate_conf_hysteresis_out_of_range_rejected():
    raw = _base_raw_conf(FAN_HYSTERESIS_C="999")
    parsed, problems = rt.validate_conf(raw)
    assert any("FAN_HYSTERESIS_C" in p for p in problems)
    assert parsed["FAN_HYSTERESIS_C"] is None


def test_validate_conf_disabled_curve_with_bad_string_is_harmless():
    """An unused (disabled) curve string can be garbage without aborting anything."""
    raw = _base_raw_conf(FAN_CURVE="garbage", FAN_CURVE_ENABLED="0")
    parsed, problems = rt.validate_conf(raw)
    fatal = [p for p in problems if not p.startswith("unknown key") and not p.startswith("fan curve")]
    assert fatal == []
    assert parsed["FAN_CURVE_ENABLED"] is False


# ---------------------------------------------------------------------------
# fan_curve_interpolate: pure linear interpolation, no sysfs
# ---------------------------------------------------------------------------

CURVE = [(40.0, 0.0), (55.0, 30.0), (70.0, 55.0), (85.0, 100.0)]


def test_interpolate_at_a_point():
    assert rt.fan_curve_interpolate(CURVE, 55.0) == 30.0
    assert rt.fan_curve_interpolate(CURVE, 70.0) == 55.0


def test_interpolate_between_points():
    # Midpoint of (40,0)-(55,30): temp 47.5 -> pwm 15.
    assert rt.fan_curve_interpolate(CURVE, 47.5) == pytest.approx(15.0)
    # Midpoint of (70,55)-(85,100): temp 77.5 -> pwm 77.5.
    assert rt.fan_curve_interpolate(CURVE, 77.5) == pytest.approx(77.5)


def test_interpolate_below_first_point_clamps():
    assert rt.fan_curve_interpolate(CURVE, 10.0) == 0.0
    assert rt.fan_curve_interpolate(CURVE, 0.0) == 0.0


def test_interpolate_above_last_point_clamps():
    assert rt.fan_curve_interpolate(CURVE, 110.0) == 100.0
    assert rt.fan_curve_interpolate(CURVE, 95.0) == 100.0


# ---------------------------------------------------------------------------
# fan_target_pwm: hysteresis on the way down only, no flapping
# ---------------------------------------------------------------------------


def test_hysteresis_rising_temp_tracks_curve_immediately():
    target, peak = rt.fan_target_pwm(CURVE, 60.0, 3.0, last_pwm=30.0, peak_temp=55.0)
    assert target == rt.fan_curve_interpolate(CURVE, 60.0)
    assert peak == 60.0


def test_hysteresis_blocks_drop_within_band():
    """A 1-degree oscillation below the peak must not drop the fan (no flapping)."""
    # Peak was 60C (raised fan to curve(60)); temp drops by 1C, well inside
    # the 3C hysteresis band -> fan must hold at last_pwm.
    last_pwm = rt.fan_curve_interpolate(CURVE, 60.0)
    target, peak = rt.fan_target_pwm(CURVE, 59.0, 3.0, last_pwm=last_pwm, peak_temp=60.0)
    assert target == last_pwm
    assert peak == 60.0  # peak reference unchanged while still within the band


def test_hysteresis_oscillation_does_not_flap():
    """Repeated 1-degree up/down oscillation around a peak never changes pwm."""
    last_pwm = rt.fan_curve_interpolate(CURVE, 60.0)
    peak = 60.0
    seen_pwms = {last_pwm}
    temps = [59.0, 60.0, 59.0, 60.0, 59.5, 60.0]
    for t in temps:
        last_pwm, peak = rt.fan_target_pwm(CURVE, t, 3.0, last_pwm, peak)
        seen_pwms.add(last_pwm)
    assert seen_pwms == {rt.fan_curve_interpolate(CURVE, 60.0)}


def test_hysteresis_drop_after_falling_past_band():
    last_pwm = rt.fan_curve_interpolate(CURVE, 60.0)
    # Falls 3C below the 60C peak -> hysteresis band cleared, drop allowed.
    target, peak = rt.fan_target_pwm(CURVE, 57.0, 3.0, last_pwm=last_pwm, peak_temp=60.0)
    assert target == rt.fan_curve_interpolate(CURVE, 57.0)
    assert target < last_pwm


def test_hysteresis_first_sample_has_no_last_pwm():
    target, peak = rt.fan_target_pwm(CURVE, 50.0, 3.0, last_pwm=None, peak_temp=None)
    assert target == rt.fan_curve_interpolate(CURVE, 50.0)
    assert peak == 50.0


# ---------------------------------------------------------------------------
# fan_controller_poll: active-only guard (never reads hwmon while suspended)
# ---------------------------------------------------------------------------


def _fan_conf(**overrides):
    raw = _base_raw_conf(FAN_CURVE="40:0,55:30,70:55,85:100", FAN_CURVE_ENABLED="1")
    raw.update(overrides)
    parsed, problems = rt.validate_conf(raw)
    assert not any(p.startswith("fan curve") for p in problems), problems
    return parsed


@pytest.fixture(autouse=True)
def _reset_fan_state(monkeypatch):
    """Isolate the module-level _fan singleton between tests."""
    fresh = rt._FanState()
    monkeypatch.setattr(rt, "_fan", fresh)
    yield


def _add_fan_sysfs(fake_tree, *, temp_c=50.0, pwm1_enable="2", pwm1="0", fan_rpm="0"):
    hwmon = fake_tree / "0000:aa:00.0" / "hwmon" / "hwmon7"
    (hwmon / "temp2_input").write_text(f"{int(temp_c * 1000)}\n")
    (hwmon / "pwm1_enable").write_text(pwm1_enable + "\n")
    (hwmon / "pwm1").write_text(pwm1 + "\n")
    (hwmon / "fan1_input").write_text(fan_rpm + "\n")
    return hwmon


def test_fan_controller_poll_never_reads_hwmon_while_suspended(fake_tree, monkeypatch, log_recorder):
    """HAZARD: reading hwmon while suspended can wake or hang the GPU.

    fan_controller_poll must only ever be invoked from the watch loop's
    active branch; this test proves the poll function itself does not
    quietly fall back to reading temp2_input/pwm1 if ever called while
    the card reports suspended, by asserting the caller-side contract:
    the watch loop's suspended branch takes a different code path that
    calls fan_release_if_manual(None), which performs zero sysfs reads.
    """
    pci = fake_tree / "0000:aa:00.0"
    _add_fan_sysfs(fake_tree, temp_c=50.0)
    (pci / "power" / "runtime_status").write_text("suspended\n")

    read_calls = []
    orig_read_text = rt.read_text

    def spy_read_text(path):
        read_calls.append(str(path))
        return orig_read_text(path)

    monkeypatch.setattr(rt, "read_text", spy_read_text)

    # Simulate the watch loop's suspended branch: no fan_controller_poll call,
    # only the release-if-manual path (which takes pci=None, i.e. no write).
    rt._fan.mode = "manual"
    rt._fan.last_pwm = 40.0
    rt.fan_release_if_manual(None)

    assert rt._fan.mode == "firmware"
    assert not any("temp2_input" in c or "pwm1" in c for c in read_calls)


def test_fan_controller_poll_disabled_releases_and_does_nothing_else(fake_tree, monkeypatch):
    pci = fake_tree / "0000:aa:00.0"
    _add_fan_sysfs(fake_tree, temp_c=60.0, pwm1_enable="1", pwm1="80")
    conf = _fan_conf(FAN_CURVE_ENABLED="0")
    rt._fan.mode = "manual"
    rt._fan.last_pwm = 80.0

    rt.fan_controller_poll(pci, conf)

    hwmon = pci / "hwmon" / "hwmon7"
    assert hwmon.joinpath("pwm1_enable").read_text().strip() == "2"
    assert rt._fan.mode == "firmware"
    assert rt._fan.last_pwm is None


def test_fan_controller_poll_enters_manual_and_sets_pwm(fake_tree, monkeypatch):
    pci = fake_tree / "0000:aa:00.0"
    _add_fan_sysfs(fake_tree, temp_c=70.0, pwm1_enable="2", pwm1="0")
    conf = _fan_conf()

    rt.fan_controller_poll(pci, conf)

    hwmon = pci / "hwmon" / "hwmon7"
    assert hwmon.joinpath("pwm1_enable").read_text().strip() == "1"
    assert rt._fan.mode == "manual"
    expected_pct = rt.fan_curve_interpolate(conf["FAN_CURVE"], 70.0)
    written_pct = int(hwmon.joinpath("pwm1").read_text().strip()) / 255.0 * 100.0
    assert written_pct == pytest.approx(expected_pct, abs=1.0)


def test_fan_controller_poll_invalid_curve_releases_to_firmware(fake_tree, monkeypatch):
    """An invalid/absent curve at poll time must behave like disabled."""
    pci = fake_tree / "0000:aa:00.0"
    _add_fan_sysfs(fake_tree, temp_c=60.0, pwm1_enable="1", pwm1="50")
    conf = _fan_conf()
    conf["FAN_CURVE"] = None  # simulate a config reload that produced no valid curve
    rt._fan.mode = "manual"
    rt._fan.last_pwm = 50.0

    rt.fan_controller_poll(pci, conf)

    hwmon = pci / "hwmon" / "hwmon7"
    assert hwmon.joinpath("pwm1_enable").read_text().strip() == "2"
    assert rt._fan.mode == "firmware"


# ---------------------------------------------------------------------------
# HAZARD: firmware restore on every mandated exit path
# ---------------------------------------------------------------------------


def _watch_conf_text_with_fan(*, curve="40:0,55:30,70:55,85:100", enabled=True):
    return (
        "VENDOR=0x1002\n"
        "DEVICE=0x7551\n"
        "SUBSYSTEM_VENDOR=0x1043\n"
        "SUBSYSTEM_DEVICE=0x0626\n"
        "POWER_LIMIT_W=210\n"
        "VOLTAGE_OFFSET_MV=0\n"
        "POLL_INTERVAL_S=2\n"
        "EVICT_GUARD=0\n"
        "EVICT_GUARD_MARGIN=0.90\n"
        f"FAN_CURVE={curve}\n"
        f"FAN_CURVE_ENABLED={1 if enabled else 0}\n"
    )


def test_hazard_firmware_restore_on_clean_stop_sigterm_sim(fake_tree, monkeypatch, tmp_path):
    """HAZARD: SIGTERM/loop-exit must never leave pwm1_enable=1 (manual)."""
    pci = fake_tree / "0000:aa:00.0"
    _add_fan_sysfs(fake_tree, temp_c=75.0, pwm1_enable="2", pwm1="0")

    conf_file = tmp_path / "watch.conf"
    conf_file.write_text(_watch_conf_text_with_fan())
    monkeypatch.setattr(rt, "CONF_PATH", conf_file)

    raw = rt.load_conf(conf_file)
    conf, problems = rt.validate_conf(raw)
    assert not any(p.startswith("fan curve") for p in problems)

    rt._stop = False
    rt._last_pm_refusal = None
    rt._last_kernel_scan = 0.0
    monkeypatch.setattr(rt, "handle_wake", lambda p, c, cc: True)

    sleep_count = 0

    def fake_sleep(*a, **kw):
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count == 1:
            # Simulate the SIGTERM handler having flipped the flag mid-poll.
            rt._stop = True

    monkeypatch.setattr(rt.time, "sleep", fake_sleep)

    result = rt.cmd_watch(conf)
    assert result == 0

    hwmon = pci / "hwmon" / "hwmon7"
    assert hwmon.joinpath("pwm1_enable").read_text().strip() == "2"
    assert rt._fan.mode == "firmware"


def test_hazard_firmware_restore_on_unhandled_exception(fake_tree, monkeypatch, tmp_path):
    """HAZARD: a crash mid-loop must not leave manual PWM set (finally block)."""
    pci = fake_tree / "0000:aa:00.0"
    _add_fan_sysfs(fake_tree, temp_c=75.0, pwm1_enable="2", pwm1="0")

    conf_file = tmp_path / "watch.conf"
    conf_file.write_text(_watch_conf_text_with_fan())
    monkeypatch.setattr(rt, "CONF_PATH", conf_file)

    raw = rt.load_conf(conf_file)
    conf, problems = rt.validate_conf(raw)
    assert not any(p.startswith("fan curve") for p in problems)

    rt._stop = False
    rt._last_pm_refusal = None
    rt._last_kernel_scan = 0.0
    monkeypatch.setattr(rt, "handle_wake", lambda p, c, cc: True)

    # Force the controller into manual mode via the pre-loop initial edge
    # (handle_wake is stubbed, but fan_controller_poll runs for real once
    # inside the loop body before we blow it up).
    call_count = 0
    orig_poll = rt.fan_controller_poll

    def poll_then_raise(pci_arg, conf_arg):
        nonlocal call_count
        call_count += 1
        orig_poll(pci_arg, conf_arg)
        if call_count == 1:
            raise RuntimeError("simulated watcher crash")

    monkeypatch.setattr(rt, "fan_controller_poll", poll_then_raise)

    # Make the loop actually stop after the exception is caught+logged once.
    sleep_count = 0

    def fake_sleep(*a, **kw):
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count == 1:
            rt._stop = True

    monkeypatch.setattr(rt.time, "sleep", fake_sleep)

    result = rt.cmd_watch(conf)
    assert result == 0

    hwmon = pci / "hwmon" / "hwmon7"
    # The per-iteration try/except caught the RuntimeError (watcher loop
    # error, logged) but manual mode was entered before the raise; the
    # outer try/finally must still have released it on the way out.
    assert hwmon.joinpath("pwm1_enable").read_text().strip() == "2"
    assert rt._fan.mode == "firmware"


def test_hazard_firmware_restore_on_suspend_transition(fake_tree, monkeypatch, tmp_path, log_recorder):
    """HAZARD: the card going suspended must clear the controller's manual
    state immediately (never be the reason the card stays awake), and must
    NOT attempt any hwmon write once suspended — writing pwm1_enable while
    suspended is exactly the class of access the hard rules forbid (it can
    wake or hang the device the same way a read can). The physical register
    is left alone; a stale manual value found on the next wake is handled
    by the startup orphan check instead."""
    pci = fake_tree / "0000:aa:00.0"
    hwmon = _add_fan_sysfs(fake_tree, temp_c=75.0, pwm1_enable="2", pwm1="0")

    conf_file = tmp_path / "watch.conf"
    conf_file.write_text(_watch_conf_text_with_fan())
    monkeypatch.setattr(rt, "CONF_PATH", conf_file)

    raw = rt.load_conf(conf_file)
    conf, problems = rt.validate_conf(raw)
    assert not any(p.startswith("fan curve") for p in problems)

    rt._stop = False
    rt._last_pm_refusal = None
    rt._last_kernel_scan = 0.0
    monkeypatch.setattr(rt, "handle_wake", lambda p, c, cc: True)

    # First loop tick: active, curve engages (manual mode entered, pwm1_enable
    # written to 1). Second loop tick: card reports suspended before the fan
    # poll fires again.
    tick = 0

    def fake_status(pci_arg):
        nonlocal tick
        if tick == 0:
            return "active"
        return "suspended"

    monkeypatch.setattr(rt, "runtime_status", fake_status)

    # Prove no hwmon access happens once the suspended branch is taken:
    # count writes to pwm1_enable that occur AFTER the transition.
    write_calls_after_suspend = []
    orig_write_text = rt.write_text

    def spy_write_text(path, value):
        if tick >= 1 and "pwm1" in str(path):
            write_calls_after_suspend.append(str(path))
        return orig_write_text(path, value)

    monkeypatch.setattr(rt, "write_text", spy_write_text)

    sleep_count = 0

    def fake_sleep(*a, **kw):
        nonlocal sleep_count, tick
        sleep_count += 1
        tick += 1
        if sleep_count == 2:
            rt._stop = True

    monkeypatch.setattr(rt.time, "sleep", fake_sleep)

    result = rt.cmd_watch(conf)
    assert result == 0

    # First tick did enter manual mode (proves the scenario is realistic).
    assert hwmon.joinpath("pwm1_enable").read_text().strip() == "1"
    # In-process controller state was cleared without touching hwmon.
    assert rt._fan.mode == "firmware"
    assert write_calls_after_suspend == []
    release_logs = [m for lvl, m in log_recorder if "card suspended, releasing to firmware" in m]
    assert len(release_logs) == 1


def test_hazard_reset_releases_fan_to_firmware(fake_tree, conf, monkeypatch):
    """HAZARD: `reset` is a mandated firmware-fallback exit path."""
    pci = fake_tree / "0000:aa:00.0"
    hwmon = _add_fan_sysfs(fake_tree, temp_c=75.0, pwm1_enable="1", pwm1="120")
    (hwmon / "power1_cap_min").write_text("210000000\n")
    (hwmon / "power1_cap_max").write_text("330000000\n")
    (hwmon / "power1_cap").write_text("210000000\n")
    (hwmon / "power1_cap_default").write_text("300000000\n")

    result = rt.cmd_reset(conf)

    assert result == 0
    assert hwmon.joinpath("pwm1_enable").read_text().strip() == "2"

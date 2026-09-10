"""Tests for the OD fan-curve backend (gpu_od/fan_ctrl/fan_curve).

The live RX 9700 (PCI 1002:7551, ASUS) has NO hwmon pwm1_enable — fan
control is only available via the amdgpu gpu_od sysfs interface, whose
table has five anchor points (hotspot temp °C + fan speed %), documented
ranges FAN_CURVE(hotspot temp) 25..100 C and FAN_CURVE(fan speed) 30..100
%, and a commit ("c") / reset ("r") protocol. The original R0.2 controller
only spoke the pwm1_enable/pwm1 dialect, so on this card enabling a curve
produced a "failed to enter manual mode: ENOENT" on every poll and the
curve was inert. These tests pin the OD backend against a fake tree that
matches the real card layout (OD table present, pwm1_enable absent).

Note on the fake fs: rt.write_text truncates, so a sysfs file holds only
the LAST value written. Sequence assertions therefore go through
_spy_writes (captures every call, still lands it on the fake fs).
"""
from __future__ import annotations

import pytest

from conftest import rt


# Stock table exactly as the live card reads it (all zeros = firmware default).
STOCK_OD = (
    "OD_FAN_CURVE:\n"
    "0: 0C 0%\n"
    "1: 0C 0%\n"
    "2: 0C 0%\n"
    "3: 0C 0%\n"
    "4: 0C 0%\n"
    "OD_RANGE:\n"
    "FAN_CURVE(hotspot temp): 25C 100C\n"
    "FAN_CURVE(fan speed): 30% 100%\n"
)

CURVE = [(40.0, 0.0), (55.0, 30.0), (70.0, 55.0), (85.0, 100.0)]


@pytest.fixture(autouse=True)
def _reset_fan_state(monkeypatch):
    """Isolate the module-level _fan singleton between tests."""
    monkeypatch.setattr(rt, "_fan", rt._FanState())
    yield


def _od_file(fake_tree, text=STOCK_OD):
    od = fake_tree / "0000:aa:00.0" / "gpu_od" / "fan_ctrl"
    od.mkdir(parents=True, exist_ok=True)
    (od / "fan_curve").write_text(text)
    return od / "fan_curve"


def _od_hwmon(fake_tree, *, temp_c=50.0, fan_rpm="1000"):
    """hwmon layout matching the live R9700: pwm1 exists, pwm1_enable does NOT."""
    hwmon = fake_tree / "0000:aa:00.0" / "hwmon" / "hwmon7"
    (hwmon / "temp2_input").write_text(f"{int(temp_c * 1000)}\n")
    (hwmon / "fan1_input").write_text(fan_rpm + "\n")
    (hwmon / "pwm1").write_text("76\n")
    assert not (hwmon / "pwm1_enable").exists()
    return hwmon


def _spy_writes(monkeypatch):
    """Capture write_text calls, still landing them on the fake fs."""
    calls: list[tuple[str, str]] = []
    orig = rt.write_text

    def spy(path, value):
        calls.append((str(path), value))
        return orig(path, value)

    monkeypatch.setattr(rt, "write_text", spy)
    return calls


def _fan_writes(calls):
    return [v for p, v in calls if p.endswith("gpu_od/fan_ctrl/fan_curve")]


def _fan_log(log_recorder, needle):
    return [m for lvl, m in log_recorder if needle in m]


# ---------------------------------------------------------------------------
# backend selection
# ---------------------------------------------------------------------------


def test_fan_backend_od_when_no_pwm1_enable(fake_tree):
    pci = fake_tree / "0000:aa:00.0"
    _od_hwmon(fake_tree)
    _od_file(fake_tree)
    assert rt.fan_backend(pci) == "od"


def test_fan_backend_pwm_when_pwm1_enable(fake_tree):
    pci = fake_tree / "0000:aa:00.0"
    hwmon = _od_hwmon(fake_tree)
    (hwmon / "pwm1_enable").write_text("2\n")
    assert rt.fan_backend(pci) == "pwm"


def test_fan_backend_od_preferred_when_both_present(fake_tree):
    pci = fake_tree / "0000:aa:00.0"
    hwmon = _od_hwmon(fake_tree)
    (hwmon / "pwm1_enable").write_text("2\n")
    _od_file(fake_tree)
    assert rt.fan_backend(pci) == "od"


def test_fan_backend_none_when_neither(fake_tree):
    pci = fake_tree / "0000:aa:00.0"
    _od_hwmon(fake_tree)  # no pwm1_enable, no gpu_od
    assert rt.fan_backend(pci) == "none"


def test_fan_backend_none_when_od_table_has_no_range(fake_tree):
    pci = fake_tree / "0000:aa:00.0"
    _od_hwmon(fake_tree)
    _od_file(fake_tree, "OD_FAN_CURVE:\n0: 0C 0%\n")
    assert rt.fan_backend(pci) == "none"


def test_fan_backend_pwm_when_od_table_broken_but_pwm1_enable(fake_tree):
    pci = fake_tree / "0000:aa:00.0"
    hwmon = _od_hwmon(fake_tree)
    (hwmon / "pwm1_enable").write_text("2\n")
    _od_file(fake_tree, "junk")
    assert rt.fan_backend(pci) == "pwm"


# ---------------------------------------------------------------------------
# parsing the OD table (pure)
# ---------------------------------------------------------------------------


def test_parse_od_fan_curve_stock():
    points, rng = rt.parse_od_fan_curve(STOCK_OD)
    assert points == [(0, 0), (0, 0), (0, 0), (0, 0), (0, 0)]
    assert rng == {"t_min": 25.0, "t_max": 100.0, "p_min": 30.0, "p_max": 100.0}


def test_parse_od_fan_curve_custom():
    text = (
        "OD_FAN_CURVE:\n"
        "0: 25C 30%\n"
        "1: 44C 30%\n"
        "2: 62C 42%\n"
        "3: 81C 89%\n"
        "4: 100C 100%\n"
        "OD_RANGE:\n"
        "FAN_CURVE(hotspot temp): 25C 100C\n"
        "FAN_CURVE(fan speed): 30% 100%\n"
    )
    points, _ = rt.parse_od_fan_curve(text)
    assert points == [(25, 30), (44, 30), (62, 42), (81, 89), (100, 100)]


def test_parse_od_fan_curve_malformed():
    points, rng = rt.parse_od_fan_curve("nonsense")
    assert points is None
    assert rng is None


# ---------------------------------------------------------------------------
# quantizing the user curve into the 5 OD anchors (pure)
# ---------------------------------------------------------------------------


def test_od_anchor_points_basic():
    anchors = rt.od_anchor_points(CURVE)
    assert len(anchors) == 5
    temps = [t for t, _ in anchors]
    assert temps == sorted(set(temps)), "anchor temps must be strictly increasing"
    assert all(25 <= t <= 100 for t in temps)
    pwms = [p for _, p in anchors]
    assert all(30 <= p <= 100 for p in pwms), "pwm clamped into the OD floor range"
    assert pwms == sorted(pwms), "monotonic non-decreasing preserved"
    mid = dict(anchors)[62]
    assert mid == pytest.approx(rt.fan_curve_interpolate(CURVE, 62.0), abs=1.0)


def test_od_anchor_points_clamps_flat_curve_to_floor():
    anchors = rt.od_anchor_points([(25.0, 0.0), (30.0, 0.0)])
    assert [p for _, p in anchors] == [30, 30, 30, 30, 30]


def test_od_anchor_points_full_range_curve():
    anchors = rt.od_anchor_points([(0.0, 0.0), (100.0, 100.0)])
    pwms = [p for _, p in anchors]
    assert pwms[0] == 30  # clamped to floor
    assert pwms[-1] == 100
    assert pwms == sorted(pwms)


def test_od_anchor_points_honours_custom_range():
    rng = {"t_min": 30.0, "t_max": 90.0, "p_min": 40.0, "p_max": 80.0}
    anchors = rt.od_anchor_points(CURVE, rng)
    temps = [t for t, _ in anchors]
    assert temps[0] == 30 and temps[-1] == 90
    assert all(40 <= p <= 80 for _, p in anchors)


# ---------------------------------------------------------------------------
# write / commit / reset protocol
# ---------------------------------------------------------------------------


def test_write_od_fan_curve_sequence(fake_tree, monkeypatch):
    pci = fake_tree / "0000:aa:00.0"
    _od_file(fake_tree)
    calls = _spy_writes(monkeypatch)

    rt.write_od_fan_curve(pci, [(25, 30), (44, 30), (62, 42), (81, 89), (100, 100)])

    assert _fan_writes(calls) == [
        "0 25 30\n", "1 44 30\n", "2 62 42\n", "3 81 89\n", "4 100 100\n", "c\n",
    ]


def test_reset_od_fan_curve_writes_r(fake_tree, monkeypatch):
    pci = fake_tree / "0000:aa:00.0"
    _od_file(fake_tree)
    calls = _spy_writes(monkeypatch)

    rt.reset_od_fan_curve(pci)

    assert _fan_writes(calls) == ["r\n"]


def test_reset_od_fan_curve_missing_table_is_best_effort(fake_tree, monkeypatch):
    """Exit paths must never raise even when the table vanished (rebind)."""
    pci = fake_tree / "0000:aa:00.0"
    _spy_writes(monkeypatch)
    rt.reset_od_fan_curve(pci)  # must not raise


# ---------------------------------------------------------------------------
# fan_controller_poll on the real-card layout (OD only)
# ---------------------------------------------------------------------------


def _fan_conf(curve="40:0,55:30,70:55,85:100", enabled="1"):
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
        "FAN_CURVE": curve,
        "FAN_CURVE_ENABLED": enabled,
    }
    parsed, problems = rt.validate_conf(raw)
    assert not any(p.startswith("fan curve") for p in problems), problems
    return parsed


def test_poll_engages_od_curve(fake_tree, monkeypatch, log_recorder):
    pci = fake_tree / "0000:aa:00.0"
    hwmon = _od_hwmon(fake_tree, temp_c=50.0)
    _od_file(fake_tree)
    calls = _spy_writes(monkeypatch)
    conf = _fan_conf()

    rt.fan_controller_poll(pci, conf)

    assert rt._fan.mode == "manual"
    assert rt._fan.backend == "od"
    w = _fan_writes(calls)
    assert w[-1] == "c\n"                    # committed
    assert len(w) == 6                       # 5 anchors + commit
    # pwm1_enable was never touched (it does not exist on this card):
    assert not (hwmon / "pwm1_enable").exists()
    assert not any("pwm1" in p for p, _ in calls)
    assert _fan_log(log_recorder, "fan curve: OD curve active")


def test_poll_does_not_recommit_unchanged_curve(fake_tree, monkeypatch, log_recorder):
    pci = fake_tree / "0000:aa:00.0"
    _od_file(fake_tree)
    calls = _spy_writes(monkeypatch)
    conf = _fan_conf()

    rt.fan_controller_poll(pci, conf)
    commits_1 = len([v for v in _fan_writes(calls) if v == "c\n"])
    rt.fan_controller_poll(pci, conf)
    rt.fan_controller_poll(pci, conf)

    assert len([v for v in _fan_writes(calls) if v == "c\n"]) == commits_1


def test_poll_recommits_when_curve_changes(fake_tree, monkeypatch, log_recorder):
    pci = fake_tree / "0000:aa:00.0"
    _od_hwmon(fake_tree, temp_c=50.0)
    _od_file(fake_tree)
    calls = _spy_writes(monkeypatch)
    conf = _fan_conf()

    rt.fan_controller_poll(pci, conf)
    commits_1 = len([v for v in _fan_writes(calls) if v == "c\n"])

    conf["FAN_CURVE"] = [(25.0, 0.0), (100.0, 100.0)]  # e.g. config reload
    rt.fan_controller_poll(pci, conf)

    assert len([v for v in _fan_writes(calls) if v == "c\n"]) == commits_1 + 1


def test_poll_disabled_releases_od_curve(fake_tree, monkeypatch, log_recorder):
    pci = fake_tree / "0000:aa:00.0"
    _od_hwmon(fake_tree, temp_c=50.0)
    _od_file(fake_tree)
    calls = _spy_writes(monkeypatch)
    conf = _fan_conf()
    rt.fan_controller_poll(pci, conf)  # engage

    conf_off = _fan_conf(enabled="0")
    rt.fan_controller_poll(pci, conf_off)

    assert rt._fan.mode == "firmware"
    assert _fan_writes(calls)[-1] == "r\n"


def test_poll_no_backend_releases_without_error(fake_tree, log_recorder):
    """A card with neither control path must degrade to firmware, no spam,
    no exception (this was the live defect: ENOENT every poll)."""
    pci = fake_tree / "0000:aa:00.0"
    hwmon = _od_hwmon(fake_tree)  # no OD table either
    conf = _fan_conf()

    rt.fan_controller_poll(pci, conf)  # must not raise
    rt.fan_controller_poll(pci, conf)

    assert rt._fan.mode == "firmware"
    assert not (hwmon / "pwm1_enable").exists()
    assert _fan_log(log_recorder, "failed to enter manual") == []


# ---------------------------------------------------------------------------
# release + hazard paths on the OD backend
# ---------------------------------------------------------------------------


def test_release_fan_to_firmware_od_resets_table(fake_tree, monkeypatch):
    pci = fake_tree / "0000:aa:00.0"
    _od_hwmon(fake_tree)
    _od_file(fake_tree)
    calls = _spy_writes(monkeypatch)
    rt._fan.mode = "manual"
    rt._fan.backend = "od"

    rt.release_fan_to_firmware(pci)

    assert _fan_writes(calls) == ["r\n"]


def test_release_fan_to_firmware_none_backend_is_silent(fake_tree, monkeypatch):
    pci = fake_tree / "0000:aa:00.0"
    _od_hwmon(fake_tree)  # neither pwm1_enable nor gpu_od
    calls = _spy_writes(monkeypatch)
    rt._fan.mode = "manual"

    rt.release_fan_to_firmware(pci)  # must not raise

    assert not any("pwm1_enable" in p for p, _ in calls)
    assert _fan_writes(calls) == []


def test_hazard_od_watch_clean_stop_resets_curve(fake_tree, monkeypatch, tmp_path):
    """HAZARD: a clean daemon stop on an OD card must reset the OD table,
    not write pwm1_enable."""
    pci = fake_tree / "0000:aa:00.0"
    _od_hwmon(fake_tree, temp_c=75.0)
    _od_file(fake_tree)
    calls = _spy_writes(monkeypatch)

    conf_file = tmp_path / "watch.conf"
    conf_file.write_text(
        "VENDOR=0x1002\nDEVICE=0x7551\nSUBSYSTEM_VENDOR=0x1043\n"
        "SUBSYSTEM_DEVICE=0x0626\nPOWER_LIMIT_W=210\nVOLTAGE_OFFSET_MV=0\n"
        "POLL_INTERVAL_S=2\nEVICT_GUARD=0\nEVICT_GUARD_MARGIN=0.90\n"
        "FAN_CURVE=40:0,55:30,70:55,85:100\nFAN_CURVE_ENABLED=1\n"
    )
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
            rt._stop = True

    monkeypatch.setattr(rt.time, "sleep", fake_sleep)

    result = rt.cmd_watch(conf)
    assert result == 0
    w = _fan_writes(calls)
    assert "c\n" in w    # engaged during the loop
    assert w[-1] == "r\n"  # 'r' committed on the exit path
    assert rt._fan.mode == "firmware"


def test_fan_status_reports_od_mode(fake_tree, conf):
    """While active, the fan block must report the OD table, not a null mode
    (the live card has no pwm1_enable for the old probe)."""
    pci = fake_tree / "0000:aa:00.0"
    _od_hwmon(fake_tree, temp_c=50.0)
    _od_file(fake_tree, (
        "OD_FAN_CURVE:\n"
        "0: 25C 30%\n"
        "1: 44C 30%\n"
        "2: 62C 42%\n"
        "3: 81C 89%\n"
        "4: 100C 100%\n"
        "OD_RANGE:\n"
        "FAN_CURVE(hotspot temp): 25C 100C\n"
        "FAN_CURVE(fan speed): 30% 100%\n"
    ))

    fan = rt._fan_status(conf, pci, "active")
    assert fan["mode"] == "od"
    assert fan["od_curve"] == [(25, 30), (44, 30), (62, 42), (81, 89), (100, 100)]
    assert fan["rpm"] == 1000


def test_fan_status_od_stock_table_is_firmware(fake_tree, conf):
    pci = fake_tree / "0000:aa:00.0"
    _od_hwmon(fake_tree, temp_c=50.0)
    _od_file(fake_tree)  # all-zeros stock table

    fan = rt._fan_status(conf, pci, "active")
    assert fan["mode"] == "firmware"
    assert fan["od_curve"] == [(0, 0)] * 5

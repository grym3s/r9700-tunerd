"""Shared fixtures for r9700-tunerd unit tests.

Loads the single-file daemon as module ``rt`` and provides a fake sysfs
tree under ``tmp_path`` so no real hardware, /sys, /etc, /run, or syslog
is ever touched.
"""
from __future__ import annotations

import importlib.util
import importlib.machinery
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Load the daemon script as a module (no .py suffix).
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "r9700-tunerd"

_spec = importlib.util.spec_from_file_location("rt", _SCRIPT, loader=importlib.machinery.SourceFileLoader("rt", str(_SCRIPT)))
rt = importlib.util.module_from_spec(_spec)
sys.modules["rt"] = rt
_spec.loader.exec_module(rt)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_tree(tmp_path, monkeypatch):
    """Build a realistic fake PCI sysfs tree and monkeypatch rt globals.

    Primary GPU  : 0000:aa:00.0  (0x1002/0x7551/0x1043/0x0626, control=auto)
    iGPU (APU)  : 0000:bb:00.0  (0x1002/0x1586/0x1f66/0x0030, control=on)

    Returns the tmp_path that serves as PCI_ROOT.
    """
    # --- Primary R9700 ---------------------------------------------------
    gpu = tmp_path / "0000:aa:00.0"
    gpu.mkdir(parents=True)
    (gpu / "vendor").write_text("0x1002\n")
    (gpu / "device").write_text("0x7551\n")
    (gpu / "subsystem_vendor").write_text("0x1043\n")
    (gpu / "subsystem_device").write_text("0x0626\n")

    power = gpu / "power"
    power.mkdir()
    (power / "runtime_status").write_text("active\n")
    (power / "control").write_text("auto\n")
    (power / "runtime_suspended_time").write_text("1000\n")
    (gpu / "power_state").write_text("D0\n")

    od_text = (
        "OD_VDDGFX_OFFSET:\n"
        "0mV\n"
        "OD_RANGE:\n"
        "VDDGFX_OFFSET: -200mV 0mV\n"
    )
    (gpu / "pp_od_clk_voltage").write_text(od_text)

    hwmon = gpu / "hwmon" / "hwmon7"
    hwmon.mkdir(parents=True)
    (hwmon / "power1_cap").write_text("210000000\n")
    (hwmon / "power1_cap_min").write_text("210000000\n")
    (hwmon / "power1_cap_max").write_text("330000000\n")
    (hwmon / "power1_cap_default").write_text("300000000\n")

    drm = gpu / "drm"
    drm.mkdir()
    (drm / "card1").write_text("")
    (drm / "renderD128").write_text("")

    # --- iGPU (APU display) ---------------------------------------------
    igpu = tmp_path / "0000:bb:00.0"
    igpu.mkdir(parents=True)
    (igpu / "vendor").write_text("0x1002\n")
    (igpu / "device").write_text("0x1586\n")
    (igpu / "subsystem_vendor").write_text("0x1f66\n")
    (igpu / "subsystem_device").write_text("0x0030\n")
    igpu_power = igpu / "power"
    igpu_power.mkdir()
    (igpu_power / "runtime_status").write_text("active\n")
    (igpu_power / "control").write_text("on\n")

    # --- Monkeypatch rt module globals ----------------------------------
    monkeypatch.setattr(rt, "PCI_ROOT", tmp_path)
    monkeypatch.setattr(rt, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(rt, "SETTLE_S", 0)
    monkeypatch.setattr(rt, "RETRY_SLEEPS", (0, 0, 0))
    monkeypatch.setattr(rt.time, "sleep", lambda *a, **kw: None)

    return tmp_path


@pytest.fixture
def log_recorder(monkeypatch):
    """Capture (level, msg) tuples instead of calling real syslog."""
    records: list[tuple[int, str]] = []

    def _fake_syslog(level, msg):
        records.append((level, msg))

    monkeypatch.setattr(rt.syslog, "syslog", _fake_syslog)
    return records


@pytest.fixture
def conf():
    """Validated config dict matching the fake R9700 identity."""
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
    parsed, problems = rt.validate_conf(raw)
    assert not problems, f"Unexpected validation problems: {problems}"
    return parsed


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def bump_suspended(pci: Path, ms: int) -> None:
    """Set power/runtime_suspended_time to the given value (ms).

    Simulates the kernel incrementing the monotonic suspend-time counter
    after a suspend/resume cycle.
    """
    (pci / "power" / "runtime_suspended_time").write_text(f"{ms}\n")

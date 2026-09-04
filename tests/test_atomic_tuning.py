"""Tests for set-tuning (atomic dual mutation), status --json, and
reset exit-code propagation.

Uses the same fake sysfs tree as tests/test_unit.py (see conftest.py).
"""
from __future__ import annotations

import json

import pytest

from conftest import rt


# ---------------------------------------------------------------------------
# cmd_set_tuning: active card
# ---------------------------------------------------------------------------


def test_set_tuning_active_validates_and_applies(fake_tree, conf, monkeypatch, tmp_path, log_recorder):
    pci = fake_tree / "0000:aa:00.0"
    hwmon = pci / "hwmon" / "hwmon7"
    (hwmon / "power1_cap_min").write_text("210000000\n")
    (hwmon / "power1_cap_max").write_text("330000000\n")
    (hwmon / "power1_cap").write_text("210000000\n")
    (hwmon / "power1_cap_default").write_text("300000000\n")

    conf_file = tmp_path / "test.conf"
    conf_file.write_text("POWER_LIMIT_W=210\nVOLTAGE_OFFSET_MV=0\n")
    monkeypatch.setattr(rt, "CONF_PATH", conf_file)

    od_path = pci / "pp_od_clk_voltage"

    def fake_write_text(path, value):
        if "pp_od_clk_voltage" in str(path) and value == "vo -25\n":
            od_path.write_text(
                "OD_VDDGFX_OFFSET:\n-25mV\nOD_RANGE:\nVDDGFX_OFFSET: -200mV 0mV\n"
            )
        if "power1_cap" in str(path) and str(path).endswith("power1_cap"):
            (hwmon / "power1_cap").write_text(value)

    monkeypatch.setattr(rt, "write_text", fake_write_text)

    result = rt.cmd_set_tuning(conf, -25, 250)
    assert result == 0

    text = conf_file.read_text(encoding="utf-8")
    assert "VOLTAGE_OFFSET_MV=-25" in text
    assert "POWER_LIMIT_W=250" in text


def test_set_tuning_active_rejects_offset_out_of_range(fake_tree, conf, monkeypatch, tmp_path):
    """Offset outside the live OD range dies before any config write, cap included."""
    pci = fake_tree / "0000:aa:00.0"
    hwmon = pci / "hwmon" / "hwmon7"
    (hwmon / "power1_cap_min").write_text("210000000\n")
    (hwmon / "power1_cap_max").write_text("330000000\n")

    conf_file = tmp_path / "test.conf"
    conf_file.write_text("POWER_LIMIT_W=210\nVOLTAGE_OFFSET_MV=0\n")
    monkeypatch.setattr(rt, "CONF_PATH", conf_file)

    with pytest.raises(SystemExit):
        rt.cmd_set_tuning(conf, -9999, 250)

    text = conf_file.read_text(encoding="utf-8")
    assert "VOLTAGE_OFFSET_MV=0" in text
    assert "POWER_LIMIT_W=210" in text  # cap untouched even though it was valid


def test_set_tuning_active_rejects_cap_out_of_range(fake_tree, conf, monkeypatch, tmp_path):
    """Cap outside the live range dies; offset (even if valid) is never written."""
    pci = fake_tree / "0000:aa:00.0"
    hwmon = pci / "hwmon" / "hwmon7"
    (hwmon / "power1_cap_min").write_text("210000000\n")
    (hwmon / "power1_cap_max").write_text("330000000\n")

    conf_file = tmp_path / "test.conf"
    conf_file.write_text("POWER_LIMIT_W=210\nVOLTAGE_OFFSET_MV=0\n")
    monkeypatch.setattr(rt, "CONF_PATH", conf_file)

    with pytest.raises(SystemExit):
        rt.cmd_set_tuning(conf, -25, 9999)

    text = conf_file.read_text(encoding="utf-8")
    assert "VOLTAGE_OFFSET_MV=0" in text
    assert "POWER_LIMIT_W=210" in text


def test_set_tuning_positive_offset_dies(fake_tree, conf):
    with pytest.raises(SystemExit):
        rt.cmd_set_tuning(conf, 5, 250)


def test_set_tuning_restores_prior_config_on_apply_failure(
    fake_tree, conf, monkeypatch, tmp_path, log_recorder
):
    """If cmd_apply fails after the config write, the prior values are
    restored via a second atomic write, and cmd_set_tuning returns nonzero."""
    pci = fake_tree / "0000:aa:00.0"
    hwmon = pci / "hwmon" / "hwmon7"
    (hwmon / "power1_cap_min").write_text("210000000\n")
    (hwmon / "power1_cap_max").write_text("330000000\n")
    (hwmon / "power1_cap").write_text("210000000\n")
    (hwmon / "power1_cap_default").write_text("300000000\n")

    conf_file = tmp_path / "test.conf"
    conf_file.write_text("POWER_LIMIT_W=210\nVOLTAGE_OFFSET_MV=0\n")
    monkeypatch.setattr(rt, "CONF_PATH", conf_file)

    monkeypatch.setattr(rt, "cmd_apply", lambda c: 1)

    result = rt.cmd_set_tuning(conf, -25, 250)
    assert result == 1

    text = conf_file.read_text(encoding="utf-8")
    assert "VOLTAGE_OFFSET_MV=0" in text
    assert "POWER_LIMIT_W=210" in text
    assert "VOLTAGE_OFFSET_MV=-25" not in text
    assert "POWER_LIMIT_W=250" not in text


def test_set_tuning_single_atomic_write_for_config(fake_tree, conf, monkeypatch, tmp_path):
    """Exactly one _atomic_write call touches CONF_PATH for the happy path
    (both values applied together, not two separate writes)."""
    pci = fake_tree / "0000:aa:00.0"
    hwmon = pci / "hwmon" / "hwmon7"
    (hwmon / "power1_cap_min").write_text("210000000\n")
    (hwmon / "power1_cap_max").write_text("330000000\n")
    (hwmon / "power1_cap").write_text("210000000\n")
    (hwmon / "power1_cap_default").write_text("300000000\n")

    conf_file = tmp_path / "test.conf"
    conf_file.write_text("POWER_LIMIT_W=210\nVOLTAGE_OFFSET_MV=0\n")
    monkeypatch.setattr(rt, "CONF_PATH", conf_file)
    monkeypatch.setattr(rt, "cmd_apply", lambda c: 0)

    calls = []
    orig_atomic_write = rt._atomic_write

    def tracked(path, data):
        calls.append(str(path))
        return orig_atomic_write(path, data)

    monkeypatch.setattr(rt, "_atomic_write", tracked)

    assert rt.cmd_set_tuning(conf, -25, 250) == 0
    conf_writes = [c for c in calls if c == str(conf_file)]
    assert len(conf_writes) == 1


# ---------------------------------------------------------------------------
# cmd_set_tuning: suspended card (cached ranges)
# ---------------------------------------------------------------------------


def _suspend(fake_tree):
    pci = fake_tree / "0000:aa:00.0"
    (pci / "power" / "runtime_status").write_text("suspended\n")
    (pci / "power_state").write_text("D3cold\n")
    return pci


def _ranges_file(tmp_path, monkeypatch, content):
    rp = tmp_path / "state" / "ranges.json"
    rp.parent.mkdir(parents=True, exist_ok=True)
    if content is not None:
        rp.write_text(content)
    monkeypatch.setattr(rt, "RANGES_PATH", rp)
    return rp


def test_set_tuning_suspended_uses_cached_range(fake_tree, conf, monkeypatch, tmp_path):
    _suspend(fake_tree)
    _ranges_file(
        tmp_path, monkeypatch,
        '{"cap_min_w": 210, "cap_max_w": 330, "cap_default_w": 300, '
        '"vo_min_mv": -200, "vo_max_mv": 0, "ts": 1}',
    )
    conf_file = tmp_path / "test.conf"
    conf_file.write_text("POWER_LIMIT_W=210\nVOLTAGE_OFFSET_MV=0\n")
    monkeypatch.setattr(rt, "CONF_PATH", conf_file)

    result = rt.cmd_set_tuning(conf, -25, 250)
    assert result == 0
    text = conf_file.read_text()
    assert "VOLTAGE_OFFSET_MV=-25" in text
    assert "POWER_LIMIT_W=250" in text


def test_set_tuning_suspended_without_cache_dies(fake_tree, conf, monkeypatch, tmp_path):
    _suspend(fake_tree)
    _ranges_file(tmp_path, monkeypatch, None)
    conf_file = tmp_path / "test.conf"
    conf_file.write_text("POWER_LIMIT_W=210\nVOLTAGE_OFFSET_MV=0\n")
    monkeypatch.setattr(rt, "CONF_PATH", conf_file)

    with pytest.raises(SystemExit):
        rt.cmd_set_tuning(conf, -25, 250)
    text = conf_file.read_text()
    assert "VOLTAGE_OFFSET_MV=0" in text
    assert "POWER_LIMIT_W=210" in text


def test_set_tuning_suspended_never_reads_hwmon_or_od(fake_tree, conf, monkeypatch, tmp_path):
    """PM safety: while suspended, set-tuning must never touch hwmon or
    pp_od_clk_voltage sysfs (that would wake the card)."""
    pci = _suspend(fake_tree)
    _ranges_file(
        tmp_path, monkeypatch,
        '{"cap_min_w": 210, "cap_max_w": 330, "cap_default_w": 300, '
        '"vo_min_mv": -200, "vo_max_mv": 0, "ts": 1}',
    )
    conf_file = tmp_path / "test.conf"
    conf_file.write_text("POWER_LIMIT_W=210\nVOLTAGE_OFFSET_MV=0\n")
    monkeypatch.setattr(rt, "CONF_PATH", conf_file)

    orig_read_text = rt.read_text
    unsafe_reads = []

    def tracked_read_text(path):
        p = str(path)
        if "hwmon" in p or "pp_od_clk_voltage" in p or "mem_info_" in p:
            unsafe_reads.append(p)
        return orig_read_text(path)

    monkeypatch.setattr(rt, "read_text", tracked_read_text)

    assert rt.cmd_set_tuning(conf, -25, 250) == 0
    assert unsafe_reads == []


# ---------------------------------------------------------------------------
# status --json
# ---------------------------------------------------------------------------


def test_status_json_active_schema(fake_tree, conf, capsys):
    result = rt.cmd_status(conf, as_json=True)
    assert result == 0
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert payload["schema_version"] == 1
    assert payload["pci"] == "0000:aa:00.0"
    assert payload["runtime_status"] == "active"
    assert payload["power_state"] == "D0"
    assert payload["control"] == "auto"
    assert payload["tuned"] == {"offset_mv": 0, "cap_w": 210, "profile": None}
    assert payload["live"] is not None
    assert payload["live"]["cap_w"] == 210.0
    assert payload["ranges"]["source"] == "live"
    assert payload["ranges"]["age_s"] == 0
    assert payload["evict_guard"] == {
        "enabled": False, "holding": False, "vram_used": None,
        "gtt_total": None, "margin": conf["EVICT_GUARD_MARGIN"],
    }
    assert "state" in payload


def test_status_json_suspended_uses_cached_ranges(fake_tree, conf, monkeypatch, tmp_path, capsys):
    _suspend(fake_tree)
    _ranges_file(
        tmp_path, monkeypatch,
        '{"cap_min_w": 210, "cap_max_w": 330, "cap_default_w": 300, '
        '"vo_min_mv": -200, "vo_max_mv": 0, "ts": 1}',
    )
    result = rt.cmd_status(conf, as_json=True)
    assert result == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["schema_version"] == 1
    assert payload["runtime_status"] == "suspended"
    assert payload["live"] is None
    assert payload["ranges"]["source"] == "cached"
    assert payload["ranges"]["cap_min_w"] == 210
    assert payload["ranges"]["vo_max_mv"] == 0
    assert payload["ranges"]["age_s"] is not None


def test_status_json_suspended_never_reads_sensors(fake_tree, conf, monkeypatch, tmp_path):
    """PM safety: status --json while suspended must never touch hwmon or
    pp_od_clk_voltage."""
    _suspend(fake_tree)
    _ranges_file(tmp_path, monkeypatch, None)

    orig_read_text = rt.read_text
    unsafe_reads = []

    def tracked_read_text(path):
        p = str(path)
        if "hwmon" in p or "pp_od_clk_voltage" in p or "mem_info_" in p:
            unsafe_reads.append(p)
        return orig_read_text(path)

    monkeypatch.setattr(rt, "read_text", tracked_read_text)

    result = rt.cmd_status(conf, as_json=True)
    assert result == 0
    assert unsafe_reads == []


def test_status_json_includes_evict_guard_holding(fake_tree, monkeypatch, capsys):
    raw = {
        "VENDOR": "0x1002",
        "DEVICE": "0x7551",
        "SUBSYSTEM_VENDOR": "0x1043",
        "SUBSYSTEM_DEVICE": "0x0626",
        "POWER_LIMIT_W": "210",
        "VOLTAGE_OFFSET_MV": "0",
        "POLL_INTERVAL_S": "2",
        "EVICT_GUARD": "1",
        "EVICT_GUARD_MARGIN": "0.90",
    }
    parsed, problems = rt.validate_conf(raw)
    assert not problems

    # status --json runs in its own process: the hold must be read from the
    # daemon's on-disk state file, never from this process's _evict globals
    # (regression from the 2026-09-04 hardware acceptance, t_35c35fc7).
    rt.STATE_DIR.mkdir(parents=True, exist_ok=True)
    (rt.STATE_DIR / "state").write_text(
        "pci=0000:aa:00.0\nstate=ACTIVE_HELD\nvram_used=900000000\n"
        "gtt_total=1000000000\nts=1\n"
    )
    assert rt._evict.holding is False  # sanity: never touched

    result = rt.cmd_status(parsed, as_json=True)
    assert result == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["evict_guard"] == {
        "enabled": True, "holding": True, "vram_used": 900_000_000,
        "gtt_total": 1_000_000_000, "margin": 0.90,
    }


# ---------------------------------------------------------------------------
# cmd_reset exit codes
# ---------------------------------------------------------------------------


def test_reset_returns_zero_on_full_success(fake_tree, conf, monkeypatch):
    pci = fake_tree / "0000:aa:00.0"
    od_path = pci / "pp_od_clk_voltage"

    def fake_write_text(path, value):
        if str(path) == str(od_path) and value == "r\n":
            od_path.write_text("OD_VDDGFX_OFFSET:\n0mV\nOD_RANGE:\nVDDGFX_OFFSET: -200mV 0mV\n")

    monkeypatch.setattr(rt, "write_text", fake_write_text)
    assert rt.cmd_reset(conf) == 0


def test_reset_returns_nonzero_when_cap_reset_fails(fake_tree, conf, monkeypatch, log_recorder):
    pci = fake_tree / "0000:aa:00.0"
    hwmon = pci / "hwmon" / "hwmon7"

    orig_write_text = rt.write_text

    def fake_write_text(path, value):
        if str(path) == str(hwmon / "power1_cap"):
            raise OSError("EBUSY simulated")
        return orig_write_text(path, value)

    monkeypatch.setattr(rt, "write_text", fake_write_text)
    result = rt.cmd_reset(conf)
    assert result == 1
    assert any("power cap reset failed" in m for _, m in log_recorder)


def test_reset_returns_nonzero_when_od_reset_fails(fake_tree, conf, monkeypatch, log_recorder):
    pci = fake_tree / "0000:aa:00.0"
    od_path = pci / "pp_od_clk_voltage"

    orig_write_text = rt.write_text

    def fake_write_text(path, value):
        if str(path) == str(od_path):
            raise OSError("EBUSY simulated")
        return orig_write_text(path, value)

    monkeypatch.setattr(rt, "write_text", fake_write_text)
    result = rt.cmd_reset(conf)
    assert result == 1
    assert any("OD reset failed" in m for _, m in log_recorder)


def test_reset_returns_nonzero_when_both_fail(fake_tree, conf, monkeypatch, log_recorder):
    def fake_write_text(path, value):
        raise OSError("EBUSY simulated")

    monkeypatch.setattr(rt, "write_text", fake_write_text)
    result = rt.cmd_reset(conf)
    assert result == 1


def test_reset_skipped_when_not_active_returns_zero(fake_tree, conf):
    pci = fake_tree / "0000:aa:00.0"
    (pci / "power" / "runtime_status").write_text("suspended\n")
    assert rt.cmd_reset(conf) == 0


def test_reset_refused_when_pm_disabled_returns_one(fake_tree, conf, monkeypatch):
    pci = fake_tree / "0000:aa:00.0"
    (pci / "power" / "control").write_text("on\n")
    monkeypatch.setattr(rt._evict, "holding", False)
    assert rt.cmd_reset(conf) == 1

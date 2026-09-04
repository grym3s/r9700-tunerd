"""Tests for named tuning profiles (EFFICIENCY/BALANCED/PERFORMANCE).

Uses the same fake sysfs tree as tests/test_unit.py (see conftest.py).
"""
from __future__ import annotations

import json

import pytest

from conftest import rt


# ---------------------------------------------------------------------------
# validate_conf: PROFILE key
# ---------------------------------------------------------------------------


def test_validate_conf_accepts_unset_profile():
    raw = {
        "VENDOR": "0x1002", "DEVICE": "0x7551",
        "SUBSYSTEM_VENDOR": "0x1043", "SUBSYSTEM_DEVICE": "0x0626",
        "POWER_LIMIT_W": "210", "VOLTAGE_OFFSET_MV": "0",
        "POLL_INTERVAL_S": "2", "EVICT_GUARD": "0",
        "EVICT_GUARD_MARGIN": "0.90",
    }
    parsed, problems = rt.validate_conf(raw)
    assert not problems
    assert parsed["PROFILE"] is None


@pytest.mark.parametrize("name", ["EFFICIENCY", "BALANCED", "PERFORMANCE", "CUSTOM"])
def test_validate_conf_accepts_known_profiles(name):
    raw = {
        "VENDOR": "0x1002", "DEVICE": "0x7551",
        "SUBSYSTEM_VENDOR": "0x1043", "SUBSYSTEM_DEVICE": "0x0626",
        "POWER_LIMIT_W": "210", "VOLTAGE_OFFSET_MV": "0",
        "POLL_INTERVAL_S": "2", "EVICT_GUARD": "0",
        "EVICT_GUARD_MARGIN": "0.90", "PROFILE": name,
    }
    parsed, problems = rt.validate_conf(raw)
    assert not problems
    assert parsed["PROFILE"] == name


def test_validate_conf_rejects_unknown_profile():
    raw = {
        "VENDOR": "0x1002", "DEVICE": "0x7551",
        "SUBSYSTEM_VENDOR": "0x1043", "SUBSYSTEM_DEVICE": "0x0626",
        "POWER_LIMIT_W": "210", "VOLTAGE_OFFSET_MV": "0",
        "POLL_INTERVAL_S": "2", "EVICT_GUARD": "0",
        "EVICT_GUARD_MARGIN": "0.90", "PROFILE": "ULTRA",
    }
    parsed, problems = rt.validate_conf(raw)
    assert parsed["PROFILE"] is None
    assert any("PROFILE" in p for p in problems)


# ---------------------------------------------------------------------------
# resolve_profile: each documented profile resolves to its documented pair
# ---------------------------------------------------------------------------


def test_resolve_profile_efficiency():
    assert rt.resolve_profile("EFFICIENCY") == (-50, 210)


def test_resolve_profile_balanced():
    assert rt.resolve_profile("BALANCED") == (-25, 210)


def test_resolve_profile_performance():
    assert rt.resolve_profile("PERFORMANCE") == (-50, 210)


def test_resolve_profile_unknown_dies():
    with pytest.raises(SystemExit):
        rt.resolve_profile("ULTRA")


# ---------------------------------------------------------------------------
# cmd_set_profile: goes through set-tuning's atomic/validated path
# ---------------------------------------------------------------------------


def _setup_active_tuning(fake_tree, tmp_path, monkeypatch):
    pci = fake_tree / "0000:aa:00.0"
    hwmon = pci / "hwmon" / "hwmon7"
    (hwmon / "power1_cap_min").write_text("210000000\n")
    (hwmon / "power1_cap_max").write_text("330000000\n")
    (hwmon / "power1_cap").write_text("210000000\n")
    (hwmon / "power1_cap_default").write_text("300000000\n")

    conf_file = tmp_path / "test.conf"
    conf_file.write_text("POWER_LIMIT_W=210\nVOLTAGE_OFFSET_MV=0\nPROFILE=\n")
    monkeypatch.setattr(rt, "CONF_PATH", conf_file)

    od_path = pci / "pp_od_clk_voltage"

    def fake_write_text(path, value):
        if "pp_od_clk_voltage" in str(path) and value.startswith("vo "):
            mv = value.strip().split()[1]
            od_path.write_text(
                f"OD_VDDGFX_OFFSET:\n{mv}mV\nOD_RANGE:\nVDDGFX_OFFSET: -200mV 0mV\n"
            )
        if str(path).endswith("power1_cap"):
            (hwmon / "power1_cap").write_text(value)

    monkeypatch.setattr(rt, "write_text", fake_write_text)
    return conf_file


def test_set_profile_efficiency_applies_and_stamps_config(fake_tree, conf, monkeypatch, tmp_path):
    conf_file = _setup_active_tuning(fake_tree, tmp_path, monkeypatch)
    result = rt.cmd_set_profile(conf, "EFFICIENCY")
    assert result == 0
    text = conf_file.read_text(encoding="utf-8")
    assert "VOLTAGE_OFFSET_MV=-50" in text
    assert "POWER_LIMIT_W=210" in text
    assert "PROFILE=EFFICIENCY" in text
    assert conf["PROFILE"] == "EFFICIENCY"


def test_set_profile_unknown_name_rejected_before_mutation(fake_tree, conf, monkeypatch, tmp_path):
    conf_file = _setup_active_tuning(fake_tree, tmp_path, monkeypatch)
    before = conf_file.read_text(encoding="utf-8")
    with pytest.raises(SystemExit):
        rt.cmd_set_profile(conf, "ULTRA")
    after = conf_file.read_text(encoding="utf-8")
    assert before == after


def test_set_profile_out_of_range_rejected_prior_config_intact(fake_tree, conf, monkeypatch, tmp_path):
    """A profile pair outside the live range must be rejected, and the
    prior config left untouched — same guarantee as set-tuning."""
    pci = fake_tree / "0000:aa:00.0"
    hwmon = pci / "hwmon" / "hwmon7"
    # Live cap range excludes 210 W entirely.
    (hwmon / "power1_cap_min").write_text("250000000\n")
    (hwmon / "power1_cap_max").write_text("330000000\n")

    conf_file = tmp_path / "test.conf"
    conf_file.write_text("POWER_LIMIT_W=210\nVOLTAGE_OFFSET_MV=0\nPROFILE=\n")
    monkeypatch.setattr(rt, "CONF_PATH", conf_file)

    with pytest.raises(SystemExit):
        rt.cmd_set_profile(conf, "BALANCED")

    text = conf_file.read_text(encoding="utf-8")
    assert "VOLTAGE_OFFSET_MV=0" in text
    assert "POWER_LIMIT_W=210" in text
    assert "PROFILE=EFFICIENCY" not in text
    assert "PROFILE=BALANCED" not in text


# ---------------------------------------------------------------------------
# set-tuning with explicit values flips PROFILE to CUSTOM
# ---------------------------------------------------------------------------


def test_set_tuning_explicit_sets_profile_custom(fake_tree, conf, monkeypatch, tmp_path):
    conf_file = _setup_active_tuning(fake_tree, tmp_path, monkeypatch)
    result = rt.cmd_set_tuning(conf, -25, 250)
    assert result == 0
    text = conf_file.read_text(encoding="utf-8")
    assert "PROFILE=CUSTOM" in text
    assert conf["PROFILE"] == "CUSTOM"


def test_set_tuning_restores_prior_profile_on_apply_failure(fake_tree, conf, monkeypatch, tmp_path):
    conf_file = _setup_active_tuning(fake_tree, tmp_path, monkeypatch)
    # Seed config with a named profile already active.
    conf_file.write_text("POWER_LIMIT_W=210\nVOLTAGE_OFFSET_MV=-25\nPROFILE=BALANCED\n")
    conf["POWER_LIMIT_W"] = 210
    conf["VOLTAGE_OFFSET_MV"] = -25
    conf["PROFILE"] = "BALANCED"

    monkeypatch.setattr(rt, "cmd_apply", lambda c: 1)

    result = rt.cmd_set_tuning(conf, -50, 250)
    assert result == 1

    text = conf_file.read_text(encoding="utf-8")
    assert "PROFILE=BALANCED" in text
    assert "PROFILE=CUSTOM" not in text
    assert conf["PROFILE"] == "BALANCED"


# ---------------------------------------------------------------------------
# status --json carries tuned.profile
# ---------------------------------------------------------------------------


def test_status_json_carries_profile(fake_tree, monkeypatch, capsys):
    raw = {
        "VENDOR": "0x1002", "DEVICE": "0x7551",
        "SUBSYSTEM_VENDOR": "0x1043", "SUBSYSTEM_DEVICE": "0x0626",
        "POWER_LIMIT_W": "210", "VOLTAGE_OFFSET_MV": "-50",
        "POLL_INTERVAL_S": "2", "EVICT_GUARD": "0",
        "EVICT_GUARD_MARGIN": "0.90", "PROFILE": "EFFICIENCY",
    }
    parsed, problems = rt.validate_conf(raw)
    assert not problems
    result = rt.cmd_status(parsed, as_json=True)
    assert result == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["schema_version"] == 1
    assert payload["tuned"]["profile"] == "EFFICIENCY"


# ---------------------------------------------------------------------------
# list-profiles
# ---------------------------------------------------------------------------


def test_list_profiles_json_reports_all_and_active(conf, capsys):
    conf = dict(conf)
    conf["PROFILE"] = "BALANCED"
    result = rt.cmd_list_profiles(conf, as_json=True)
    assert result == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["active"] == "BALANCED"
    names = {row["name"] for row in payload["profiles"]}
    assert names == {"EFFICIENCY", "BALANCED", "PERFORMANCE"}
    active_rows = [row for row in payload["profiles"] if row["active"]]
    assert [row["name"] for row in active_rows] == ["BALANCED"]
    for row in payload["profiles"]:
        assert "source" in row and row["source"]
        assert isinstance(row["offset_mv"], int)
        assert isinstance(row["cap_w"], int)


def test_list_profiles_text_output(conf, capsys):
    result = rt.cmd_list_profiles(conf, as_json=False)
    assert result == 0
    out = capsys.readouterr().out
    assert "EFFICIENCY" in out
    assert "BALANCED" in out
    assert "PERFORMANCE" in out

"""Unit tests for r9700-tunerd — no hardware, no root, no /sys access."""
from __future__ import annotations

import errno
import os
import syslog
from pathlib import Path

import pytest

import rt  # loaded by conftest.py


# ===================================================================
# parse_od
# ===================================================================


def test_parse_od_normal():
    text = "OD_VDDGFX_OFFSET:\n0mV\nOD_RANGE:\nVDDGFX_OFFSET: -200mV 0mV\n"
    r = rt.parse_od(text)
    assert r["current_mv"] == 0
    assert r["min_mv"] == -200
    assert r["max_mv"] == 0
    assert r["raw"] == text


def test_parse_od_missing_range():
    text = "OD_VDDGFX_OFFSET:\n-25mV\n"
    r = rt.parse_od(text)
    assert r["current_mv"] == -25
    assert r["min_mv"] is None
    assert r["max_mv"] is None


def test_parse_od_empty():
    r = rt.parse_od("")
    assert r["current_mv"] is None
    assert r["min_mv"] is None
    assert r["max_mv"] is None


def test_parse_od_value_on_header_line():
    """One-line form: OD_VDDGFX_OFFSET: -25mV on the header itself."""
    text = "OD_VDDGFX_OFFSET: -25mV\nOD_RANGE:\nVDDGFX_OFFSET: -200mV 0mV"
    r = rt.parse_od(text)
    assert r["current_mv"] == -25
    assert r["min_mv"] == -200
    assert r["max_mv"] == 0


# ===================================================================
# load_conf
# ===================================================================


def test_load_conf_defaults(tmp_path):
    p = tmp_path / "does_not_exist.conf"
    result = rt.load_conf(p)
    assert result == rt.DEFAULTS


def test_load_conf_overrides_and_comments(tmp_path):
    p = tmp_path / "test.conf"
    p.write_text(
        "# full-line comment\n"
        "VOLTAGE_OFFSET_MV=-25  # inline comment\n"
        "POWER_LIMIT_W=250\n"
        "\n"
        "   \n"
    )
    result = rt.load_conf(p)
    assert result["VOLTAGE_OFFSET_MV"] == "-25"
    assert result["POWER_LIMIT_W"] == "250"
    # Unset keys retain defaults
    assert result["VENDOR"] == "0x1002"
    assert result["DEVICE"] == "0x7551"


# ===================================================================
# validate_conf
# ===================================================================


def test_validate_conf_ok():
    raw = dict(rt.DEFAULTS)
    parsed, problems = rt.validate_conf(raw)
    assert problems == []
    assert parsed["VENDOR"] == "0x1002"
    assert parsed["DEVICE"] == "0x7551"
    assert parsed["POWER_LIMIT_W"] == 210
    assert parsed["VOLTAGE_OFFSET_MV"] == 0
    assert parsed["POLL_INTERVAL_S"] == 2.0


def test_validate_conf_bad_offset():
    raw = dict(rt.DEFAULTS)
    raw["VOLTAGE_OFFSET_MV"] = "100"  # positive → out of range -500..0
    parsed, problems = rt.validate_conf(raw)
    assert any("VOLTAGE_OFFSET_MV" in p for p in problems)
    assert parsed["VOLTAGE_OFFSET_MV"] is None


def test_validate_conf_out_of_range_cap():
    raw = dict(rt.DEFAULTS)
    raw["POWER_LIMIT_W"] = "5"  # below 50
    parsed, problems = rt.validate_conf(raw)
    assert any("POWER_LIMIT_W" in p for p in problems)
    assert parsed["POWER_LIMIT_W"] is None


def test_validate_conf_unknown_key_is_warning_only():
    raw = dict(rt.DEFAULTS)
    raw["MY_CUSTOM_KEY"] = "hello"
    parsed, problems = rt.validate_conf(raw)
    assert any("unknown key" in p for p in problems)
    # All known keys still validate correctly
    assert parsed["VENDOR"] == "0x1002"
    assert parsed["POWER_LIMIT_W"] == 210
    assert parsed["VOLTAGE_OFFSET_MV"] == 0


def test_validate_conf_bad_hex_identity():
    raw = dict(rt.DEFAULTS)
    raw["VENDOR"] = "0x12345"  # five hex digits, not four
    parsed, problems = rt.validate_conf(raw)
    assert any("VENDOR" in p for p in problems)
    assert parsed["VENDOR"] is None


# ===================================================================
# discover
# ===================================================================


def test_discover_one_match(fake_tree, conf):
    result = rt.discover(conf)
    assert result is not None
    assert result.name == "0000:aa:00.0"


def test_discover_zero_match_required_exits(fake_tree, conf, log_recorder):
    conf["VENDOR"] = "0x9999"  # no device matches
    with pytest.raises(SystemExit):
        rt.discover(conf, required=True)


def test_discover_two_matches_returns_none(fake_tree, conf):
    # Create a duplicate R9700 so two devices share the same identity.
    dup = fake_tree / "0000:cc:00.0"
    dup.mkdir()
    (dup / "vendor").write_text("0x1002\n")
    (dup / "device").write_text("0x7551\n")
    (dup / "subsystem_vendor").write_text("0x1043\n")
    (dup / "subsystem_device").write_text("0x0626\n")
    result = rt.discover(conf, required=False)
    assert result is None


# ===================================================================
# od_attempts
# ===================================================================


def test_od_attempts_first_try(fake_tree, conf):
    pci = fake_tree / "0000:aa:00.0"
    calls: list[int] = []

    def fn():
        calls.append(1)
        return "ok"

    result = rt.od_attempts(pci, fn, settle=True)
    assert result == "ok"
    assert len(calls) == 1


def test_od_attempts_transient_then_success(fake_tree, conf):
    pci = fake_tree / "0000:aa:00.0"
    calls: list[int] = []

    def fn():
        calls.append(1)
        if len(calls) <= 2:
            raise OSError(errno.EBUSY, "Device or resource busy")
        return "ok"

    result = rt.od_attempts(pci, fn, settle=True)
    assert result == "ok"
    assert len(calls) == 3  # two EBUSY + one success


def test_od_attempts_exhausted_raises(fake_tree, conf):
    pci = fake_tree / "0000:aa:00.0"
    calls: list[int] = []

    def fn():
        calls.append(1)
        raise OSError(errno.EBUSY, "Device or resource busy")

    with pytest.raises(OSError) as exc_info:
        rt.od_attempts(pci, fn, settle=True)
    assert exc_info.value.errno == errno.EBUSY
    assert len(calls) == 4  # 1 initial + 3 retries


def test_od_attempts_non_transient_reraises(fake_tree, conf):
    pci = fake_tree / "0000:aa:00.0"
    calls: list[int] = []

    def fn():
        calls.append(1)
        raise ValueError("not a transient error")

    with pytest.raises(ValueError, match="not a transient error"):
        rt.od_attempts(pci, fn, settle=True)
    assert len(calls) == 1  # no retry for non-transient


def test_od_attempts_inactive_raises(fake_tree, conf):
    pci = fake_tree / "0000:aa:00.0"
    # Flip runtime_status to suspended
    (pci / "power" / "runtime_status").write_text("suspended\n")
    with pytest.raises(RuntimeError, match="runtime_status"):
        rt.od_attempts(pci, lambda: "ok", settle=True)


# ===================================================================
# restore_voltage
# ===================================================================


def test_restore_voltage_refuses_without_range(fake_tree, conf):
    pci = fake_tree / "0000:aa:00.0"
    # OD text with no OD_RANGE section
    (pci / "pp_od_clk_voltage").write_text("OD_VDDGFX_OFFSET:\n0mV\n")
    conf["VOLTAGE_OFFSET_MV"] = -25
    with pytest.raises(RuntimeError, match="OD_RANGE"):
        rt.restore_voltage(pci, conf)


def test_restore_voltage_out_of_range(fake_tree, conf):
    pci = fake_tree / "0000:aa:00.0"
    # Range is -200..0; request -300
    conf["VOLTAGE_OFFSET_MV"] = -300
    with pytest.raises(RuntimeError, match="outside live range"):
        rt.restore_voltage(pci, conf)


def test_restore_voltage_writes_vo_then_commit(fake_tree, conf, monkeypatch):
    pci = fake_tree / "0000:aa:00.0"
    od_path = pci / "pp_od_clk_voltage"
    # Initial state: offset at 0 mV
    od_path.write_text(
        "OD_VDDGFX_OFFSET:\n0mV\nOD_RANGE:\nVDDGFX_OFFSET: -200mV 0mV\n"
    )
    conf["VOLTAGE_OFFSET_MV"] = -25

    writes: list[tuple[str, str]] = []

    def fake_write_text(path, value):
        writes.append((str(path), value))
        # Simulate sysfs: "vo -25" updates the reported offset
        if "pp_od_clk_voltage" in str(path) and value == "vo -25\n":
            od_path.write_text(
                "OD_VDDGFX_OFFSET:\n-25mV\nOD_RANGE:\nVDDGFX_OFFSET: -200mV 0mV\n"
            )

    monkeypatch.setattr(rt, "write_text", fake_write_text)

    result = rt.restore_voltage(pci, conf)
    assert result == "restored -25"

    # Only the two OD writes should appear
    vo_writes = [v for p, v in writes if "pp_od_clk_voltage" in p]
    assert vo_writes == ["vo -25\n", "c\n"]


# ===================================================================
# require_runtime_pm / handle_wake guards
# ===================================================================


def test_require_runtime_pm_refuses_control_on(fake_tree):
    igpu = fake_tree / "0000:bb:00.0"
    with pytest.raises(RuntimeError, match="control=on"):
        rt.require_runtime_pm(igpu)


def test_handle_wake_skips_when_control_on(
    fake_tree, conf, log_recorder, monkeypatch
):
    igpu = fake_tree / "0000:bb:00.0"
    rt._last_pm_refusal = None

    writes: list[tuple[str, str]] = []
    monkeypatch.setattr(
        rt, "write_text", lambda p, v: writes.append((str(p), v))
    )

    result = rt.handle_wake(igpu, conf, False)
    assert result is False  # cap_checked unchanged
    assert len(writes) == 0  # no sysfs writes

    warnings = [msg for level, msg in log_recorder if level == syslog.LOG_WARNING]
    assert len(warnings) == 1
    assert "control=on" in warnings[0]


def test_handle_wake_restores_offset(
    fake_tree, conf, log_recorder, monkeypatch
):
    pci = fake_tree / "0000:aa:00.0"
    od_path = pci / "pp_od_clk_voltage"
    od_path.write_text(
        "OD_VDDGFX_OFFSET:\n0mV\nOD_RANGE:\nVDDGFX_OFFSET: -200mV 0mV\n"
    )
    conf["VOLTAGE_OFFSET_MV"] = -25
    rt._last_pm_refusal = None
    rt._last_kernel_scan = 0.0

    writes: list[tuple[str, str]] = []

    def fake_write_text(path, value):
        writes.append((str(path), value))
        if "pp_od_clk_voltage" in str(path) and value == "vo -25\n":
            od_path.write_text(
                "OD_VDDGFX_OFFSET:\n-25mV\nOD_RANGE:\nVDDGFX_OFFSET: -200mV 0mV\n"
            )

    monkeypatch.setattr(rt, "write_text", fake_write_text)

    result = rt.handle_wake(pci, conf, False)
    assert result is True  # cap_checked set to True

    vo_writes = [v for p, v in writes if "pp_od_clk_voltage" in p]
    assert "vo -25\n" in vo_writes
    assert "c\n" in vo_writes

    all_msgs = [msg for _, msg in log_recorder]
    assert any("VDDGFX offset restored" in m for m in all_msgs)


# ===================================================================
# require_runtime_pm retry / refusal logging (Patch 1c)
# ===================================================================


def test_check_runtime_pm_auto_does_not_suppress_later_warning(
    fake_tree, conf, log_recorder, monkeypatch
):
    """control=auto at startup must NOT suppress a later refusal warning."""
    pci = fake_tree / "0000:aa:00.0"
    rt._last_pm_refusal = None

    # Startup check: control is "auto" → no warning, no flag set.
    rt.check_runtime_pm(pci)
    assert rt._last_pm_refusal is None

    # Now flip control to "on" (simulating operator change).
    (pci / "power" / "control").write_text("on\n")

    # Monkeypatch restore_voltage to raise if called (it must not be).
    def _boom(*a, **kw):
        raise AssertionError("restore_voltage must not be called when PM refuses")

    monkeypatch.setattr(rt, "restore_voltage", _boom)

    rt.handle_wake(pci, conf, False)

    warnings = [msg for level, msg in log_recorder if level == syslog.LOG_WARNING]
    assert any("control=on" in w for w in warnings)


def test_handle_wake_pm_refusal_logged_once_then_recovery(
    fake_tree, conf, log_recorder, monkeypatch
):
    """Two refusals → one WARNING; recovery → INFO 'guard OK again' + restore."""
    pci = fake_tree / "0000:aa:00.0"
    rt._last_pm_refusal = None
    rt._last_kernel_scan = 0.0

    # Set control to "on" (refusal state).
    (pci / "power" / "control").write_text("on\n")

    # First refusal.
    rt.handle_wake(pci, conf, False)
    # Second refusal (same message → deduped).
    rt.handle_wake(pci, conf, False)

    warnings = [msg for level, msg in log_recorder if level == syslog.LOG_WARNING]
    control_on_warnings = [w for w in warnings if "control=on" in w]
    assert len(control_on_warnings) == 1

    # Flip back to "auto" → recovery.
    (pci / "power" / "control").write_text("auto\n")

    # OD is already at 0 mV and conf wants 0 → "already 0" path.
    result = rt.handle_wake(pci, conf, False)
    assert result is True  # cap_checked set

    infos = [msg for level, msg in log_recorder if level == syslog.LOG_INFO]
    assert any("guard OK again" in m for m in infos)


def test_require_runtime_pm_retries_on_transient_eio(fake_tree, monkeypatch):
    """Two EIO errors then success → no exception, exactly 3 reads."""
    pci = fake_tree / "0000:aa:00.0"
    calls: list[Path] = []

    def fake_read_text(path):
        calls.append(path)
        if len(calls) <= 2:
            raise OSError(errno.EIO, "eio")
        return "auto"

    monkeypatch.setattr(rt, "read_text", fake_read_text)

    # Should not raise.
    rt.require_runtime_pm(pci)
    assert len(calls) == 3


def test_require_runtime_pm_gives_up_after_three_errors(fake_tree, monkeypatch):
    """Always OSError → RuntimeError with 'unreadable after retries'."""
    pci = fake_tree / "0000:aa:00.0"
    calls: list[Path] = []

    def fake_read_text(path):
        calls.append(path)
        raise OSError(errno.EIO, "eio")

    monkeypatch.setattr(rt, "read_text", fake_read_text)

    with pytest.raises(RuntimeError, match="unreadable after retries"):
        rt.require_runtime_pm(pci)
    assert len(calls) == 3


def test_require_runtime_pm_refuses_on_without_retry(fake_tree, monkeypatch):
    """control=on → immediate RuntimeError after exactly one read."""
    pci = fake_tree / "0000:bb:00.0"  # fixture sets control=on
    calls: list[Path] = []

    def fake_read_text(path):
        calls.append(path)
        return "on"

    monkeypatch.setattr(rt, "read_text", fake_read_text)

    with pytest.raises(RuntimeError, match="control=on"):
        rt.require_runtime_pm(pci)
    assert len(calls) == 1


# ===================================================================
# _atomic_write / set_conf_value
# ===================================================================


def test_atomic_write_replaces_and_keeps_mode(tmp_path):
    p = tmp_path / "target"
    p.write_text("original content")
    os.chmod(p, 0o640)

    rt._atomic_write(p, b"replaced content")

    assert p.read_text(encoding="utf-8") == "replaced content"
    assert p.stat().st_mode & 0o777 == 0o640


def test_set_conf_value_updates_or_appends(tmp_path):
    p = tmp_path / "test.conf"
    p.write_text("VENDOR=0x1002\nPOWER_LIMIT_W=210\n")

    # Update an existing key
    rt.set_conf_value(p, "POWER_LIMIT_W", "250")
    text = p.read_text(encoding="utf-8")
    assert "POWER_LIMIT_W=250" in text
    assert "POWER_LIMIT_W=210" not in text
    assert "VENDOR=0x1002" in text

    # Append a new key
    rt.set_conf_value(p, "VOLTAGE_OFFSET_MV", "-25")
    text = p.read_text(encoding="utf-8")
    assert "VOLTAGE_OFFSET_MV=-25" in text
    assert "VENDOR=0x1002" in text


# ===================================================================
# _stderr_is_journal
# ===================================================================


def test_stderr_is_journal_false_without_env(monkeypatch):
    monkeypatch.delenv("JOURNAL_STREAM", raising=False)
    assert rt._stderr_is_journal() is False


# ===================================================================
# cmd_watch — config reload resilience
# ===================================================================


def test_watch_loop_keeps_last_good_config(
    fake_tree, conf, log_recorder, monkeypatch, tmp_path
):
    """Drive cmd_watch for a few iterations; corrupt the config mid-run.

    Expect exactly one 'config reload failed' warning and a clean exit.
    """
    # --- Config file the watcher will reload each iteration -------------
    conf_file = tmp_path / "watch.conf"
    conf_file.write_text(
        "VENDOR=0x1002\n"
        "DEVICE=0x7551\n"
        "SUBSYSTEM_VENDOR=0x1043\n"
        "SUBSYSTEM_DEVICE=0x0626\n"
        "POWER_LIMIT_W=210\n"
        "VOLTAGE_OFFSET_MV=0\n"
        "POLL_INTERVAL_S=2\n"
    )
    monkeypatch.setattr(rt, "CONF_PATH", conf_file)

    # Reset module-level state
    rt._stop = False
    rt._last_pm_refusal = None
    rt._last_kernel_scan = 0.0

    # --- Instrument time.sleep ------------------------------------------
    # Call 1: od_attempts settle inside initial handle_wake (value 0)
    # Call 2: end of 1st loop iteration  → corrupt config
    # Call 3: end of 2nd loop iteration  → stop the watcher
    sleep_count = 0

    def fake_sleep(*args, **kwargs):
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count == 2:
            # Make the config invalid (VOLTAGE_OFFSET_MV out of range)
            conf_file.write_text("VOLTAGE_OFFSET_MV=999\n")
        elif sleep_count == 3:
            rt._stop = True

    monkeypatch.setattr(rt.time, "sleep", fake_sleep)

    # --- Run -------------------------------------------------------------
    result = rt.cmd_watch(conf)
    assert result == 0  # clean exit, not sys.exit

    # Exactly one "config reload failed" warning (deduplicated by last_warn_text)
    reload_warnings = [
        msg for level, msg in log_recorder if "config reload failed" in msg
    ]
    assert len(reload_warnings) == 1


# ===================================================================
# cmd_watch — counter-based resume detection (Patch 3)
# ===================================================================


def test_watch_detects_resume_without_suspended_sample(
    fake_tree, conf, log_recorder, monkeypatch, tmp_path
):
    """Status stays 'active' the whole time; counter bump triggers a 2nd wake."""
    pci = fake_tree / "0000:aa:00.0"

    conf_file = tmp_path / "watch.conf"
    conf_file.write_text(
        "VENDOR=0x1002\n"
        "DEVICE=0x7551\n"
        "SUBSYSTEM_VENDOR=0x1043\n"
        "SUBSYSTEM_DEVICE=0x0626\n"
        "POWER_LIMIT_W=210\n"
        "VOLTAGE_OFFSET_MV=0\n"
        "POLL_INTERVAL_S=2\n"
    )
    monkeypatch.setattr(rt, "CONF_PATH", conf_file)

    rt._stop = False
    rt._last_pm_refusal = None
    rt._last_kernel_scan = 0.0

    # Monkeypatch handle_wake to count invocations.
    wake_count = 0

    def fake_handle_wake(p, c, cc):
        nonlocal wake_count
        wake_count += 1
        return True

    monkeypatch.setattr(rt, "handle_wake", fake_handle_wake)

    # Drive the loop:
    #   Initial startup: handle_wake (count=1), susp_at_last_wake=1000
    #   Iter 1: active, counter=1000, no change → no wake
    #   sleep 1: bump counter to 2000
    #   Iter 2: active, counter=2000 > 1000 → counter-detected wake (count=2)
    #   sleep 2: stop
    sleep_count = 0

    def fake_sleep(*args, **kwargs):
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count == 1:
            # Simulate a suspend+resume that the poll never witnessed.
            (pci / "power" / "runtime_suspended_time").write_text("2000\n")
        elif sleep_count == 2:
            rt._stop = True

    monkeypatch.setattr(rt.time, "sleep", fake_sleep)

    result = rt.cmd_watch(conf)
    assert result == 0
    assert wake_count == 2  # initial + counter-detected

    # The distinct log line must be present.
    all_msgs = [msg for _, msg in log_recorder]
    assert any(
        "resume detected via runtime_suspended_time" in m for m in all_msgs
    )


def test_watch_no_reapply_when_counter_unchanged(
    fake_tree, conf, log_recorder, monkeypatch, tmp_path
):
    """Status active for several polls, counter constant → exactly one wake."""
    pci = fake_tree / "0000:aa:00.0"

    conf_file = tmp_path / "watch.conf"
    conf_file.write_text(
        "VENDOR=0x1002\n"
        "DEVICE=0x7551\n"
        "SUBSYSTEM_VENDOR=0x1043\n"
        "SUBSYSTEM_DEVICE=0x0626\n"
        "POWER_LIMIT_W=210\n"
        "VOLTAGE_OFFSET_MV=0\n"
        "POLL_INTERVAL_S=2\n"
    )
    monkeypatch.setattr(rt, "CONF_PATH", conf_file)

    rt._stop = False
    rt._last_pm_refusal = None
    rt._last_kernel_scan = 0.0

    wake_count = 0

    def fake_handle_wake(p, c, cc):
        nonlocal wake_count
        wake_count += 1
        return True

    monkeypatch.setattr(rt, "handle_wake", fake_handle_wake)

    # 3 loop iterations; counter stays at 1000 the whole time.
    sleep_count = 0

    def fake_sleep(*args, **kwargs):
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count == 3:
            rt._stop = True

    monkeypatch.setattr(rt.time, "sleep", fake_sleep)

    result = rt.cmd_watch(conf)
    assert result == 0
    assert wake_count == 1  # only the initial startup wake


def test_watch_counter_unreadable_falls_back_to_edges(
    fake_tree, conf, log_recorder, monkeypatch, tmp_path
):
    """Counter file removed; active→suspended→active still triggers two wakes."""
    pci = fake_tree / "0000:aa:00.0"

    # Remove the counter file so runtime_suspended_ms returns None.
    susp_file = pci / "power" / "runtime_suspended_time"
    susp_file.unlink()

    conf_file = tmp_path / "watch.conf"
    conf_file.write_text(
        "VENDOR=0x1002\n"
        "DEVICE=0x7551\n"
        "SUBSYSTEM_VENDOR=0x1043\n"
        "SUBSYSTEM_DEVICE=0x0626\n"
        "POWER_LIMIT_W=210\n"
        "VOLTAGE_OFFSET_MV=0\n"
        "POLL_INTERVAL_S=2\n"
    )
    monkeypatch.setattr(rt, "CONF_PATH", conf_file)

    rt._stop = False
    rt._last_pm_refusal = None
    rt._last_kernel_scan = 0.0

    wake_count = 0

    def fake_handle_wake(p, c, cc):
        nonlocal wake_count
        wake_count += 1
        return True

    monkeypatch.setattr(rt, "handle_wake", fake_handle_wake)

    # Sequence:
    #   Initial: active → wake 1 (startup)
    #   Iter 1: active, state=ACTIVE_CONFIGURED → no wake
    #   sleep 1: set status to suspended
    #   Iter 2: suspended → state=SUSPENDED
    #   sleep 2: set status to active
    #   Iter 3: active + state=SUSPENDED → wake 2 (edge)
    #   sleep 3: stop
    sleep_count = 0

    def fake_sleep(*args, **kwargs):
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count == 1:
            (pci / "power" / "runtime_status").write_text("suspended\n")
        elif sleep_count == 2:
            (pci / "power" / "runtime_status").write_text("active\n")
        elif sleep_count == 3:
            rt._stop = True

    monkeypatch.setattr(rt.time, "sleep", fake_sleep)

    result = rt.cmd_watch(conf)
    assert result == 0
    assert wake_count == 2  # initial + edge-detected


# ===================================================================
# cmd_watch — Patch 3b review fixes
# ===================================================================


def test_counter_none_after_wake_keeps_previous_baseline(
    fake_tree, conf, log_recorder, monkeypatch, tmp_path
):
    """Counter file removed after first wake → baseline kept, one warning,
    later growth still detected when the file returns."""
    pci = fake_tree / "0000:aa:00.0"
    susp_file = pci / "power" / "runtime_suspended_time"

    conf_file = tmp_path / "watch.conf"
    conf_file.write_text(
        "VENDOR=0x1002\n"
        "DEVICE=0x7551\n"
        "SUBSYSTEM_VENDOR=0x1043\n"
        "SUBSYSTEM_DEVICE=0x0626\n"
        "POWER_LIMIT_W=210\n"
        "VOLTAGE_OFFSET_MV=0\n"
        "POLL_INTERVAL_S=2\n"
    )
    monkeypatch.setattr(rt, "CONF_PATH", conf_file)

    rt._stop = False
    rt._last_pm_refusal = None
    rt._last_kernel_scan = 0.0

    wake_count = 0

    def fake_handle_wake(p, c, cc):
        nonlocal wake_count
        wake_count += 1
        return True

    monkeypatch.setattr(rt, "handle_wake", fake_handle_wake)

    # Sequence:
    #   Startup: active → wake 1, susp_at_last_wake=1000, _susp_prev_ok=True
    #   Iter 1: active, state=ACTIVE_CONFIGURED → read counter → None
    #           → keep 1000, log warning (prev was OK), _susp_prev_ok=False
    #   sleep 1: remove counter file (already gone after iter 1 read)
    #   Iter 2: active, state=ACTIVE_CONFIGURED → read counter → 2000 > 1000
    #           → wake 2
    #   sleep 2: stop
    sleep_count = 0

    def fake_sleep(*args, **kwargs):
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count == 1:
            # Remove the counter file so the next poll's read returns None.
            susp_file.unlink()
        elif sleep_count == 2:
            # Restore with a higher value; the NEXT poll must detect growth.
            susp_file.write_text("2000\n")
        elif sleep_count == 3:
            rt._stop = True

    monkeypatch.setattr(rt.time, "sleep", fake_sleep)

    result = rt.cmd_watch(conf)
    assert result == 0
    assert wake_count == 2  # startup + growth-detected

    # Exactly one "unreadable" warning (deduped: only logged on good→None transition)
    unreadable_warnings = [
        msg for level, msg in log_recorder
        if "runtime_suspended_time unreadable" in msg
    ]
    assert len(unreadable_warnings) == 1


def test_counter_regression_treated_as_wake(
    fake_tree, conf, log_recorder, monkeypatch, tmp_path
):
    """baseline 42000, next read 0 → handle_wake once, WARNING contains
    'regressed', baseline becomes 0."""
    pci = fake_tree / "0000:aa:00.0"
    susp_file = pci / "power" / "runtime_suspended_time"
    susp_file.write_text("42000\n")

    conf_file = tmp_path / "watch.conf"
    conf_file.write_text(
        "VENDOR=0x1002\n"
        "DEVICE=0x7551\n"
        "SUBSYSTEM_VENDOR=0x1043\n"
        "SUBSYSTEM_DEVICE=0x0626\n"
        "POWER_LIMIT_W=210\n"
        "VOLTAGE_OFFSET_MV=0\n"
        "POLL_INTERVAL_S=2\n"
    )
    monkeypatch.setattr(rt, "CONF_PATH", conf_file)

    rt._stop = False
    rt._last_pm_refusal = None
    rt._last_kernel_scan = 0.0

    wake_count = 0

    def fake_handle_wake(p, c, cc):
        nonlocal wake_count
        wake_count += 1
        return True

    monkeypatch.setattr(rt, "handle_wake", fake_handle_wake)

    # Sequence:
    #   Startup: active → wake 1, susp_at_last_wake=42000
    #   Iter 1: active, state=ACTIVE_CONFIGURED → counter=42000, no change → no wake
    #   sleep 1: set counter to 0 (simulates driver rebind/reset)
    #   Iter 2: active, state=ACTIVE_CONFIGURED → counter=0 < 42000 → regression!
    #           → WARNING, wake 2, susp_at_last_wake=0
    #   sleep 2: (no-op)
    #   Iter 3: active, state=ACTIVE_CONFIGURED → counter=0, not < 0 → no wake
    #   sleep 3: stop
    sleep_count = 0

    def fake_sleep(*args, **kwargs):
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count == 1:
            susp_file.write_text("0\n")
        elif sleep_count == 3:
            rt._stop = True

    monkeypatch.setattr(rt.time, "sleep", fake_sleep)

    result = rt.cmd_watch(conf)
    assert result == 0
    assert wake_count == 2  # startup + regression-detected

    # WARNING must contain "regressed" and the old→new values.
    regressed_warnings = [
        msg for level, msg in log_recorder if "regressed" in msg
    ]
    assert len(regressed_warnings) == 1
    assert "42000" in regressed_warnings[0]
    assert "0" in regressed_warnings[0]


def test_loop_exception_resets_state(
    fake_tree, conf, log_recorder, monkeypatch, tmp_path
):
    """monkeypatch handle_wake to raise once → next active poll calls
    handle_wake again (state was reset to SUSPENDED by the except handler)."""
    pci = fake_tree / "0000:aa:00.0"

    conf_file = tmp_path / "watch.conf"
    conf_file.write_text(
        "VENDOR=0x1002\n"
        "DEVICE=0x7551\n"
        "SUBSYSTEM_VENDOR=0x1043\n"
        "SUBSYSTEM_DEVICE=0x0626\n"
        "POWER_LIMIT_W=210\n"
        "VOLTAGE_OFFSET_MV=0\n"
        "POLL_INTERVAL_S=2\n"
    )
    monkeypatch.setattr(rt, "CONF_PATH", conf_file)

    rt._stop = False
    rt._last_pm_refusal = None
    rt._last_kernel_scan = 0.0

    wake_calls: list[int] = []

    def fake_handle_wake(p, c, cc):
        wake_calls.append(1)
        if len(wake_calls) == 2:
            raise RuntimeError("simulated failure in wake path")
        return True

    monkeypatch.setattr(rt, "handle_wake", fake_handle_wake)

    # Sequence:
    #   Startup: active → handle_wake (call 1, OK) → state=ACTIVE_CONFIGURED
    #   Iter 1: active, state=ACTIVE_CONFIGURED → no wake → sleep 1 (→ suspended)
    #   Iter 2: suspended → state=SUSPENDED → sleep 2 (→ active)
    #   Iter 3: active, state=SUSPENDED → edge wake → handle_wake (call 2, RAISES)
    #           → except → state=SUSPENDED → sleep 3
    #   Iter 4: active, state=SUSPENDED → edge wake → handle_wake (call 3, OK)
    #           → sleep 4 (stop)
    sleep_count = 0

    def fake_sleep(*args, **kwargs):
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count == 1:
            (pci / "power" / "runtime_status").write_text("suspended\n")
        elif sleep_count == 2:
            (pci / "power" / "runtime_status").write_text("active\n")
        elif sleep_count == 4:
            rt._stop = True

    monkeypatch.setattr(rt.time, "sleep", fake_sleep)

    result = rt.cmd_watch(conf)
    assert result == 0
    assert len(wake_calls) == 3  # startup + failed + retry

    # The exception was logged by the except handler.
    error_msgs = [
        msg for level, msg in log_recorder if "watcher loop error" in msg
    ]
    assert len(error_msgs) == 1
    assert "simulated failure" in error_msgs[0]


def test_hwmon_dir_numeric_sort_and_power1_cap(fake_tree):
    """hwmon12 without power1_cap and hwmon7 with it → hwmon7 chosen.

    Lexical sort would pick hwmon12 first; numeric sort + power1_cap
    preference must pick hwmon7.
    """
    pci = fake_tree / "0000:aa:00.0"
    hroot = pci / "hwmon"

    # hwmon7 already exists in the fixture with power1_cap.
    # Add hwmon12 WITHOUT power1_cap (simulates a stale post-rebind entry).
    hwmon12 = hroot / "hwmon12"
    hwmon12.mkdir()
    (hwmon12 / "temp1_input").write_text("45000\n")  # some sensor, but no power1_cap

    result = rt.hwmon_dir(pci)
    assert result is not None
    assert result.name == "hwmon7"


# ===================================================================
# cmd_set_power_cap
# ===================================================================


def test_set_power_cap_updates_conf_and_applies(fake_tree, conf, monkeypatch, tmp_path):
    """Active card, cap 250 within live range → config updated, cmd_apply called."""
    pci = fake_tree / "0000:aa:00.0"
    hwmon = pci / "hwmon" / "hwmon7"
    (hwmon / "power1_cap_min").write_text("210000000\n")
    (hwmon / "power1_cap_max").write_text("330000000\n")
    (hwmon / "power1_cap").write_text("210000000\n")
    (hwmon / "power1_cap_default").write_text("250000000\n")

    conf_file = tmp_path / "test.conf"
    conf_file.write_text("POWER_LIMIT_W=210\nVOLTAGE_OFFSET_MV=0\n")
    monkeypatch.setattr(rt, "CONF_PATH", conf_file)

    apply_calls: list[dict] = []

    def fake_cmd_apply(c):
        apply_calls.append(dict(c))
        return 0

    monkeypatch.setattr(rt, "cmd_apply", fake_cmd_apply)

    result = rt.cmd_set_power_cap(conf, 250)
    assert result == 0

    # Config file now says 250
    text = conf_file.read_text(encoding="utf-8")
    assert "POWER_LIMIT_W=250" in text

    # cmd_apply was invoked with the updated conf
    assert len(apply_calls) == 1
    assert apply_calls[0]["POWER_LIMIT_W"] == 250


def test_set_power_cap_rejects_out_of_range(fake_tree, conf, monkeypatch, tmp_path):
    """Live range 210..330, request 400 → SystemExit, config unchanged."""
    pci = fake_tree / "0000:aa:00.0"
    hwmon = pci / "hwmon" / "hwmon7"
    (hwmon / "power1_cap_min").write_text("210000000\n")
    (hwmon / "power1_cap_max").write_text("330000000\n")
    (hwmon / "power1_cap").write_text("210000000\n")
    (hwmon / "power1_cap_default").write_text("250000000\n")

    conf_file = tmp_path / "test.conf"
    conf_file.write_text("POWER_LIMIT_W=210\nVOLTAGE_OFFSET_MV=0\n")
    monkeypatch.setattr(rt, "CONF_PATH", conf_file)

    with pytest.raises(SystemExit):
        rt.cmd_set_power_cap(conf, 400)

    # Config unchanged
    text = conf_file.read_text(encoding="utf-8")
    assert "POWER_LIMIT_W=210" in text
    assert "POWER_LIMIT_W=400" not in text

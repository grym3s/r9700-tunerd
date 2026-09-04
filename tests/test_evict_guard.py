"""Unit tests for the evict-guard helpers: drm_vram_in_use, gtt_total_bytes.

Uses the shared ``rt`` module fixture loaded by tests/conftest.py. All
filesystem access is redirected into tmp_path via monkeypatching
rt.PROC_ROOT / rt.MEMINFO_PATH so no real /proc or /sys is touched.
"""
from __future__ import annotations

import os

import pytest

import rt  # loaded by conftest.py

PCI_A = "0000:03:00.0"
PCI_B = "0000:04:00.0"


def _make_fd(proc_root, pid, fd, *, dri_target, fdinfo_text):
    """Create /proc/<pid>/fd/<fd> as a symlink to dri_target plus a
    matching /proc/<pid>/fdinfo/<fd> file with fdinfo_text content."""
    fd_dir = proc_root / str(pid) / "fd"
    fdinfo_dir = proc_root / str(pid) / "fdinfo"
    fd_dir.mkdir(parents=True, exist_ok=True)
    fdinfo_dir.mkdir(parents=True, exist_ok=True)
    # Symlink target need not exist for os.readlink() to work.
    (fd_dir / str(fd)).symlink_to(dri_target)
    (fdinfo_dir / str(fd)).write_text(fdinfo_text)


def _fdinfo(pdev, client_id, total_vram_kib=None, memory_vram_kib=None):
    lines = [f"drm-pdev:\t{pdev}", f"drm-client-id:\t{client_id}"]
    if total_vram_kib is not None:
        lines.append(f"drm-total-vram:\t{total_vram_kib} KiB")
    if memory_vram_kib is not None:
        lines.append(f"drm-memory-vram:\t{memory_vram_kib} KiB")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# drm_vram_in_use
# ---------------------------------------------------------------------------


def test_drm_vram_basic_match(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "PROC_ROOT", tmp_path)
    _make_fd(
        tmp_path, 100, 3,
        dri_target="/dev/dri/renderD128",
        fdinfo_text=_fdinfo(PCI_A, "1", total_vram_kib=1024),
    )
    assert rt.drm_vram_in_use(PCI_A) == 1024 * 1024


def test_drm_vram_filters_wrong_pci_addr(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "PROC_ROOT", tmp_path)
    _make_fd(
        tmp_path, 100, 3,
        dri_target="/dev/dri/renderD128",
        fdinfo_text=_fdinfo(PCI_B, "1", total_vram_kib=1024),
    )
    assert rt.drm_vram_in_use(PCI_A) == 0


def test_drm_vram_ignores_non_dri_fds(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "PROC_ROOT", tmp_path)
    fd_dir = tmp_path / "100" / "fd"
    fdinfo_dir = tmp_path / "100" / "fdinfo"
    fd_dir.mkdir(parents=True)
    fdinfo_dir.mkdir(parents=True)
    (fd_dir / "5").symlink_to("/some/regular/file")
    (fdinfo_dir / "5").write_text(_fdinfo(PCI_A, "1", total_vram_kib=1024))
    assert rt.drm_vram_in_use(PCI_A) == 0


def test_drm_vram_dedupes_by_client_id_takes_max(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "PROC_ROOT", tmp_path)
    # Same client-id opened on two different fds (e.g. two threads);
    # only the max drm-total-vram should be counted, not the sum.
    _make_fd(
        tmp_path, 100, 3,
        dri_target="/dev/dri/renderD128",
        fdinfo_text=_fdinfo(PCI_A, "42", total_vram_kib=500),
    )
    _make_fd(
        tmp_path, 100, 4,
        dri_target="/dev/dri/renderD128",
        fdinfo_text=_fdinfo(PCI_A, "42", total_vram_kib=2000),
    )
    assert rt.drm_vram_in_use(PCI_A) == 2000 * 1024


def test_drm_vram_sums_distinct_clients(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "PROC_ROOT", tmp_path)
    _make_fd(
        tmp_path, 100, 3,
        dri_target="/dev/dri/renderD128",
        fdinfo_text=_fdinfo(PCI_A, "1", total_vram_kib=500),
    )
    _make_fd(
        tmp_path, 200, 3,
        dri_target="/dev/dri/renderD128",
        fdinfo_text=_fdinfo(PCI_A, "2", total_vram_kib=1500),
    )
    assert rt.drm_vram_in_use(PCI_A) == 2000 * 1024


def test_drm_vram_falls_back_to_memory_vram(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "PROC_ROOT", tmp_path)
    _make_fd(
        tmp_path, 100, 3,
        dri_target="/dev/dri/renderD128",
        fdinfo_text=_fdinfo(PCI_A, "1", memory_vram_kib=777),
    )
    assert rt.drm_vram_in_use(PCI_A) == 777 * 1024


def test_drm_vram_prefers_total_vram_over_memory_vram(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "PROC_ROOT", tmp_path)
    _make_fd(
        tmp_path, 100, 3,
        dri_target="/dev/dri/renderD128",
        fdinfo_text=_fdinfo(PCI_A, "1", total_vram_kib=900, memory_vram_kib=100),
    )
    assert rt.drm_vram_in_use(PCI_A) == 900 * 1024


def test_drm_vram_returns_zero_when_no_matches(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "PROC_ROOT", tmp_path)
    assert rt.drm_vram_in_use(PCI_A) == 0


def test_drm_vram_skips_non_pid_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "PROC_ROOT", tmp_path)
    (tmp_path / "self").mkdir()
    (tmp_path / "cpuinfo").write_text("")
    assert rt.drm_vram_in_use(PCI_A) == 0


def test_drm_vram_handles_broken_symlink_gracefully(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "PROC_ROOT", tmp_path)
    fd_dir = tmp_path / "100" / "fd"
    fd_dir.mkdir(parents=True)
    # Symlink to a target within a pid dir that has since vanished; the
    # fdinfo file is simply missing -> should be skipped, not raise.
    (fd_dir / "5").symlink_to("/dev/dri/renderD128")
    assert rt.drm_vram_in_use(PCI_A) == 0


# ---------------------------------------------------------------------------
# gtt_total_bytes
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_gtt_cache(monkeypatch):
    monkeypatch.setattr(rt, "_gtt_cache", {})
    monkeypatch.setattr(rt, "_gtt_fallback_warned", False)


def test_gtt_reads_sysfs_value(tmp_path):
    pci = tmp_path / "0000:03:00.0"
    pci.mkdir()
    (pci / "mem_info_gtt_total").write_text("34359738368\n")
    assert rt.gtt_total_bytes(pci) == 34359738368


def test_gtt_caches_result(tmp_path):
    pci = tmp_path / "0000:03:00.0"
    pci.mkdir()
    gtt_file = pci / "mem_info_gtt_total"
    gtt_file.write_text("1000\n")
    first = rt.gtt_total_bytes(pci)
    assert first == 1000
    # Mutate the file after the first read; cached value must not change.
    gtt_file.write_text("2000\n")
    assert rt.gtt_total_bytes(pci) == 1000


def test_gtt_falls_back_to_half_memtotal(tmp_path, monkeypatch):
    pci = tmp_path / "0000:03:00.0"
    pci.mkdir()  # no mem_info_gtt_total file present
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:       65856856 kB\nMemFree:        1234 kB\n")
    monkeypatch.setattr(rt, "MEMINFO_PATH", meminfo)
    expected = (65856856 * 1024) // 2
    assert rt.gtt_total_bytes(pci) == expected


def test_gtt_fallback_warns_once(tmp_path, monkeypatch):
    pci = tmp_path / "0000:03:00.0"
    pci.mkdir()
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:       1000 kB\n")
    monkeypatch.setattr(rt, "MEMINFO_PATH", meminfo)

    records = []
    monkeypatch.setattr(rt, "log", lambda msg, level=rt.syslog.LOG_INFO: records.append((level, msg)))

    pci2 = tmp_path / "0000:04:00.0"
    pci2.mkdir()

    rt.gtt_total_bytes(pci)
    rt.gtt_total_bytes(pci)  # cached, no new warning
    rt.gtt_total_bytes(pci2)  # different key, would fall back again but warn only once total

    warnings = [r for r in records if r[0] == rt.syslog.LOG_WARNING]
    assert len(warnings) == 1


# ---------------------------------------------------------------------------
# Hold/release logic in cmd_watch + require_runtime_pm interaction
# ---------------------------------------------------------------------------


def _watch_conf_text(*, evict_guard=True, margin=0.5):
    return (
        "VENDOR=0x1002\n"
        "DEVICE=0x7551\n"
        "SUBSYSTEM_VENDOR=0x1043\n"
        "SUBSYSTEM_DEVICE=0x0626\n"
        "POWER_LIMIT_W=210\n"
        "VOLTAGE_OFFSET_MV=0\n"
        "POLL_INTERVAL_S=2\n"
        f"EVICT_GUARD={1 if evict_guard else 0}\n"
        f"EVICT_GUARD_MARGIN={margin}\n"
    )


@pytest.fixture(autouse=True)
def _reset_evict_state(monkeypatch):
    """Isolate the module-level _evict singleton between tests."""
    fresh = rt._EvictGuardState()
    monkeypatch.setattr(rt, "_evict", fresh)
    yield


def _run_one_watch_iteration(conf, monkeypatch, tmp_path, *, evict_guard=True, margin=0.5):
    """Drive cmd_watch through exactly one polling-loop iteration, then stop."""
    conf_file = tmp_path / "watch.conf"
    conf_file.write_text(_watch_conf_text(evict_guard=evict_guard, margin=margin))
    monkeypatch.setattr(rt, "CONF_PATH", conf_file)

    rt._stop = False
    rt._last_pm_refusal = None
    rt._last_kernel_scan = 0.0

    # handle_wake is a no-op here — status is already "active" so the
    # initial pre-loop block runs it once; keep it harmless & cheap.
    monkeypatch.setattr(rt, "handle_wake", lambda p, c, cc: True)

    sleep_count = 0

    def fake_sleep(*args, **kwargs):
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count == 1:
            rt._stop = True

    monkeypatch.setattr(rt.time, "sleep", fake_sleep)
    result = rt.cmd_watch(conf)
    assert result == 0


def test_evict_guard_hold_trigger(fake_tree, conf, monkeypatch, tmp_path, log_recorder):
    pci = fake_tree / "0000:aa:00.0"
    (pci / "mem_info_gtt_total").write_text("1000\n")  # gtt = 1000 bytes
    # margin=0.5 -> hold threshold = 500 bytes; VRAM well above it.
    monkeypatch.setattr(rt, "drm_vram_in_use", lambda pci_addr: 900)

    _run_one_watch_iteration(conf, monkeypatch, tmp_path, margin=0.5)

    # The in-loop trigger fired (hold acquired, state written ACTIVE_HELD)
    # even though the harness's single-iteration stop then runs the new
    # shutdown-release path below, which is why the final on-disk state is
    # "auto"/ACTIVE_CONFIGURED rather than "on"/ACTIVE_HELD — that release
    # is asserted explicitly, not just implied by omission.
    hold_logs = [m for lvl, m in log_recorder if "holding dGPU awake" in m]
    assert len(hold_logs) == 1
    assert rt._evict.last_vram == 900
    assert rt._evict.last_gtt == 1000
    shutdown_logs = [m for lvl, m in log_recorder if "released hold on shutdown" in m]
    assert len(shutdown_logs) == 1
    assert (pci / "power" / "control").read_text().strip() == "auto"
    assert rt._evict.holding is False
    state_text = (rt.STATE_DIR / "state").read_text()
    assert "state=ACTIVE_CONFIGURED" in state_text


def test_evict_guard_release_trigger(fake_tree, conf, monkeypatch, tmp_path):
    pci = fake_tree / "0000:aa:00.0"
    (pci / "mem_info_gtt_total").write_text("1000\n")
    (pci / "power" / "control").write_text("on\n")
    rt._evict.holding = True
    # margin=0.5 -> release threshold = 0.8*0.5*1000 = 400; VRAM well below it.
    monkeypatch.setattr(rt, "drm_vram_in_use", lambda pci_addr: 100)

    _run_one_watch_iteration(conf, monkeypatch, tmp_path, margin=0.5)

    assert (pci / "power" / "control").read_text().strip().startswith("auto")
    assert rt._evict.holding is False
    state_text = (rt.STATE_DIR / "state").read_text()
    assert "state=ACTIVE_CONFIGURED" in state_text


def test_evict_guard_hysteresis_no_release(fake_tree, conf, monkeypatch, tmp_path, log_recorder):
    """VRAM between 0.8*margin*gtt and margin*gtt must NOT release while holding.

    The harness stops the loop after this single iteration, which now also
    triggers the (separate, card-c) shutdown-release path — so the final
    on-disk control value is "auto" regardless. What this test actually
    verifies is that the in-loop hysteresis branch itself did not fire: no
    "released hold — VRAM ..." log (the loop's own release message, as
    opposed to "released hold on shutdown").
    """
    pci = fake_tree / "0000:aa:00.0"
    (pci / "mem_info_gtt_total").write_text("1000\n")
    (pci / "power" / "control").write_text("on\n")
    rt._evict.holding = True
    # margin=0.5 -> hold=500, release=400; pick 450 (in the hysteresis band).
    monkeypatch.setattr(rt, "drm_vram_in_use", lambda pci_addr: 450)

    _run_one_watch_iteration(conf, monkeypatch, tmp_path, margin=0.5)

    in_loop_release_logs = [
        m for lvl, m in log_recorder if m.startswith("evict-guard: released hold —")
    ]
    assert in_loop_release_logs == []
    shutdown_logs = [m for lvl, m in log_recorder if "released hold on shutdown" in m]
    assert len(shutdown_logs) == 1


def test_require_runtime_pm_passes_when_guard_holding(fake_tree, conf):
    pci = fake_tree / "0000:aa:00.0"
    (pci / "power" / "control").write_text("on\n")
    rt._evict.holding = True
    rt.require_runtime_pm(pci)  # must not raise


def test_require_runtime_pm_still_refuses_when_not_holding(fake_tree, conf):
    pci = fake_tree / "0000:aa:00.0"
    (pci / "power" / "control").write_text("on\n")
    rt._evict.holding = False
    with pytest.raises(RuntimeError):
        rt.require_runtime_pm(pci)


def test_evict_guard_never_reads_vram_gtt_while_not_active(
    fake_tree, conf, monkeypatch, tmp_path
):
    """VRAM/GTT reads must only happen on an 'active' poll, never otherwise."""
    pci = fake_tree / "0000:aa:00.0"
    (pci / "power" / "runtime_status").write_text("suspended\n")
    (pci / "mem_info_gtt_total").write_text("1000\n")

    calls = {"vram": 0, "gtt": 0}

    def _tracked_vram(pci_addr):
        calls["vram"] += 1
        return 0

    def _tracked_gtt(p):
        calls["gtt"] += 1
        return 1000

    monkeypatch.setattr(rt, "drm_vram_in_use", _tracked_vram)
    monkeypatch.setattr(rt, "gtt_total_bytes", _tracked_gtt)

    _run_one_watch_iteration(conf, monkeypatch, tmp_path, margin=0.5)

    assert calls["vram"] == 0
    assert calls["gtt"] == 0


# ---------------------------------------------------------------------------
# Startup adoption
# ---------------------------------------------------------------------------


def test_evict_guard_adopts_existing_hold(
    fake_tree, monkeypatch, tmp_path, log_recorder
):
    """control=on + state=ACTIVE_HELD at startup -> adopt, don't re-log hold."""
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
    conf, problems = rt.validate_conf(raw)
    assert not problems

    pci = fake_tree / "0000:aa:00.0"
    (pci / "power" / "control").write_text("on\n")
    (pci / "mem_info_gtt_total").write_text("1000\n")
    # Hysteresis band (450) so the normal loop does not itself release it —
    # isolates adoption from the in-loop hold/release decision.
    monkeypatch.setattr(rt, "drm_vram_in_use", lambda pci_addr: 450)

    state_dir = tmp_path / "state"
    monkeypatch.setattr(rt, "STATE_DIR", state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "state").write_text(
        "pci=0000:aa:00.0\nstate=ACTIVE_HELD\nvo=0\nvram_used=900\ngtt_total=1000\nts=1\n"
    )

    _run_one_watch_iteration(conf, monkeypatch, tmp_path, margin=0.5)

    adopted = [m for lvl, m in log_recorder if "adopted existing hold" in m]
    assert len(adopted) == 1
    # In-loop hold-trigger log must NOT fire — logged_hold was pre-set True.
    fresh_hold_logs = [m for lvl, m in log_recorder if "holding dGPU awake" in m]
    assert fresh_hold_logs == []
    # Shutdown then releases the adopted hold (loop exits via SIGTERM sim).
    assert (pci / "power" / "control").read_text().strip() == "auto"


def test_evict_guard_adoption_skipped_when_state_not_held(
    fake_tree, monkeypatch, tmp_path, log_recorder
):
    """control=on but state != ACTIVE_HELD -> warn, never adopt, never write."""
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
    conf, problems = rt.validate_conf(raw)
    assert not problems

    pci = fake_tree / "0000:aa:00.0"
    (pci / "power" / "control").write_text("on\n")
    (pci / "mem_info_gtt_total").write_text("1000\n")
    monkeypatch.setattr(rt, "drm_vram_in_use", lambda pci_addr: 100)

    state_dir = tmp_path / "state"
    monkeypatch.setattr(rt, "STATE_DIR", state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "state").write_text(
        "pci=0000:aa:00.0\nstate=ACTIVE_CONFIGURED\nvo=0\nts=1\n"
    )

    _run_one_watch_iteration(conf, monkeypatch, tmp_path, margin=0.5)

    warnings = [
        m
        for lvl, m in log_recorder
        if "not touching it" in m
    ]
    assert len(warnings) == 1
    assert rt._evict.holding is False
    # Control must remain exactly "on" — never overwritten to "auto".
    assert pci.joinpath("power", "control").read_text().strip() == "on"


# ---------------------------------------------------------------------------
# Shutdown / SIGTERM release
# ---------------------------------------------------------------------------


def test_evict_guard_releases_hold_on_shutdown(
    fake_tree, conf, monkeypatch, tmp_path
):
    """A hold still active when the loop exits must be released to 'auto'."""
    pci = fake_tree / "0000:aa:00.0"
    (pci / "power" / "control").write_text("on\n")
    (pci / "mem_info_gtt_total").write_text("1000\n")
    rt._evict.holding = True
    # Hysteresis band: normal in-loop logic must not itself release it, so
    # the observed release below is provably the shutdown path.
    monkeypatch.setattr(rt, "drm_vram_in_use", lambda pci_addr: 450)

    _run_one_watch_iteration(conf, monkeypatch, tmp_path, margin=0.5)

    assert pci.joinpath("power", "control").read_text().strip() == "auto"
    assert rt._evict.holding is False
    state_text = (rt.STATE_DIR / "state").read_text()
    assert "state=ACTIVE_CONFIGURED" in state_text


# ---------------------------------------------------------------------------
# release-hold subcommand
# ---------------------------------------------------------------------------


def test_cmd_release_hold_releases_stale_hold(fake_tree, conf, monkeypatch, tmp_path):
    pci = fake_tree / "0000:aa:00.0"
    (pci / "power" / "control").write_text("on\n")

    state_dir = tmp_path / "state"
    monkeypatch.setattr(rt, "STATE_DIR", state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "state").write_text(
        "pci=0000:aa:00.0\nstate=ACTIVE_HELD\nvo=0\nts=1\n"
    )

    result = rt.cmd_release_hold(conf)

    assert result == 0
    assert pci.joinpath("power", "control").read_text().strip() == "auto"
    state_text = (state_dir / "state").read_text()
    assert "state=ACTIVE_CONFIGURED" in state_text


def test_cmd_release_hold_noop_when_not_held(fake_tree, conf, monkeypatch, tmp_path):
    pci = fake_tree / "0000:aa:00.0"
    (pci / "power" / "control").write_text("auto\n")

    state_dir = tmp_path / "state"
    monkeypatch.setattr(rt, "STATE_DIR", state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "state").write_text("pci=0000:aa:00.0\nstate=ACTIVE_CONFIGURED\nts=1\n")

    result = rt.cmd_release_hold(conf)

    assert result == 0
    assert pci.joinpath("power", "control").read_text().strip() == "auto"


# ---------------------------------------------------------------------------
# cmd_status evict guard line
# ---------------------------------------------------------------------------


def test_cmd_status_evict_guard_off(fake_tree, conf, capsys):
    result = rt.cmd_status(conf)  # conf fixture has EVICT_GUARD=0
    assert result == 0
    out = capsys.readouterr().out
    assert "evict_guard=off" in out


def test_cmd_status_evict_guard_idle(fake_tree, monkeypatch, capsys):
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

    # State file present but not held: status must read this, not any
    # in-process _evict globals (those belong to the daemon process, not
    # this one).
    rt.STATE_DIR.mkdir(parents=True, exist_ok=True)
    (rt.STATE_DIR / "state").write_text(
        "pci=0000:aa:00.0\nstate=ACTIVE_CONFIGURED\nvram_used=100000000\n"
        "gtt_total=1000000000\nts=1\n"
    )

    result = rt.cmd_status(parsed)
    assert result == 0
    out = capsys.readouterr().out
    assert "evict_guard=idle" in out
    assert "vram_used=0.1G" in out
    assert "gtt_total=1.0G" in out
    assert "margin=0.90" in out


def test_cmd_status_evict_guard_holding(fake_tree, monkeypatch, capsys):
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

    # This is the acceptance-test regression: cmd_status() must derive the
    # label from the on-disk state file (state=ACTIVE_HELD), never from
    # _evict.holding, which is always False/default in the status process.
    rt.STATE_DIR.mkdir(parents=True, exist_ok=True)
    (rt.STATE_DIR / "state").write_text(
        "pci=0000:aa:00.0\nstate=ACTIVE_HELD\nvram_used=900000000\n"
        "gtt_total=1000000000\nts=1\n"
    )
    assert rt._evict.holding is False  # sanity: default, never touched

    result = rt.cmd_status(parsed)
    assert result == 0
    out = capsys.readouterr().out
    assert "evict_guard=holding" in out
    assert "vram_used=0.9G" in out
    assert "gtt_total=1.0G" in out


def test_cmd_status_evict_guard_holding_no_state_file(fake_tree, capsys):
    """State file missing entirely (e.g. /run/r9700-tunerd lost) but the
    daemon is actively holding: fall back to power/control==on rather than
    printing a fabricated 'idle'."""
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
    assert not (rt.STATE_DIR / "state").exists()

    pci = rt.PCI_ROOT / "0000:aa:00.0"
    (pci / "power" / "control").write_text("on\n")

    result = rt.cmd_status(parsed)
    assert result == 0
    out = capsys.readouterr().out
    assert "evict_guard=holding (control=on)" in out
    assert "vram_used=unknown" in out


def test_cmd_status_never_reads_gtt_sysfs(fake_tree, conf, monkeypatch, capsys):
    """status must be safe under D3cold: never call gtt_total_bytes/drm reads
    that would touch mem_info_gtt_total or otherwise wake the card."""
    raw = dict(conf)
    raw["EVICT_GUARD"] = True
    calls = {"gtt": 0}

    def _tracked_gtt(p):
        calls["gtt"] += 1
        return 1000

    monkeypatch.setattr(rt, "gtt_total_bytes", _tracked_gtt)
    rt.STATE_DIR.mkdir(parents=True, exist_ok=True)
    (rt.STATE_DIR / "state").write_text(
        "pci=0000:aa:00.0\nstate=ACTIVE_CONFIGURED\nvram_used=0\n"
        "gtt_total=500000000\nts=1\n"
    )

    result = rt.cmd_status(raw)
    assert result == 0
    assert calls["gtt"] == 0


# ---------------------------------------------------------------------------
# write_state: warn-once on OSError (DEFECT 1 — /run/r9700-tunerd disappearing
# must not be silently swallowed forever).
# ---------------------------------------------------------------------------


def test_write_state_warns_once_on_failure(fake_tree, log_recorder, monkeypatch):
    """A vanished/read-only STATE_DIR fails every write, but only the first
    failure logs a WARNING; subsequent failures stay silent until a write
    succeeds again."""
    monkeypatch.setattr(rt, "_state_write_warned", False)

    # Point STATE_DIR at a path that can never be created: its parent is a
    # regular file, so STATE_DIR.mkdir() always raises NotADirectoryError
    # (an OSError subclass), simulating /run being read-only post-boot.
    blocker = rt.STATE_DIR.parent / "blocker"
    blocker.write_text("not a directory\n")
    monkeypatch.setattr(rt, "STATE_DIR", blocker / "r9700-tunerd")

    rt.write_state("pci=0000:aa:00.0\nstate=SUSPENDED\nts=1\n")
    rt.write_state("pci=0000:aa:00.0\nstate=SUSPENDED\nts=2\n")
    rt.write_state("pci=0000:aa:00.0\nstate=SUSPENDED\nts=3\n")

    warnings = [msg for level, msg in log_recorder if level == rt.syslog.LOG_WARNING]
    assert len(warnings) == 1
    assert "state write failed" in warnings[0]


def test_write_state_warns_again_after_recovery(fake_tree, log_recorder, monkeypatch):
    """Once a write succeeds, the warn-once latch resets so a later failure
    (e.g. another udev re-bind tearing the dir down again) is not silent."""
    monkeypatch.setattr(rt, "_state_write_warned", False)

    blocker = rt.STATE_DIR.parent / "blocker"
    blocker.write_text("not a directory\n")
    real_state_dir = rt.STATE_DIR
    monkeypatch.setattr(rt, "STATE_DIR", blocker / "r9700-tunerd")

    rt.write_state("pci=0000:aa:00.0\nstate=SUSPENDED\nts=1\n")

    # Recover: point STATE_DIR back at a writable location.
    monkeypatch.setattr(rt, "STATE_DIR", real_state_dir)
    rt.write_state("pci=0000:aa:00.0\nstate=SUSPENDED\nts=2\n")

    # Fail again after recovery.
    monkeypatch.setattr(rt, "STATE_DIR", blocker / "r9700-tunerd")
    rt.write_state("pci=0000:aa:00.0\nstate=SUSPENDED\nts=3\n")

    warnings = [msg for level, msg in log_recorder if level == rt.syslog.LOG_WARNING]
    assert len(warnings) == 2

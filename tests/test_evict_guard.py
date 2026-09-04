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

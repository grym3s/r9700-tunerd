"""Unit tests for the shared hwmon-selection correction in tools/r9700-bench.py.

No hardware, no root, no network. The bench script is loaded as a module
(the top level is argparse/definitions only, nothing executes on import).
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BENCH_SCRIPT = _REPO_ROOT / "tools" / "r9700-bench.py"

_spec = importlib.util.spec_from_file_location(
    "bench_mod",
    _BENCH_SCRIPT,
    loader=importlib.machinery.SourceFileLoader("bench_mod", str(_BENCH_SCRIPT)),
)
bench_mod = importlib.util.module_from_spec(_spec)
sys.modules["bench_mod"] = bench_mod
_spec.loader.exec_module(bench_mod)


def _mk_pci(tmp_path: Path, name: str) -> Path:
    d = tmp_path / "pci" / name
    d.mkdir(parents=True)
    (d / "power").mkdir()
    (d / "power" / "runtime_status").write_text("active\n")
    return d


class TestBenchFindHwmon:
    def test_prefers_power1_cap_over_stale_lower(self, tmp_path):
        """Stale hwmon7 without power1_cap must lose to valid hwmon12."""
        pci = _mk_pci(tmp_path, "0000:aa:00.0")
        (pci / "hwmon" / "hwmon7").mkdir(parents=True)          # stale
        (pci / "hwmon" / "hwmon12").mkdir(parents=True)
        (pci / "hwmon" / "hwmon12" / "power1_cap").write_text("1\n")
        gpu = bench_mod.GpuSysfs(pci)
        assert gpu.hwmon.name == "hwmon12"

    def test_numeric_ordering_beats_lexicographic(self, tmp_path):
        pci = _mk_pci(tmp_path, "0000:aa:00.0")
        (pci / "hwmon" / "hwmon2").mkdir(parents=True)
        (pci / "hwmon" / "hwmon2" / "power1_cap").write_text("1\n")
        (pci / "hwmon" / "hwmon10").mkdir(parents=True)
        gpu = bench_mod.GpuSysfs(pci)
        assert gpu.hwmon.name == "hwmon2"

    def test_malformed_names_ignored(self, tmp_path):
        pci = _mk_pci(tmp_path, "0000:aa:00.0")
        (pci / "hwmon" / "hwmonx").mkdir(parents=True)
        (pci / "hwmon" / "junk").mkdir(parents=True)
        (pci / "hwmon" / "hwmon5").mkdir(parents=True)
        (pci / "hwmon" / "hwmon5" / "power1_cap").write_text("1\n")
        gpu = bench_mod.GpuSysfs(pci)
        assert gpu.hwmon.name == "hwmon5"

    def test_fallback_to_lowest_when_no_cap(self, tmp_path):
        pci = _mk_pci(tmp_path, "0000:aa:00.0")
        (pci / "hwmon" / "hwmon9").mkdir(parents=True)
        (pci / "hwmon" / "hwmon4").mkdir(parents=True)
        gpu = bench_mod.GpuSysfs(pci)
        assert gpu.hwmon.name == "hwmon4"

    def test_missing_hwmon_dir_none(self, tmp_path):
        pci = _mk_pci(tmp_path, "0000:aa:00.0")
        gpu = bench_mod.GpuSysfs(pci)
        assert gpu.hwmon is None

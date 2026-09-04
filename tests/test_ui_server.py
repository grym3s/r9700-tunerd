"""Unit tests for tools/r9700-ui.py.

No hardware, no root, no network binding beyond 127.0.0.1:0.
All subprocess calls are recorded, never executed.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

import pytest

# ---------------------------------------------------------------------------
# Load the UI server module (single-file script, no package)
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
_UI_SCRIPT = _REPO_ROOT / "tools" / "r9700-ui.py"

_spec = importlib.util.spec_from_file_location(
    "ui_mod",
    _UI_SCRIPT,
    loader=importlib.machinery.SourceFileLoader("ui_mod", str(_UI_SCRIPT)),
)
ui_mod = importlib.util.module_from_spec(_spec)
sys.modules["ui_mod"] = ui_mod
_spec.loader.exec_module(ui_mod)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_ui_tree(tmp_path, monkeypatch):
    """Build a fake R9700 sysfs tree and monkeypatch all ui_mod paths."""
    # --- PCI device -------------------------------------------------------
    gpu = tmp_path / "pci" / "0000:aa:00.0"
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

    # OD voltage table
    (gpu / "pp_od_clk_voltage").write_text(
        "OD_VDDGFX_OFFSET:\n-25mV\nOD_RANGE:\nVDDGFX_OFFSET: -200mV 0mV\n"
    )
    # DPM clocks
    (gpu / "pp_dpm_sclk").write_text("0: *1900mhz\n1: 2100mhz\n")
    (gpu / "pp_dpm_mclk").write_text("0: *1600mhz\n")
    (gpu / "gpu_busy_percent").write_text("97\n")

    # hwmon
    hwmon = gpu / "hwmon" / "hwmon7"
    hwmon.mkdir(parents=True)
    (hwmon / "power1_cap").write_text("210000000\n")
    (hwmon / "power1_cap_min").write_text("210000000\n")
    (hwmon / "power1_cap_max").write_text("330000000\n")
    (hwmon / "power1_cap_default").write_text("300000000\n")
    (hwmon / "power1_average").write_text("180000000\n")
    (hwmon / "temp1_input").write_text("55000\n")
    (hwmon / "temp2_input").write_text("60000\n")
    (hwmon / "temp3_input").write_text("45000\n")
    (hwmon / "fan1_input").write_text("1200\n")

    # --- Config file ------------------------------------------------------
    conf_path = tmp_path / "r9700-tunerd.conf"
    conf_path.write_text(
        "VENDOR=0x1002\nDEVICE=0x7551\n"
        "SUBSYSTEM_VENDOR=0x1043\nSUBSYSTEM_DEVICE=0x0626\n"
        "POWER_LIMIT_W=210\nVOLTAGE_OFFSET_MV=-25\n"
    )

    # --- Ranges cache -----------------------------------------------------
    ranges_path = tmp_path / "ranges.json"
    ranges_path.write_text(json.dumps({
        "cap_min_w": 210, "cap_max_w": 330, "cap_default_w": 300,
        "vo_min_mv": -200, "vo_max_mv": 0,
    }))

    # --- State file -------------------------------------------------------
    state_path = tmp_path / "state"
    state_path.write_text("tuned\n")

    # --- Bench directory --------------------------------------------------
    bench_dir = tmp_path / "bench"
    bench_dir.mkdir()

    # --- UI HTML ----------------------------------------------------------
    ui_html = tmp_path / "index.html"
    ui_html.write_text("<html><body>r9700</body></html>")

    # --- Monkeypatch module-level paths -----------------------------------
    monkeypatch.setattr(ui_mod, "CONF_PATH", conf_path)
    monkeypatch.setattr(ui_mod, "RANGES_CACHE", ranges_path)
    monkeypatch.setattr(ui_mod, "STATE_FILE", state_path)
    monkeypatch.setattr(ui_mod, "BENCH_DIR", bench_dir)
    monkeypatch.setattr(ui_mod, "UI_HTML", ui_html)
    monkeypatch.setattr(ui_mod, "BENCH_SCRIPT", tmp_path / "r9700-bench.py")
    monkeypatch.setattr(ui_mod, "DAEMON_CLI", "/usr/local/sbin/r9700-tunerd")

    # Bypass real PCI discovery
    monkeypatch.setattr(ui_mod, "_discover_pci", lambda cfg: gpu)

    # Set module globals
    ui_mod.GPU = ui_mod.GpuSysfs(gpu)
    ui_mod.CONFIG = {
        "VENDOR": "0x1002", "DEVICE": "0x7551",
        "SUBSYSTEM_VENDOR": "0x1043", "SUBSYSTEM_DEVICE": "0x0626",
        "POWER_LIMIT_W": "210", "VOLTAGE_OFFSET_MV": "-25",
    }
    ui_mod.TOKEN = "test-token-abcdef"
    ui_mod._SAMPLING = ui_mod._SamplingState()
    ui_mod.BENCH_PROC.clear()

    return tmp_path


@pytest.fixture
def fake_subprocess(monkeypatch):
    """Record subprocess.run / Popen calls; never execute anything."""
    calls: list[dict[str, Any]] = []

    class _FakeCP:
        def __init__(self, args, rc=0, out="", err=""):
            self.args = args
            self.returncode = rc
            self.stdout = out
            self.stderr = err

    def _fake_run(cmd, **kw):
        calls.append({"kind": "run", "cmd": list(cmd), "kw": kw})
        # Sensible defaults per command type
        if cmd and cmd[0] == "systemctl":
            if "is-active" in cmd:
                return _FakeCP(cmd, 0, "active\n", "")
            if "is-enabled" in cmd:
                return _FakeCP(cmd, 0, "enabled\n", "")
            if "show" in cmd:
                return _FakeCP(cmd, 0, "MainPID=4242\n", "")
            return _FakeCP(cmd, 0, "", "")
        if cmd and cmd[0] == "journalctl":
            return _FakeCP(cmd, 0, "2025-01-01T00:00:00Z msg\n", "")
        if cmd and cmd[0] == "sudo":
            return _FakeCP(cmd, 0, "ok\n", "")
        return _FakeCP(cmd, 0, "", "")

    def _fake_popen(cmd, **kw):
        calls.append({"kind": "popen", "cmd": list(cmd), "kw": kw})

        class _FakeProc:
            pid = 99999
            stdout = iter([])
            def poll(self):
                return 0
            def wait(self):
                return 0

        return _FakeProc()

    monkeypatch.setattr(ui_mod.subprocess, "run", _fake_run)
    monkeypatch.setattr(ui_mod.subprocess, "Popen", _fake_popen)
    return calls


@pytest.fixture
def server(fake_ui_tree, fake_subprocess):
    """Start ThreadingHTTPServer on 127.0.0.1:0 in a daemon thread."""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), ui_mod.Handler)
    srv.daemon_threads = True
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    yield base
    srv.shutdown()
    srv.server_close()


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _get(base: str, path: str, token: str | None = None) -> tuple[int, dict | list]:
    url = base + path
    if token is not None:
        url += f"?t={token}"
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _post(base: str, path: str, body: dict, token: str = "test-token-abcdef") -> tuple[int, dict | list]:
    url = base + path + f"?t={token}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


# ---------------------------------------------------------------------------
# Token / auth tests
# ---------------------------------------------------------------------------

class TestAuth:
    def test_missing_token_403(self, server):
        code, body = _get(server, "/api/status", token=None)
        assert code == 403
        assert "error" in body

    def test_wrong_token_403(self, server):
        code, body = _get(server, "/api/status", token="wrong-token")
        assert code == 403
        assert "error" in body

    def test_correct_token_200(self, server):
        code, body = _get(server, "/api/status", token="test-token-abcdef")
        assert code == 200
        assert "ts" in body
        assert "runtime_status" in body


# ---------------------------------------------------------------------------
# /api/status payload shape
# ---------------------------------------------------------------------------

class TestStatusPayload:
    def test_expected_keys(self, server):
        code, body = _get(server, "/api/status", token="test-token-abcdef")
        assert code == 200
        expected = {
            "ts", "pci", "runtime_status", "power_state",
            "suspended_ms", "control", "tuned", "live", "ranges",
            "service", "state_file", "sampling",
        }
        assert expected.issubset(body.keys()), f"Missing keys: {expected - set(body.keys())}"

    def test_tuned_values_from_config(self, server):
        code, body = _get(server, "/api/status", token="test-token-abcdef")
        assert code == 200
        assert body["tuned"]["offset_mv"] == -25
        assert body["tuned"]["cap_w"] == 210

    def test_suspended_live_null_no_sensor_read(self, server, fake_ui_tree, monkeypatch):
        """When runtime_status=suspended, live must be null and no hwmon/pp_od
        file may be opened."""
        # Flip the fake tree to suspended
        (fake_ui_tree / "pci" / "0000:aa:00.0" / "power" / "runtime_status").write_text("suspended\n")

        # Guard: raise if any sensor-level read is attempted
        def _guard_hwmon(self, name):
            raise AssertionError(f"read_hwmon({name!r}) called while suspended")

        def _guard_active(self, relpath):
            raise AssertionError(f"read_active({relpath!r}) called while suspended")

        monkeypatch.setattr(ui_mod.GpuSysfs, "read_hwmon", _guard_hwmon)
        monkeypatch.setattr(ui_mod.GpuSysfs, "read_active", _guard_active)

        # Reset sampling so no cached block leaks in
        ui_mod._SAMPLING = ui_mod._SamplingState()

        code, body = _get(server, "/api/status", token="test-token-abcdef")
        assert code == 200
        assert body["runtime_status"] == "suspended"
        assert body["live"] is None
        assert body["sampling"]["mode"] == "asleep"


# ---------------------------------------------------------------------------
# Adaptive sampling (unit-level, no HTTP)
# ---------------------------------------------------------------------------

class TestAdaptiveSampling:
    def test_busy_mode_after_high_busy(self, fake_ui_tree):
        """After recording gpu_busy=97, subsequent ticks use 2 s interval (busy)."""
        s = ui_mod._SamplingState()
        t0 = 1000.0

        # First tick: no prior read → should_read, mode idle-backoff (first read)
        mode, _, should_read, _, _ = s.tick(t0, "active")
        assert should_read is True
        # Record the read with high busy
        s.record_read(t0, {"gpu_busy": 97}, 97)

        # Second tick 1 s later: busy interval is 2 s, so NOT due yet
        mode, next_s, should_read, _, _ = s.tick(t0 + 1.0, "active")
        assert mode == "busy"
        assert should_read is False
        assert next_s == pytest.approx(1.0, abs=0.01)

        # Third tick 3 s after first: beyond 2 s interval → read again
        mode, _, should_read, _, _ = s.tick(t0 + 3.0, "active")
        assert mode == "busy"
        assert should_read is True

    def test_idle_backoff_no_second_read_within_9s(self, fake_ui_tree):
        """After recording gpu_busy=2, the 9 s backoff prevents a second read."""
        s = ui_mod._SamplingState()
        t0 = 2000.0

        # First tick: initial read
        mode, _, should_read, _, _ = s.tick(t0, "active")
        assert should_read is True
        s.record_read(t0, {"gpu_busy": 2}, 2)

        # 5 s later: still within 9 s window → no read
        mode, next_s, should_read, cached, _ = s.tick(t0 + 5.0, "active")
        assert mode == "idle-backoff"
        assert should_read is False
        assert cached == {"gpu_busy": 2}
        assert next_s == pytest.approx(4.0, abs=0.01)

        # 10 s later: beyond 9 s → read again
        mode, _, should_read, _, _ = s.tick(t0 + 10.0, "active")
        assert mode == "idle-backoff"
        assert should_read is True

    def test_suspend_resets_and_forces_busy_on_wake(self, fake_ui_tree):
        """Suspend→wake transition forces immediate busy-mode read."""
        s = ui_mod._SamplingState()
        t0 = 3000.0

        # Active, record a read
        s.tick(t0, "active")
        s.record_read(t0, {"gpu_busy": 3}, 3)

        # Suspend
        mode, _, should_read, _, _ = s.tick(t0 + 1.0, "suspended")
        assert mode == "asleep"
        assert should_read is False

        # Wake: should force immediate busy read
        mode, _, should_read, _, _ = s.tick(t0 + 2.0, "active")
        assert mode == "busy"
        assert should_read is True


# ---------------------------------------------------------------------------
# /api/set validation and CLI invocation
# ---------------------------------------------------------------------------

class TestApiSet:
    def test_nonint_offset_400(self, server, fake_subprocess):
        code, body = _post(server, "/api/set", {"offset_mv": "abc"})
        assert code == 400
        assert "offset_mv" in body["error"]
        # No subprocess call should have been made
        assert len(fake_subprocess) == 0

    def test_bool_offset_400(self, server, fake_subprocess):
        code, body = _post(server, "/api/set", {"offset_mv": True})
        assert code == 400
        assert len(fake_subprocess) == 0

    def test_positive_offset_400(self, server, fake_subprocess):
        code, body = _post(server, "/api/set", {"offset_mv": 10})
        assert code == 400
        assert len(fake_subprocess) == 0

    def test_offset_below_range_400(self, server, fake_subprocess):
        code, body = _post(server, "/api/set", {"offset_mv": -600})
        assert code == 400
        assert len(fake_subprocess) == 0

    def test_cap_below_range_400(self, server, fake_subprocess):
        code, body = _post(server, "/api/set", {"cap_w": 10})
        assert code == 400
        assert len(fake_subprocess) == 0

    def test_cap_above_range_400(self, server, fake_subprocess):
        code, body = _post(server, "/api/set", {"cap_w": 2000})
        assert code == 400
        assert len(fake_subprocess) == 0

    def test_valid_offset_and_cap_argv(self, server, fake_subprocess):
        code, body = _post(server, "/api/set", {"offset_mv": -25, "cap_w": 210})
        assert code == 200
        assert body["ok"] is True
        # Two CLI calls: set-undervolt then set-power-cap
        assert len(fake_subprocess) == 2
        assert fake_subprocess[0]["cmd"] == [
            "sudo", "-n", "/usr/local/sbin/r9700-tunerd", "set-undervolt", "-25"
        ]
        assert fake_subprocess[1]["cmd"] == [
            "sudo", "-n", "/usr/local/sbin/r9700-tunerd", "set-power-cap", "210"
        ]

    def test_body_too_large_413(self, server, fake_subprocess):
        """A body exceeding 65536 bytes must be rejected with 413."""
        # Build a JSON body > 64 KiB
        big = {"offset_mv": -25, "pad": "x" * 70000}
        data = json.dumps(big).encode()
        assert len(data) > 65536

        url = server + "/api/set?t=test-token-abcdef"
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                code = resp.status
        except urllib.error.HTTPError as e:
            code = e.code
        assert code == 413
        assert len(fake_subprocess) == 0


# ---------------------------------------------------------------------------
# /api/bench listing
# ---------------------------------------------------------------------------

class TestApiBench:
    def test_lists_json_skips_superseded(self, server, fake_ui_tree):
        bench_dir = fake_ui_tree / "bench"

        # Valid result at top level
        (bench_dir / "run-001.json").write_text(json.dumps({
            "label": "eff-test",
            "timestamp": "2025-06-01T12:00:00Z",
            "pre_run": {"vddgfx_offset_mv": -25, "power_cap_w": 210},
            "aggregates": {
                "mean_gen_tok_s": 42.0, "agg_tok_s": 84.0,
                "mean_power_w": 195.0, "tok_s_per_w": 0.43,
                "max_junction_c": 68.0,
                "offset_stable": True, "cap_stable": True,
            },
            "errors": [], "d3cold_s": 12.0, "warmup_s": 30.0,
        }))

        # Superseded result (must be skipped)
        sup = bench_dir / "superseded"
        sup.mkdir()
        (sup / "old-run.json").write_text(json.dumps({
            "label": "old", "timestamp": "2025-01-01T00:00:00Z",
            "pre_run": {"vddgfx_offset_mv": 0, "power_cap_w": 300},
            "aggregates": {}, "errors": [],
        }))

        code, body = _get(server, "/api/bench", token="test-token-abcdef")
        assert code == 200
        assert isinstance(body, list)
        assert len(body) == 1
        assert body[0]["label"] == "eff-test"
        # Superseded must NOT appear
        labels = [b.get("label") for b in body]
        assert "old" not in labels


# ---------------------------------------------------------------------------
# /api/profiles measured flag
# ---------------------------------------------------------------------------

class TestApiProfiles:
    def test_efficiency_measured_when_bench_exists(self, server, fake_ui_tree):
        bench_dir = fake_ui_tree / "bench"
        # Create a bench result matching Efficiency (offset=-50, cap=210)
        (bench_dir / "eff-result.json").write_text(json.dumps({
            "label": "eff",
            "timestamp": "2025-06-01T00:00:00Z",
            "pre_run": {"vddgfx_offset_mv": -50, "power_cap_w": 210},
            "aggregates": {
                "mean_gen_tok_s": 40.0, "agg_tok_s": 80.0,
                "mean_power_w": 190.0, "tok_s_per_w": 0.42,
                "max_junction_c": 65.0,
                "offset_stable": True, "cap_stable": True,
            },
            "errors": [],
        }))

        code, body = _get(server, "/api/profiles", token="test-token-abcdef")
        assert code == 200
        assert isinstance(body, list)
        by_name = {p["name"]: p for p in body}
        assert by_name["Efficiency"]["measured"] is True
        # Stock (offset=0, cap=300) has no matching bench → not measured
        assert by_name["Stock"]["measured"] is False

    def test_no_bench_none_measured(self, server, fake_ui_tree):
        """With an empty bench dir, no profile is marked measured."""
        code, body = _get(server, "/api/profiles", token="test-token-abcdef")
        assert code == 200
        for p in body:
            assert p["measured"] is False
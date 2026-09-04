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
        """After recording gpu_busy=97, subsequent ticks use the 0.5 s busy interval."""
        s = ui_mod._SamplingState()
        t0 = 1000.0

        # First tick: no prior read → should_read, mode idle-backoff (first read)
        mode, _, should_read, _, _ = s.tick(t0, "active")
        assert should_read is True
        # Record the read with high busy
        s.record_read(t0, {"gpu_busy": 97}, 97)

        # Second tick 0.2 s later: busy interval is 0.5 s, so NOT due yet
        mode, next_s, should_read, _, _ = s.tick(t0 + 0.2, "active")
        assert mode == "busy"
        assert should_read is False
        assert next_s == pytest.approx(0.3, abs=0.01)

        # Third tick 0.6 s after first: beyond 0.5 s interval → read again
        mode, _, should_read, _, _ = s.tick(t0 + 0.6, "active")
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
            "errors": [], "d3cold_s": 11.0,
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


# ---------------------------------------------------------------------------
# GET-route auth: every /api/* GET must 403 without a valid token
# ---------------------------------------------------------------------------

class TestAuthGetRoutes:
    @pytest.mark.parametrize("path", [
        "/api/status",
        "/api/events",
        "/api/bench",
        "/api/bench/status",
        "/api/profiles",
        "/api/unknown",
    ])
    def test_get_no_token_403(self, server, path):
        code, body = _get(server, path, token=None)
        assert code == 403
        assert "error" in body

    @pytest.mark.parametrize("path", [
        "/api/status",
        "/api/bench",
        "/api/bench/status",
        "/api/profiles",
    ])
    def test_get_wrong_token_403(self, server, path):
        code, body = _get(server, path, token="wrong-token")
        assert code == 403
        assert "error" in body

    def test_static_ui_requires_no_token(self, server):
        req = urllib.request.Request(server + "/")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                assert resp.status == 200
                assert b"r9700" in resp.read()
        except urllib.error.HTTPError:
            pytest.fail("static UI must be served without a token")


# ---------------------------------------------------------------------------
# /api/set: full-request validation before ANY subprocess call
# ---------------------------------------------------------------------------

class TestApiSetValidation:
    def test_mixed_valid_offset_invalid_cap_zero_cli(self, server, fake_subprocess):
        """The spec's example: a valid offset plus an out-of-range cap must
        make ZERO CLI calls (validate the complete request first)."""
        code, body = _post(server, "/api/set", {"offset_mv": -50, "cap_w": 10})
        assert code == 400
        assert "cap_w" in body["error"]
        assert len(fake_subprocess) == 0

    def test_mixed_invalid_offset_valid_cap_zero_cli(self, server, fake_subprocess):
        code, body = _post(server, "/api/set", {"offset_mv": 10, "cap_w": 210})
        assert code == 400
        assert "offset_mv" in body["error"]
        assert len(fake_subprocess) == 0

    def test_non_object_body_zero_cli(self, server, fake_subprocess):
        url = server + "/api/set?t=test-token-abcdef"
        data = json.dumps(["offset_mv", -25]).encode()
        req = urllib.request.Request(url, data=data,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                code = resp.status
        except urllib.error.HTTPError as e:
            code = e.code
        assert code == 400
        assert len(fake_subprocess) == 0

    def test_unknown_field_zero_cli(self, server, fake_subprocess):
        code, body = _post(server, "/api/set", {"offset_mv": -25, "bogus": 1})
        assert code == 400
        assert "bogus" in body["error"]
        assert len(fake_subprocess) == 0

    def test_empty_object_zero_cli(self, server, fake_subprocess):
        code, body = _post(server, "/api/set", {})
        assert code == 400
        assert len(fake_subprocess) == 0

    def test_null_values_zero_cli(self, server, fake_subprocess):
        code, body = _post(server, "/api/set", {"offset_mv": None, "cap_w": None})
        assert code == 400
        assert len(fake_subprocess) == 0

    def test_bool_cap_zero_cli(self, server, fake_subprocess):
        code, body = _post(server, "/api/set", {"offset_mv": -25, "cap_w": True})
        assert code == 400
        assert "cap_w" in body["error"]
        assert len(fake_subprocess) == 0

    def test_float_offset_zero_cli(self, server, fake_subprocess):
        code, body = _post(server, "/api/set", {"offset_mv": -25.5})
        assert code == 400
        assert len(fake_subprocess) == 0


# ---------------------------------------------------------------------------
# Mutation serialization: concurrent mutations get 409, no subprocess call
# ---------------------------------------------------------------------------

class TestMutationLock:
    def test_concurrent_set_one_409_no_concurrent_exec(self, server, fake_subprocess, monkeypatch):
        import threading as _threading
        release = _threading.Event()
        fake_run_real = ui_mod.subprocess.run  # the recording fake from the fixture

        def _blocking(cmd, **kw):
            if cmd and cmd[0] == "sudo":
                result = fake_run_real(cmd, **kw)  # records exactly once
                assert release.wait(timeout=10), "test release timed out"
                return result
            return fake_run_real(cmd, **kw)       # systemctl/journalctl etc.

        monkeypatch.setattr(ui_mod.subprocess, "run", _blocking)
        results = [None, None]

        def worker(i):
            results[i] = _post(server, "/api/set", {"offset_mv": -25})[0]

        t = _threading.Thread(target=worker, args=(0,), daemon=True)
        t.start()
        # Worker holds MUT_LOCK and is parked inside the blocked CLI call;
        # wait until it has actually appended its call before racing it.
        deadline = time.monotonic() + 10
        while not any(c["cmd"] and c["cmd"][0] == "sudo"
                      for c in fake_subprocess) and time.monotonic() < deadline:
            time.sleep(0.01)
        code, body = _post(server, "/api/set", {"offset_mv": -25})
        assert code == 409
        assert "error" in body
        release.set()
        t.join(timeout=10)
        assert results[0] == 200
        # Exactly one CLI call total; the 409 loser made none.
        sudo_calls = [c for c in fake_subprocess if c["cmd"] and c["cmd"][0] == "sudo"]
        assert len(sudo_calls) == 1
        ui_mod.BENCH_PROC.clear()

    def test_409_when_mutation_lock_held(self, server, fake_subprocess):
        assert ui_mod.MUT_LOCK.acquire(blocking=False) is True
        try:
            code, body = _post(server, "/api/apply", {})
        finally:
            ui_mod.MUT_LOCK.release()
        assert code == 409
        assert "error" in body
        assert len(fake_subprocess) == 0


# ---------------------------------------------------------------------------
# Config refresh: a successful Apply is visible on the next /api/status
# ---------------------------------------------------------------------------

class TestConfigRefresh:
    def test_apply_reflects_in_next_status(self, server, fake_ui_tree, fake_subprocess):
        code, body = _get(server, "/api/status", token="test-token-abcdef")
        assert code == 200
        assert body["tuned"]["cap_w"] == 210

        code, body = _post(server, "/api/apply", {})
        assert code == 200
        assert body["ok"] is True

        # Apply rewrote the config (simulated): next status must show it
        # without any server restart.
        (fake_ui_tree / "r9700-tunerd.conf").write_text(
            "VENDOR=0x1002\nDEVICE=0x7551\n"
            "SUBSYSTEM_VENDOR=0x1043\nSUBSYSTEM_DEVICE=0x0626\n"
            "POWER_LIMIT_W=250\nVOLTAGE_OFFSET_MV=-50\n"
        )
        code, body = _get(server, "/api/status", token="test-token-abcdef")
        assert code == 200
        assert body["tuned"]["offset_mv"] == -50
        assert body["tuned"]["cap_w"] == 250


# ---------------------------------------------------------------------------
# /api/bench/run: bounded, sanitized launch input
# ---------------------------------------------------------------------------

class TestApiBenchRun:
    @pytest.mark.parametrize("label", [
        "../evil", "a/b", "a\\b", "a b", "tab\there", "ctrl\x01",
        "x" * 65, "",
    ])
    def test_unsafe_label_400(self, server, fake_subprocess, label):
        code, body = _post(server, "/api/bench/run", {"label": label})
        assert code == 400
        assert "label" in body["error"]
        assert len(fake_subprocess) == 0

    def test_nonstring_label_400(self, server, fake_subprocess):
        code, body = _post(server, "/api/bench/run", {"label": 123})
        assert code == 400
        assert len(fake_subprocess) == 0

    @pytest.mark.parametrize("prompts", [0, 7, True, -1, 2.5])
    def test_bad_prompts_400(self, server, fake_subprocess, prompts):
        code, body = _post(server, "/api/bench/run", {"label": "ok", "prompts": prompts})
        assert code == 400
        assert "prompts" in body["error"]
        assert len(fake_subprocess) == 0

    @pytest.mark.parametrize("max_tokens", [0, 4097, True, -5, 10.5])
    def test_bad_max_tokens_400(self, server, fake_subprocess, max_tokens):
        code, body = _post(server, "/api/bench/run", {"label": "ok", "max_tokens": max_tokens})
        assert code == 400
        assert "max_tokens" in body["error"]
        assert len(fake_subprocess) == 0

    def test_unknown_field_400(self, server, fake_subprocess):
        code, body = _post(server, "/api/bench/run", {"label": "ok", "extra": 1})
        assert code == 400
        assert "extra" in body["error"]
        assert len(fake_subprocess) == 0

    def test_valid_run_starts_once(self, server, fake_ui_tree, fake_subprocess):
        code, body = _post(
            server, "/api/bench/run",
            {"label": "run-01.A", "prompts": 3, "max_tokens": 256})
        assert code == 200
        assert body["ok"] is True
        popens = [c for c in fake_subprocess if c["kind"] == "popen"]
        assert len(popens) == 1
        cmd = popens[0]["cmd"]
        assert "run" in cmd and "--label" in cmd and "run-01.A" in cmd
        assert "--prompts" in cmd and "3" in cmd
        assert "--max-tokens" in cmd and "256" in cmd
        ui_mod.BENCH_PROC.clear()

    def test_bench_already_running_409(self, server, fake_subprocess):
        # Seed a RUNNING bench (poll() is None) so the "already running"
        # guard is actually exercised; a second /api/bench/run must get 409
        # and start nothing new.
        class _RunningProc:
            def poll(self):
                return None  # still running
        ui_mod.BENCH_PROC.clear()
        ui_mod.BENCH_PROC.update({
            "proc": _RunningProc(), "label": "already", "started": time.time(),
            "lines": deque(),
        })
        code, body = _post(server, "/api/bench/run", {"label": "ok"})
        assert code == 409
        assert "error" in body
        assert len(fake_subprocess) == 0
        ui_mod.BENCH_PROC.clear()


# ---------------------------------------------------------------------------
# /api/bench listing: integer error counts, strict stability evidence
# ---------------------------------------------------------------------------

class TestBenchStability:
    def _write(self, bench_dir, name, obj):
        (bench_dir / name).write_text(json.dumps(obj))

    def test_legacy_error_shapes_and_stability(self, fake_ui_tree):
        bench_dir = fake_ui_tree / "bench"
        # errors as a list → count 2 → never stable
        self._write(bench_dir, "legacy-list.json", {
            "label": "lst", "timestamp": "2025-06-01T00:00:00Z",
            "aggregates": {"offset_stable": True, "cap_stable": True},
            "errors": ["a", "b"], "d3cold_s": 10.0,
        })
        # errors as a number
        self._write(bench_dir, "num.json", {
            "label": "num", "timestamp": "2025-06-02T00:00:00Z",
            "aggregates": {"offset_stable": True, "cap_stable": True},
            "errors": 3, "d3cold_s": 10.0,
        })
        # Legacy numeric counts may have been serialized as JSON floats.
        self._write(bench_dir, "floatnum.json", {
            "label": "float", "timestamp": "2025-06-02T12:00:00Z",
            "aggregates": {"offset_stable": True, "cap_stable": True},
            "errors": 2.0, "d3cold_s": 10.0,
        })
        # A negative count is malformed and must not be exposed as a count.
        self._write(bench_dir, "negative.json", {
            "label": "negative", "timestamp": "2025-06-02T13:00:00Z",
            "aggregates": {"offset_stable": True, "cap_stable": True},
            "errors": -1, "d3cold_s": 10.0,
        })
        # errors null → 0
        self._write(bench_dir, "nullerr.json", {
            "label": "nul", "timestamp": "2025-06-03T00:00:00Z",
            "aggregates": {"offset_stable": True, "cap_stable": True},
            "errors": None, "d3cold_s": 10.0,
        })
        # malformed errors → 0
        self._write(bench_dir, "malformed.json", {
            "label": "mal", "timestamp": "2025-06-04T00:00:00Z",
            "aggregates": {"offset_stable": True, "cap_stable": True},
            "errors": "weird", "d3cold_s": 10.0,
        })
        # fully valid → stable
        self._write(bench_dir, "good.json", {
            "label": "good", "timestamp": "2025-06-05T00:00:00Z",
            "aggregates": {"offset_stable": True, "cap_stable": True},
            "errors": [], "d3cold_s": 12.5,
        })
        # missing d3cold → NOT stable even with clean errors
        self._write(bench_dir, "nod3.json", {
            "label": "nod3", "timestamp": "2025-06-06T00:00:00Z",
            "aggregates": {"offset_stable": True, "cap_stable": True},
            "errors": [],
        })
        # stability fields missing → NOT stable (no silent success)
        self._write(bench_dir, "nostab.json", {
            "label": "nostab", "timestamp": "2025-06-07T00:00:00Z",
            "errors": [], "d3cold_s": 10.0,
        })
        # one stability field false → NOT stable
        self._write(bench_dir, "half.json", {
            "label": "half", "timestamp": "2025-06-08T00:00:00Z",
            "aggregates": {"offset_stable": True, "cap_stable": False},
            "errors": [], "d3cold_s": 10.0,
        })
        # negative d3cold → NOT stable
        self._write(bench_dir, "neg.json", {
            "label": "neg", "timestamp": "2025-06-09T00:00:00Z",
            "aggregates": {"offset_stable": True, "cap_stable": True},
            "errors": [], "d3cold_s": -1.0,
        })
        by_label = {b["label"]: b for b in ui_mod._list_bench()}
        assert by_label["lst"]["errors"] == 2 and by_label["lst"]["stable"] is False
        assert by_label["num"]["errors"] == 3 and by_label["num"]["stable"] is False
        assert by_label["float"]["errors"] == 2 and by_label["float"]["stable"] is False
        assert by_label["negative"]["errors"] == 0 and by_label["negative"]["stable"] is True
        assert by_label["nul"]["errors"] == 0 and by_label["nul"]["stable"] is True
        assert by_label["mal"]["errors"] == 0 and by_label["mal"]["stable"] is True
        assert by_label["good"]["errors"] == 0 and by_label["good"]["stable"] is True
        assert by_label["nod3"]["stable"] is False
        assert by_label["nostab"]["stable"] is False
        assert by_label["half"]["stable"] is False
        assert by_label["neg"]["stable"] is False
        for b in by_label.values():
            assert isinstance(b["errors"], int)
            assert not isinstance(b["errors"], bool)


# ---------------------------------------------------------------------------
# GpuSysfs._find_hwmon: numeric ordering, power1_cap preference, malformed
# names, re-resolve after driver rebind
# ---------------------------------------------------------------------------

def _mk_pci(tmp_path, name, files=()):
    d = tmp_path / "pci" / name
    d.mkdir(parents=True)
    (d / "power").mkdir()
    (d / "power" / "runtime_status").write_text("active\n")
    for f in files:
        (d / f).write_text("1\n")
    return d


class TestFindHwmon:
    def test_prefers_power1_cap_over_stale_lower(self, tmp_path):
        pci = _mk_pci(tmp_path, "0000:aa:00.0")
        (pci / "hwmon" / "hwmon7").mkdir(parents=True)       # stale, no power1_cap
        (pci / "hwmon" / "hwmon12").mkdir(parents=True)
        (pci / "hwmon" / "hwmon12" / "power1_cap").write_text("1\n")
        gpu = ui_mod.GpuSysfs(pci)
        assert gpu.hwmon.name == "hwmon12"

    def test_numeric_ordering_beats_lexicographic(self, tmp_path):
        pci = _mk_pci(tmp_path, "0000:aa:00.0")
        # hwmon2 (with power1_cap) must win over hwmon10 even though
        # lexicographically "hwmon10" < "hwmon2".
        (pci / "hwmon" / "hwmon2").mkdir(parents=True)
        (pci / "hwmon" / "hwmon2" / "power1_cap").write_text("1\n")
        (pci / "hwmon" / "hwmon10").mkdir(parents=True)
        gpu = ui_mod.GpuSysfs(pci)
        assert gpu.hwmon.name == "hwmon2"

    def test_malformed_names_ignored(self, tmp_path):
        pci = _mk_pci(tmp_path, "0000:aa:00.0")
        (pci / "hwmon" / "hwmonx").mkdir(parents=True)          # malformed
        (pci / "hwmon" / "notahwmon").mkdir(parents=True)       # malformed
        (pci / "hwmon" / "hwmon3").mkdir(parents=True)
        (pci / "hwmon" / "hwmon3" / "power1_cap").write_text("1\n")
        gpu = ui_mod.GpuSysfs(pci)
        assert gpu.hwmon.name == "hwmon3"

    def test_no_power1_cap_falls_back_to_lowest(self, tmp_path):
        pci = _mk_pci(tmp_path, "0000:aa:00.0")
        (pci / "hwmon" / "hwmon9").mkdir(parents=True)
        (pci / "hwmon" / "hwmon4").mkdir(parents=True)
        gpu = ui_mod.GpuSysfs(pci)
        assert gpu.hwmon.name == "hwmon4"

    def test_re_resolve_after_rebind(self, tmp_path):
        pci = _mk_pci(tmp_path, "0000:aa:00.0")
        old = pci / "hwmon" / "hwmon7"
        old.mkdir(parents=True)
        (old / "power1_cap").write_text("1\n")
        gpu = ui_mod.GpuSysfs(pci)
        assert gpu.hwmon.name == "hwmon7"
        # Driver rebind: old hwmon gone, new one appears.
        import shutil
        shutil.rmtree(old)
        new = pci / "hwmon" / "hwmon21"
        new.mkdir(parents=True)
        (new / "power1_cap").write_text("1\n")
        assert gpu.read_hwmon("power1_cap") == "1"
        assert gpu.hwmon.name == "hwmon21"

    def test_suspended_no_hwmon_read(self, tmp_path, monkeypatch):
        """While suspended, read_hwmon must return None and must NOT open the
        hwmon sensor file (only PM-safe runtime_status may be read)."""
        pci = _mk_pci(tmp_path, "0000:aa:00.0")
        hw = pci / "hwmon" / "hwmon7"
        hw.mkdir(parents=True)
        (hw / "power1_cap").write_text("1\n")
        (pci / "power" / "runtime_status").write_text("suspended\n")
        gpu = ui_mod.GpuSysfs(pci)
        assert gpu.hwmon.name == "hwmon7"

        import shutil
        shutil.rmtree(hw)  # hwmon vanished (rebind) while suspended

        opened = []

        def _spy_read(*args):
            opened.append(str(args[-1]) if args else "")
            return None

        # _read is a staticmethod; monkeypatch to a plain fn that records.
        monkeypatch.setattr(ui_mod.GpuSysfs, "_read", staticmethod(_spy_read),
                            raising=False)
        assert gpu.read_hwmon("power1_cap") is None
        hwmon_reads = [p for p in opened if "hwmon7" in p]
        assert hwmon_reads == [], f"hwmon sensor read while suspended: {hwmon_reads}"
        # No re-resolve happened (hwmon path unchanged, still the deleted dir).
        assert gpu.hwmon.name == "hwmon7"


# ---------------------------------------------------------------------------
# PCI discovery: exact-identity matches, refuse ambiguity
# ---------------------------------------------------------------------------

def _mk_pci_dev(root, name, subd="0x0626"):
    d = root / name
    d.mkdir(parents=True)
    (d / "vendor").write_text("0x1002\n")
    (d / "device").write_text("0x7551\n")
    (d / "subsystem_vendor").write_text("0x1043\n")
    (d / "subsystem_device").write_text(subd + "\n")
    return d


class TestPciDiscovery:
    def test_single_exact_match_selected(self, tmp_path, monkeypatch):
        root = tmp_path / "pci"
        _mk_pci_dev(root, "0000:aa:00.0")
        _mk_pci_dev(root, "0000:bb:00.0", subd="0x9999")  # different identity
        monkeypatch.setattr(ui_mod, "PCI_DEVICES_ROOT", root)
        cfg = {"VENDOR": "0x1002", "DEVICE": "0x7551",
               "SUBSYSTEM_VENDOR": "0x1043", "SUBSYSTEM_DEVICE": "0x0626"}
        assert ui_mod._discover_pci(cfg) == root / "0000:aa:00.0"

    def test_ambiguous_matches_refused(self, tmp_path, monkeypatch):
        root = tmp_path / "pci"
        _mk_pci_dev(root, "0000:aa:00.0")
        _mk_pci_dev(root, "0000:cc:00.0")  # exact duplicate identity
        monkeypatch.setattr(ui_mod, "PCI_DEVICES_ROOT", root)
        cfg = {"VENDOR": "0x1002", "DEVICE": "0x7551",
               "SUBSYSTEM_VENDOR": "0x1043", "SUBSYSTEM_DEVICE": "0x0626"}
        assert ui_mod._discover_pci(cfg) is None

    def test_no_match_none(self, tmp_path, monkeypatch):
        root = tmp_path / "pci"
        _mk_pci_dev(root, "0000:bb:00.0", subd="0x9999")
        monkeypatch.setattr(ui_mod, "PCI_DEVICES_ROOT", root)
        cfg = {"VENDOR": "0x1002", "DEVICE": "0x7551",
               "SUBSYSTEM_VENDOR": "0x1043", "SUBSYSTEM_DEVICE": "0x0626"}
        assert ui_mod._discover_pci(cfg) is None

    def test_ambiguous_no_gpu_no_hardware_read(self, tmp_path, monkeypatch,
                                               fake_subprocess):
        """Two exact matches → GPU stays None; _build_status must not touch
        any sensor file."""
        root = tmp_path / "pci"
        a = _mk_pci_dev(root, "0000:aa:00.0")
        b = _mk_pci_dev(root, "0000:cc:00.0")
        monkeypatch.setattr(ui_mod, "PCI_DEVICES_ROOT", root)
        cfg = {"VENDOR": "0x1002", "DEVICE": "0x7551",
               "SUBSYSTEM_VENDOR": "0x1043", "SUBSYSTEM_DEVICE": "0x0626"}
        assert ui_mod._discover_pci(cfg) is None

        monkeypatch.setattr(ui_mod, "GPU", None)
        opened = []

        def _spy_read(self, path):
            opened.append(str(path))
            return None

        monkeypatch.setattr(ui_mod.GpuSysfs, "_read", _spy_read, raising=False)
        payload = ui_mod._build_status(include_journal=False)
        assert payload["pci"] is None
        assert payload["live"] is None
        assert opened == []
        # No daemon/CLI work was started for the ambiguous (absent) GPU.
        sudo_calls = [c for c in fake_subprocess
                      if c["cmd"] and c["cmd"][0] == "sudo"]
        assert sudo_calls == []

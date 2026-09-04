#!/usr/bin/env python3
"""r9700-ui.py — local UI backend for r9700-tunerd (R9700 / RDNA4).

Binds 127.0.0.1:7970.  All privileged ops go through
  sudo -n /usr/local/sbin/r9700-tunerd …
Never opens /dev/dri, never writes sysfs, never wakes the card.
"""
import argparse
import csv
import errno
import json
import math
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CONF_PATH = Path("/etc/r9700-tunerd.conf")
RANGES_CACHE = Path("/run/r9700-tunerd/ranges.json")
STATE_FILE = Path("/run/r9700-tunerd/state")
BENCH_DIR = Path("~/r9700-bench").expanduser()
BENCH_SCRIPT = Path(__file__).resolve().parent / "r9700-bench.py"
REPO_ROOT = Path(__file__).resolve().parent.parent
UI_HTML = REPO_ROOT / "ui" / "index.html"
MATRIX_DOC = REPO_ROOT / "docs" / "MATRIX-2026-09-04.md"
RESULTS_DIR = REPO_ROOT / "docs" / "results"
DAEMON_CLI = "/usr/local/sbin/r9700-tunerd"
BENCH_ENDPOINT = "http://127.0.0.1:1234/v1"
BENCH_MODEL = "qwen/qwen3.8-27b@q4_k_m"
PCI_DEVICES_ROOT = Path("/sys/bus/pci/devices")

MAX_BODY_BYTES = 65536

# Named profiles the daemon understands (mirrors r9700-tunerd's PROFILE_NAMES;
# used only for a fast pre-check before the CLI call, never as the source of
# truth — list-profiles --json is the source of truth for names/values).
KNOWN_PROFILE_NAMES = ("EFFICIENCY", "BALANCED", "PERFORMANCE")

TOKEN = ""
GPU = None
CONFIG = {}
BENCH_LOCK = threading.Lock()
BENCH_PROC = {}
# Serialization lock for mutating API operations (set / apply / reset /
# bench/run): a second concurrent mutation gets 409 instead of racing the
# daemon.  Deliberately separate from BENCH_LOCK, which keeps benchmark
# ownership under its own lock for the lifetime of the bench process.
MUT_LOCK = threading.Lock()


# ─── PCI discovery (identity only, never bus addr / card#) ──────────────────

def _load_config():
    cfg = {}
    try:
        for line in CONF_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    except OSError:
        pass
    return cfg


def _discover_pci(cfg):
    """Collect ALL exact identity matches; refuse ambiguity.

    Identity is vendor/device/subsystem only — never card number or bus
    address.  With zero matches returns None; with two or more the device
    is ambiguous and we select none (no silent first-match pick, no
    hardware read attempted).
    """
    vendor = cfg.get("VENDOR", "0x1002").lstrip("0x")
    device = cfg.get("DEVICE", "0x7551").lstrip("0x")
    subv = cfg.get("SUBSYSTEM_VENDOR", "0x1043").lstrip("0x")
    subd = cfg.get("SUBSYSTEM_DEVICE", "0x0626").lstrip("0x")
    try:
        devices = sorted(PCI_DEVICES_ROOT.iterdir())
    except (OSError, FileNotFoundError):
        return None
    matches = []
    for dev in devices:
        try:
            if (dev.joinpath("vendor").read_text().strip().lstrip("0x") == vendor
                    and dev.joinpath("device").read_text().strip().lstrip("0x") == device
                    and dev.joinpath("subsystem_vendor").read_text().strip().lstrip("0x") == subv
                    and dev.joinpath("subsystem_device").read_text().strip().lstrip("0x") == subd):
                matches.append(dev)
        except (OSError, FileNotFoundError):
            continue
    if len(matches) == 1:
        return matches[0]
    return None


# ─── Safe sysfs reader (runtime-PM aware) ───────────────────────────────────
# RDNA4/amdgpu quirk: reading hwmon or pp_od_* while in D3cold returns EBUSY.
# power/runtime_status and power_state are the ONLY files safe while suspended
# (proven not to trigger a runtime resume).  We gate every other read behind a
# status check and treat EBUSY/EAGAIN as a benign TOCTOU (device suspended
# between check and read).

class GpuSysfs:
    def __init__(self, pci):
        self.pci = pci
        self.hwmon = self._find_hwmon()

    @staticmethod
    def _hwmon_sort_key(name):
        """Numeric hwmonN ordering (hwmon2 < hwmon10 < hwmon12); malformed
        names sort after all numeric ones."""
        m = re.fullmatch(r"hwmon(\d+)", name)
        if not m:
            return (1, 0, name)
        return (0, int(m.group(1)), name)

    def _find_hwmon(self):
        """Pick the R9700 hwmon: numeric ordering, prefer a candidate that
        contains power1_cap, ignore malformed (non hwmonN) names."""
        hroot = self.pci / "hwmon"
        try:
            if not hroot.is_dir():
                return None
            children = sorted(hroot.iterdir(), key=lambda c: self._hwmon_sort_key(c.name))
        except OSError:
            return None
        best = None
        for child in children:
            if not re.fullmatch(r"hwmon\d+", child.name):
                continue
            if child.is_dir() and (child / "power1_cap").is_file():
                return child
            if best is None:
                best = child
        return best

    def _ensure_hwmon(self):
        """Re-resolve once if a previously-cached hwmon path has disappeared
        (e.g. driver rebind).  Only directory stat checks — safe while
        suspended, no sensor reads, never opens /dev/dri."""
        if self.hwmon is not None and self.hwmon.is_dir():
            return  # cached path still valid
        self.hwmon = self._find_hwmon()

    @staticmethod
    def _read(path):
        try:
            return path.read_text().strip()
        except OSError as e:
            if e.errno in (errno.EBUSY, errno.EAGAIN):
                return None  # device suspended mid-read (TOCTOU)
            raise

    def runtime_status(self):
        """Safe while suspended — does NOT wake the device."""
        return self._read(self.pci / "power" / "runtime_status")

    def power_state(self):
        """Safe while suspended. Path is <pci>/power_state (NOT under power/)."""
        return self._read(self.pci / "power_state")

    def runtime_suspended_time(self):
        """Accumulated ms in suspend. Safe while suspended."""
        return self._read(self.pci / "power" / "runtime_suspended_time")

    def read_active(self, relpath):
        """Read a PCI-level sysfs file only if device is active."""
        if self.runtime_status() != "active":
            return None
        return self._read(self.pci / relpath)

    def read_hwmon(self, name):
        """Read a hwmon attribute only if device is active."""
        if self.runtime_status() != "active":
            return None
        # Recover a cached hwmon that vanished (e.g. driver rebind): re-resolve
        # once.  A previously-cached Path is truthy even when its directory is
        # gone, so gate on the directory still existing, not on truthiness.
        # _ensure_hwmon() is a no-op while the cached path is still a directory,
        # so this is PM-safe (stat checks only, no sensor reads, never /dev/dri).
        if self.hwmon is None or not self.hwmon.is_dir():
            self._ensure_hwmon()
        if self.hwmon is None:
            return None
        return self._read(self.hwmon / name)


# ─── Adaptive sensor sampling state ─────────────────────────────────────────
# Prevents the SSE loop from re-arming the amdgpu 5 s autosuspend timer on
# every 2 s poll.  PM-safe fields are still read every tick; the heavier
# hwmon/OD/clock block is read only when a "sensor slot" is due.
#   • busy mode (last gpu_busy >= 5):  slot = 0.5 s (card is working anyway;
#                                       fast trace is free while it is busy)
#   • idle mode (last gpu_busy < 5):   slot = 9 s  (> 5 s autosuspend)
#   • after suspend→wake:             first slot is immediate (busy mode)
BUSY_INTERVAL_S = 0.5
IDLE_INTERVAL_S = 9.0

class _SamplingState:
    def __init__(self):
        self._lock = threading.Lock()
        self._last_read = None       # monotonic ts of last sensor read
        self._last_busy = None       # last observed gpu_busy_percent
        self._last_block = None      # last successful sensor dict
        self._prev_status = None     # previous tick's runtime_status
        self._force_busy = False     # one-shot: force 2 s mode after wake

    def tick(self, now, runtime_status):
        """Decide whether to read sensors this tick.

        Returns (mode, next_read_s, should_read, cached_block, age_s).
        """
        with self._lock:
            # Detect active → non-active transition (suspend).
            if runtime_status != "active" and self._prev_status == "active":
                self._last_read = None
                self._last_busy = None
                self._force_busy = True  # next wake samples immediately
            self._prev_status = runtime_status

            if runtime_status != "active":
                age = (now - self._last_read) if self._last_read is not None else None
                return ("asleep", 0.0, False, self._last_block, age)

            # Active: pick interval.
            if self._force_busy or (self._last_busy is not None and self._last_busy >= 5):
                interval = BUSY_INTERVAL_S
            else:
                interval = IDLE_INTERVAL_S

            if self._last_read is None or (now - self._last_read) >= interval:
                mode = "busy" if (self._force_busy
                                  or (self._last_busy is not None and self._last_busy >= 5)) \
                       else "idle-backoff"
                return (mode, 0.0, True, None, None)
            else:
                remaining = interval - (now - self._last_read)
                mode = "busy" if (self._force_busy
                                  or (self._last_busy is not None and self._last_busy >= 5)) \
                       else "idle-backoff"
                age = now - self._last_read
                return (mode, remaining, False, self._last_block, age)

    def record_read(self, now, block, gpu_busy):
        with self._lock:
            self._last_read = now
            self._last_block = block
            self._last_busy = gpu_busy
            self._force_busy = False  # consume the one-shot flag


_SAMPLING = _SamplingState()


# ─── Parsers ─────────────────────────────────────────────────────────────────

def _parse_dpm_clock(text):
    for line in text.splitlines():
        if "*" in line:
            m = re.search(r"(\d+)\s*mhz", line, re.I)
            if m:
                return int(m.group(1))
    return None


def _parse_od_vddgfx(text):
    """Return (current, min, max) mV from OD_VDDGFX_OFFSET section."""
    vals, in_sec = [], False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("OD_VDDGFX_OFFSET"):
            in_sec = True
            continue
        if in_sec and s.startswith("OD_"):
            break
        if in_sec:
            m = re.search(r"(-?\d+)\s*mV", s, re.I)
            if m:
                vals.append(int(m.group(1)))
    if len(vals) >= 3:
        return vals[0], vals[1], vals[2]
    if len(vals) == 1:
        return vals[0], None, None
    return None, None, None


def _hwmon_w(name):
    """Read a hwmon power attribute (µW) and convert to watts."""
    v = GPU.read_hwmon(name) if GPU else None
    return int(v) / 1_000_000 if v is not None else None


# ─── Fan-curve parsing (mirrors r9700-tunerd's parse_fan_curve; pure, no I/O) ──

_FAN_POINT_RANGES = {"temp": (0, 110), "pwm": (0, 100)}


def _parse_curve_str(raw):
    """Parse a "temp:pwm,temp:pwm,..." string into a list of [temp, pwm]
    pairs sorted by temp, or None if raw is empty/unparsable. Used only for
    display (the daemon is the authority on validity via set-fan-curve)."""
    text = (raw or "").strip()
    if not text:
        return None
    points = []
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        t_raw, p_raw = chunk.split(":", 1)
        try:
            points.append([float(t_raw.strip()), float(p_raw.strip())])
        except ValueError:
            continue
    if not points:
        return None
    points.sort(key=lambda pt: pt[0])
    return points


def _validate_fan_curve_points(points):
    """Mirror the daemon's parse_fan_curve validation rules (monotonic,
    2+ points, ranges). Returns a list of problem strings (empty = valid).
    This is belt-and-suspenders only: the server never trusts this result
    to skip the daemon's own independent validation in set-fan-curve."""
    problems = []
    if not isinstance(points, list) or len(points) < 2:
        return ["at least two points are required"]
    parsed = []
    for pt in points:
        if (not isinstance(pt, (list, tuple)) or len(pt) != 2
                or isinstance(pt[0], bool) or isinstance(pt[1], bool)
                or not isinstance(pt[0], (int, float))
                or not isinstance(pt[1], (int, float))):
            problems.append(f"point {pt!r} is not [temp, pwm]")
            continue
        t, p = float(pt[0]), float(pt[1])
        if t < 0 or t > 110:
            problems.append(f"temp {t} out of range 0..110")
            continue
        if p < 0 or p > 100:
            problems.append(f"pwm {p} out of range 0..100")
            continue
        parsed.append((t, p))
    if problems:
        return problems
    parsed.sort(key=lambda pt: pt[0])
    temps = [t for t, _ in parsed]
    if len(set(temps)) != len(temps):
        return ["duplicate temperature points"]
    for i in range(1, len(parsed)):
        if parsed[i][0] <= parsed[i - 1][0]:
            return ["temp points must be strictly increasing"]
        if parsed[i][1] < parsed[i - 1][1]:
            return ["pwm points must be monotonically non-decreasing with temp"]
    return []


def _points_to_curve_str(points):
    def _fmt(x):
        return str(int(x)) if float(x).is_integer() else str(x)
    return ",".join(f"{_fmt(t)}:{_fmt(p)}" for t, p in points)


# ─── Sensor block reader (extracted for adaptive sampling) ──────────────────

def _read_sensors():
    """Read the full sensor block.  Returns a dict (fields may be None on
    individual EBUSY) or None if GPU is unavailable."""
    if GPU is None:
        return None
    live = {}
    od = GPU.read_active("pp_od_clk_voltage")
    if od:
        cur, vmin, vmax = _parse_od_vddgfx(od)
        live["offset_mv"] = cur
        live["vo_min_mv"] = vmin
        live["vo_max_mv"] = vmax
    else:
        live["offset_mv"] = None
        live["vo_min_mv"] = None
        live["vo_max_mv"] = None
    cap = GPU.read_hwmon("power1_cap")
    live["cap_w"] = int(cap) / 1_000_000 if cap is not None else None
    live["cap_min_w"] = _hwmon_w("power1_cap_min")
    live["cap_max_w"] = _hwmon_w("power1_cap_max")
    live["cap_default_w"] = _hwmon_w("power1_cap_default")
    pw = GPU.read_hwmon("power1_average") or GPU.read_hwmon("power1_input")
    live["power_w"] = int(pw) / 1_000_000 if pw is not None else None
    for key, attr in [("edge_c", "temp1_input"),
                      ("junction_c", "temp2_input"),
                      ("mem_c", "temp3_input")]:
        v = GPU.read_hwmon(attr)
        live[key] = int(v) / 1000 if v is not None else None
    v = GPU.read_hwmon("fan1_input")
    live["fan_rpm"] = int(v) if v is not None else None
    pwm_raw = GPU.read_hwmon("pwm1")
    live["fan_pwm_pct"] = round(int(pwm_raw) / 255.0 * 100.0, 1) if pwm_raw is not None else None
    pwm_mode = GPU.read_hwmon("pwm1_enable")
    live["fan_mode"] = ("manual" if pwm_mode == "1" else "firmware") if pwm_mode is not None else None
    sclk = GPU.read_active("pp_dpm_sclk")
    live["sclk_mhz"] = _parse_dpm_clock(sclk) if sclk else None
    mclk = GPU.read_active("pp_dpm_mclk")
    live["mclk_mhz"] = _parse_dpm_clock(mclk) if mclk else None
    busy = GPU.read_active("gpu_busy_percent")
    live["gpu_busy"] = int(busy) if busy is not None else None
    return live


# ─── Status payload ──────────────────────────────────────────────────────────

def _build_status(include_journal=True):
    ts = datetime.now(timezone.utc).isoformat()
    now = time.monotonic()
    # Refresh config on every status build so a successful Apply (which
    # rewrites the tuning values in /etc/r9700-tunerd.conf) is visible
    # without restarting the UI server.  Config file read only — never
    # touches sensors.
    global CONFIG
    CONFIG = _load_config()
    pci = str(GPU.pci) if GPU else None

    # PM-safe reads (safe in any power state — never wake the card).
    rs = GPU.runtime_status() if GPU else None
    ps = GPU.power_state() if GPU else None
    susp = GPU.runtime_suspended_time() if GPU else None
    suspended_ms = int(susp) if susp is not None else None
    # PM-safe read (safe in any power state — never wakes the card).  Guard on
    # pci: with no target GPU, pci is None and Path(None) would raise TypeError
    # (not OSError), which the except below does not catch.
    control = None
    if pci is not None:
        try:
            control = (Path(pci) / "power" / "control").read_text().strip()
        except OSError:
            control = None

    # Adaptive sensor sampling.
    mode, next_read_s, should_read, cached_block, age_s = _SAMPLING.tick(now, rs)

    live = None
    if should_read and rs == "active":
        live = _read_sensors()
        if live is not None:
            _SAMPLING.record_read(now, live, live.get("gpu_busy"))
            age_s = 0.0
    elif cached_block is not None:
        live = cached_block

    tuned = {
        "offset_mv": int(CONFIG.get("VOLTAGE_OFFSET_MV", "0")),
        "cap_w": int(CONFIG.get("POWER_LIMIT_W", "300")),
    }

    # Ranges: prefer live; fall back to daemon-written cache.
    ranges = {"source": None, "cap_min_w": None, "cap_max_w": None,
              "cap_default_w": None, "vo_min_mv": None, "vo_max_mv": None,
              "age_s": None}
    if live:
        ranges.update(source="live", age_s=0,
                      cap_min_w=live.get("cap_min_w"),
                      cap_max_w=live.get("cap_max_w"),
                      cap_default_w=live.get("cap_default_w"),
                      vo_min_mv=live.get("vo_min_mv"),
                      vo_max_mv=live.get("vo_max_mv"))
    elif RANGES_CACHE.exists():
        try:
            c = json.loads(RANGES_CACHE.read_text())
            ranges.update(source="cache",
                          cap_min_w=c.get("cap_min_w"),
                          cap_max_w=c.get("cap_max_w"),
                          cap_default_w=c.get("cap_default_w"),
                          vo_min_mv=c.get("vo_min_mv"),
                          vo_max_mv=c.get("vo_max_mv"),
                          age_s=round(time.time() - RANGES_CACHE.stat().st_mtime, 1))
        except (OSError, json.JSONDecodeError):
            pass

    # Service state via systemctl.
    svc = {"active": False, "enabled": False, "pid": None}
    try:
        r = subprocess.run(["systemctl", "is-active", "r9700-tunerd"],
                           capture_output=True, text=True, timeout=5)
        svc["active"] = r.stdout.strip() == "active"
        r = subprocess.run(["systemctl", "is-enabled", "r9700-tunerd"],
                           capture_output=True, text=True, timeout=5)
        svc["enabled"] = r.stdout.strip() == "enabled"
        r = subprocess.run(["systemctl", "show", "-p", "MainPID", "r9700-tunerd"],
                           capture_output=True, text=True, timeout=5)
        m = re.search(r"MainPID=(\d+)", r.stdout)
        svc["pid"] = int(m.group(1)) if m and m.group(1) != "0" else None
    except (OSError, subprocess.TimeoutExpired):
        pass

    state_text = None
    daemon_state = {}
    try:
        state_text = STATE_FILE.read_text().strip()
        for line in state_text.splitlines():
            k, _, v = line.partition("=")
            if k.strip():
                daemon_state[k.strip()] = v.strip()
    except OSError:
        pass
    # Held-awake state: daemon writes state=ACTIVE_HELD with vram_used/gtt_total.
    held_awake = None
    if daemon_state.get("state") == "ACTIVE_HELD":
        def _gb(key):
            try:
                return round(int(daemon_state[key]) / 1073741824, 1)
            except (KeyError, ValueError):
                return None
        held_awake = {"vram_used_gb": _gb("vram_used"), "gtt_total_gb": _gb("gtt_total")}
    # Eviction risk is measured by the daemon only during active wake handling.
    # Show it only when eviction_risk=1 AND not held (mutually exclusive).
    eviction_risk = None
    if daemon_state.get("eviction_risk") == "1" and not held_awake:
        def _gb(key):
            try:
                return round(int(daemon_state[key]) / 1073741824, 1)
            except (KeyError, ValueError):
                return None
        eviction_risk = {"unsafe_to_suspend": True, "vram_used_gb": _gb("vram_used"), "gtt_total_gb": _gb("gtt_total")}

    # Fan block: config (always PM-safe, disk read only) plus live pwm/rpm
    # taken from the same `live` sensor dict built above under the same
    # active-only gate — no extra hwmon reads are ever made here.
    def _safe_float(raw, default):
        try:
            return float(raw)
        except (TypeError, ValueError):
            return default
    fan = {
        "enabled": bool(CONFIG.get("FAN_CURVE_ENABLED") in ("1", "true", "True")),
        "curve": CONFIG.get("FAN_CURVE") or None,
        "points": _parse_curve_str(CONFIG.get("FAN_CURVE") or ""),
        "hysteresis_c": _safe_float(CONFIG.get("FAN_HYSTERESIS_C"), 3.0),
        "mode": live.get("fan_mode") if live else None,
        "pwm_pct": live.get("fan_pwm_pct") if live else None,
        "rpm": live.get("fan_rpm") if live else None,
    }

    payload = {
        "ts": ts, "pci": pci, "runtime_status": rs, "power_state": ps,
        "suspended_ms": suspended_ms, "control": control,
        "tuned": tuned, "live": live, "ranges": ranges,
        "service": svc, "state_file": state_text,
        "daemon_state": daemon_state.get("state"), "held_awake": held_awake, "eviction_risk": eviction_risk,
        "sampling": {"mode": mode, "next_sensor_read_s": round(next_read_s, 2)},
        "fan": fan,
    }
    if live is not None:
        payload["live_age_s"] = round(age_s, 2) if age_s is not None else 0.0

    if include_journal:
        try:
            r = subprocess.run(
                ["journalctl", "-t", "r9700-tunerd", "-n", "15",
                 "-o", "short-precise", "--no-pager"],
                capture_output=True, text=True, timeout=5)
            payload["recent_journal"] = r.stdout.strip().splitlines()
        except (OSError, subprocess.TimeoutExpired):
            payload["recent_journal"] = []
    return payload


# ─── Bench helpers ───────────────────────────────────────────────────────────

def _bench_reader(proc, lines):
    """Daemon thread: drain bench stdout into a bounded deque."""
    for line in proc.stdout:
        lines.append(line.rstrip())
    proc.wait()


_BENCH_LABEL_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _valid_bench_label(label):
    """1..64 chars, filename-safe (A-Z a-z 0-9 . _ -), no path separators or
    control characters."""
    return (isinstance(label, str)
            and _BENCH_LABEL_RE.fullmatch(label) is not None)


def _error_count(value):
    """Coerce legacy ``errors`` payloads to a nonnegative integer count.

    Historical result files used lists, integers, and occasionally null or
    malformed values.  Keep the API schema stable for all of them while not
    allowing booleans, NaN, or negative values to masquerade as a count.
    """
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float) and math.isfinite(value) and value >= 0:
        return int(value)
    if isinstance(value, (list, tuple)):
        return len(value)
    return 0


def _bench_stable(r, agg, errors):
    """Stable only with explicit evidence: BOTH stability fields explicitly
    true, zero errors, and a D3cold recovery present and nonnegative.
    Missing fields are never treated as success."""
    if agg.get("offset_stable") is not True or agg.get("cap_stable") is not True:
        return False
    if errors != 0:
        return False
    d3 = r.get("d3cold_s")
    if isinstance(d3, bool) or not isinstance(d3, (int, float)) or d3 < 0:
        return False
    return True


def _list_bench():
    results = []
    if not BENCH_DIR.is_dir():
        return results
    for f in sorted(BENCH_DIR.glob("*.json")):
        if "superseded" in f.parts:
            continue
        try:
            r = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        agg = r.get("aggregates")
        if not isinstance(agg, dict):
            agg = {}
        pre = r.get("pre_run")
        if not isinstance(pre, dict):
            pre = {}
        errors = _error_count(r.get("errors"))
        results.append({
            "label": r.get("label"), "timestamp": r.get("timestamp"),
            "offset_mv": pre.get("vddgfx_offset_mv"),
            "cap_w": pre.get("power_cap_w"),
            "gen_tok_s": agg.get("mean_gen_tok_s"),
            "agg_tok_s": agg.get("agg_tok_s"),
            "mean_power_w": agg.get("mean_power_w"),
            "tok_s_per_w": agg.get("tok_s_per_w"),
            "max_junction_c": agg.get("max_junction_c"),
            "errors": errors,
            "d3cold_s": r.get("d3cold_s"),
            "stable": _bench_stable(r, agg, errors),
            "warmup_s": r.get("warmup_s"),
        })
    results.sort(key=lambda x: x.get("timestamp") or "", reverse=True)
    return results


# ─── Matrix results (read-only parse of docs/MATRIX-*.md + docs/results/*) ──

_MD_TABLE_ROW_RE = re.compile(r"^\|(.+)\|\s*$")
_MD_HEADER_SEP_RE = re.compile(r"^[\s|:-]+$")

# Canonical column names the UI understands, keyed by a normalized (lower,
# stripped, non-alnum removed) match against the markdown header cell.
_MD_COLUMN_ALIASES = {
    "offsetmv": "offset_mv",
    "capw": "cap_w",
    "gentoks": "gen_tok_s",
    "aggtoks": "agg_tok_s",
    "meanw": "mean_w",
    "toksw": "tok_s_per_w",
    "tjmaxc": "max_junction_c",
    "errors": "errors",
    "d3colds": "d3cold_s",
    "verdict": "verdict",
}


def _norm_col(cell):
    return re.sub(r"[^a-z0-9]", "", cell.strip().lower())


def _num(cell):
    cell = cell.strip()
    if cell == "" or cell == "—":
        return None
    try:
        if re.fullmatch(r"-?\d+", cell):
            return int(cell)
        return float(cell)
    except ValueError:
        return cell  # non-numeric column (e.g. verdict) passes through as text


def _parse_markdown_matrix(text, source):
    """Parse every pipe-table in a markdown doc into row dicts.

    Tolerant of any pipe table whose header cells match the known column
    aliases (order-independent); tables with no recognised columns are
    skipped. Returns a list of dicts, each carrying "source".
    """
    rows = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = _MD_TABLE_ROW_RE.match(lines[i])
        if not m or i + 1 >= len(lines) or not _MD_HEADER_SEP_RE.match(lines[i + 1]):
            i += 1
            continue
        header_cells = [c.strip() for c in m.group(1).split("|")]
        cols = [_MD_COLUMN_ALIASES.get(_norm_col(c)) for c in header_cells]
        if not any(cols):
            i += 2
            continue
        j = i + 2
        while j < len(lines):
            rm = _MD_TABLE_ROW_RE.match(lines[j])
            if not rm:
                break
            cells = [c.strip().rstrip("*").strip() for c in rm.group(1).split("|")]
            row = {"source": source}
            for col, cell in zip(cols, cells):
                if col is None:
                    continue
                row[col] = _num(cell) if col != "verdict" else cell
            if len(row) > 1:
                rows.append(row)
            j += 1
        i = j
    return rows


def _parse_results_csv(path):
    rows = []
    try:
        with path.open(newline="") as f:
            reader = csv.DictReader(f)
            for raw in reader:
                row = {"source": str(path.relative_to(REPO_ROOT))}
                for k, v in raw.items():
                    if k is None:
                        continue
                    key = _MD_COLUMN_ALIASES.get(_norm_col(k), k.strip())
                    row[key] = _num(v) if v is not None else None
                rows.append(row)
    except (OSError, csv.Error):
        return []
    return rows


def _list_matrix_results():
    """Server-side parse of docs/MATRIX-*.md and docs/results/*. Read-only,
    no rerun. Returns a flat list of per-run dicts."""
    rows = []
    if MATRIX_DOC.is_file():
        try:
            text = MATRIX_DOC.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        if text:
            rows.extend(_parse_markdown_matrix(text, MATRIX_DOC.name))
    if RESULTS_DIR.is_dir():
        for p in sorted(RESULTS_DIR.rglob("*")):
            if not p.is_file():
                continue
            rel = str(p.relative_to(REPO_ROOT))
            if p.suffix.lower() == ".csv":
                rows.extend(_parse_results_csv(p))
            elif p.suffix.lower() == ".md":
                try:
                    text = p.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                rows.extend(_parse_markdown_matrix(text, rel))
    return rows


# ─── HTTP handler ────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "r9700-ui/1.0"

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {self.address_string()} {fmt % args}",
              flush=True)

    def log_request(self, code='-', size='-'):
        """Override to log the path WITHOUT the query string (avoids leaking
        the ?t=… token into access logs)."""
        if hasattr(code, 'value'):
            code = code.value
        path = urllib.parse.urlparse(self.path).path
        self.log_message('"%s %s %s" %s %s',
                         self.command, path, self.request_version,
                         str(code), str(size))

    def _auth_ok(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        tok = q.get("t", [None])[0] or self.headers.get("X-Token")
        if tok is None or not TOKEN:
            return False
        # #6: constant-time comparison to prevent timing side-channel.
        return secrets.compare_digest(tok.encode("utf-8"), TOKEN.encode("utf-8"))

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code, msg):
        self._json(code, {"error": msg})

    def _check_post(self):
        ct = self.headers.get("Content-Type", "")
        if "application/json" not in ct:
            self._err(415, "Content-Type must be application/json")
            return None
        # #5: refuse chunked bodies (cannot determine size up-front).
        te = self.headers.get("Transfer-Encoding", "").lower()
        if "chunked" in te:
            self._err(400, "chunked Transfer-Encoding not supported")
            return None
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length < 0:
                self._err(400, "invalid Content-Length")
                return None
        except (ValueError, TypeError):
            self._err(400, "invalid or missing Content-Length")
            return None
        # #5: cap body at 64 KiB.
        if length > MAX_BODY_BYTES:
            self._err(413, f"request body too large (max {MAX_BODY_BYTES} bytes)")
            return None
        try:
            return json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError):
            self._err(400, "invalid JSON body")
            return None

    # ── GET ──

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._serve_ui()
        elif path.startswith("/api/"):
            if not self._auth_ok():
                self._err(403, "invalid or missing token")
                return
            if path == "/api/status":
                self._api_status()
            elif path == "/api/events":
                self._api_events()
            elif path == "/api/bench":
                self._json(200, _list_bench())
            elif path == "/api/bench/status":
                self._api_bench_status()
            elif path == "/api/profiles":
                self._api_profiles()
            elif path == "/api/named-profiles":
                self._api_named_profiles()
            elif path == "/api/matrix":
                self._json(200, _list_matrix_results())
            else:
                self._err(404, "unknown endpoint")
        else:
            self._err(404, "not found")

    def _serve_ui(self):
        try:
            data = UI_HTML.read_bytes()
        except OSError:
            self._err(404, "ui/index.html not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _api_status(self):
        if not self._auth_ok():
            self._err(403, "invalid or missing token")
            return
        self._json(200, _build_status(include_journal=True))

    def _api_events(self):
        if not self._auth_ok():
            self._err(403, "invalid or missing token")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        last_ka = time.monotonic()
        try:
            while True:
                payload = _build_status(include_journal=False)
                self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
                self.wfile.flush()
                if time.monotonic() - last_ka >= 15:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    last_ka = time.monotonic()
                # Fast ticks only while the card is busy; 2 s otherwise (asleep or idle).
                time.sleep(BUSY_INTERVAL_S if (payload.get("sampling") or {}).get("mode") == "busy" else 2)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # client disconnected

    def _api_bench_status(self):
        if not self._auth_ok():
            self._err(403, "invalid or missing token")
            return
        with BENCH_LOCK:
            if not BENCH_PROC:
                self._json(200, {"running": False, "label": None,
                                 "started": None, "tail": []})
                return
            proc = BENCH_PROC["proc"]
            running = proc.poll() is None
            tail = list(BENCH_PROC["lines"])[-20:]
            self._json(200, {"running": running, "label": BENCH_PROC["label"],
                             "started": BENCH_PROC["started"], "tail": tail})

    def _api_profiles(self):
        if not self._auth_ok():
            self._err(403, "invalid or missing token")
            return
        # Efficiency = the Phase 4 result at 210 W (docs/MATRIX-2026-09-04.md):
        # -50 mV captures the whole measured gain. Balanced/Performance keep
        # the shallower -25 mV until -50 mV is measured at their caps.
        profiles = [
            {"name": "Stock", "offset_mv": 0, "cap_w": 300, "measured": False},
            {"name": "Efficiency", "offset_mv": -50, "cap_w": 210, "measured": False},
            {"name": "Balanced", "offset_mv": -25, "cap_w": 250, "measured": False},
            {"name": "Performance", "offset_mv": -25, "cap_w": 300, "measured": False},
        ]
        bench = _list_bench()
        for p in profiles:
            for b in bench:
                if (b.get("offset_mv") == p["offset_mv"]
                        and b.get("cap_w") == p["cap_w"]):
                    p["measured"] = True
                    break
        self._json(200, profiles)

    def _api_named_profiles(self):
        if not self._auth_ok():
            self._err(403, "invalid or missing token")
            return
        # Source of truth: the daemon's list-profiles --json. Never
        # hardcoded on the UI side, so a profile-engine change is picked
        # up without touching this file.
        step = self._run_cli("list-profiles", "--json")
        if step["rc"] != 0:
            self._err(502, f"list-profiles failed: {step['stderr'] or step['stdout']}")
            return
        try:
            data = json.loads(step["stdout"])
        except (ValueError, json.JSONDecodeError):
            self._err(502, "list-profiles returned invalid JSON")
            return
        self._json(200, data)

    # ── POST ──

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if not path.startswith("/api/"):
            self._err(404, "not found")
            return
        if not self._auth_ok():
            self._err(403, "invalid or missing token")
            return
        if path not in ("/api/set", "/api/reset", "/api/apply", "/api/bench/run",
                        "/api/set-profile", "/api/set-fan-curve"):
            self._err(404, "unknown endpoint")
            return
        # Serialize mutations (set/apply/reset/bench run): a concurrent
        # mutation gets a fast 409 instead of racing the daemon.  The lock
        # is released as soon as the synchronous operation finishes; async
        # benchmark ownership stays under BENCH_LOCK.
        if not MUT_LOCK.acquire(blocking=False):
            self._err(409, "another mutation is in progress")
            return
        try:
            if path == "/api/set":
                self._api_set()
            elif path == "/api/reset":
                self._api_reset()
            elif path == "/api/apply":
                self._api_apply()
            elif path == "/api/bench/run":
                self._api_bench_run()
            elif path == "/api/set-profile":
                self._api_set_profile()
            elif path == "/api/set-fan-curve":
                self._api_set_fan_curve()
        finally:
            MUT_LOCK.release()

    def _run_cli(self, *args):
        cmd = ["sudo", "-n", DAEMON_CLI] + list(args)
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            return {"cmd": " ".join(cmd), "rc": r.returncode,
                    "stdout": r.stdout.strip(), "stderr": r.stderr.strip()}
        except subprocess.TimeoutExpired:
            return {"cmd": " ".join(cmd), "rc": -1, "stdout": "",
                    "stderr": "timeout (30s)"}
        except OSError as e:
            return {"cmd": " ".join(cmd), "rc": -1, "stdout": "", "stderr": str(e)}

    def _api_set(self):
        body = self._check_post()
        if body is None:
            return
        # Validate the COMPLETE request (JSON object, no unknown fields,
        # real integers, at least one of offset_mv/cap_w) BEFORE any
        # subprocess is invoked — a bad request makes zero CLI calls.
        if not isinstance(body, dict):
            self._err(400, "body must be a JSON object")
            return
        unknown = set(body) - {"offset_mv", "cap_w"}
        if unknown:
            self._err(400, f"unknown field(s): {', '.join(sorted(unknown))}")
            return
        offset = body.get("offset_mv")
        cap = body.get("cap_w")
        if offset is None and cap is None:
            self._err(400, "provide at least one of offset_mv or cap_w")
            return
        if offset is not None:
            if (not isinstance(offset, int) or isinstance(offset, bool)
                    or offset > 0 or offset < -500):
                self._err(400, "offset_mv must be int in [-500, 0]")
                return
        if cap is not None:
            if (not isinstance(cap, int) or isinstance(cap, bool)
                    or cap < 50 or cap > 1000):
                self._err(400, "cap_w must be int in [50, 1000]")
                return
        # All fields valid — only now may we touch the CLI.
        steps = []
        if offset is not None and cap is not None:
            # Both values: one atomic set-tuning call (single config write,
            # single range-validation snapshot, restore-on-failure in the
            # daemon) instead of two separate mutations.
            steps.append(self._run_cli(
                "set-tuning", "--offset-mv", str(offset), "--cap-w", str(cap)))
        elif offset is not None:
            steps.append(self._run_cli("set-undervolt", str(offset)))
        elif cap is not None:
            steps.append(self._run_cli("set-power-cap", str(cap)))
        ok = all(s["rc"] == 0 for s in steps)
        self._json(200, {"ok": ok, "steps": steps})

    def _api_reset(self):
        step = self._run_cli("reset")
        self._json(200, {"ok": step["rc"] == 0, "steps": [step]})

    def _api_apply(self):
        step = self._run_cli("apply")
        self._json(200, {"ok": step["rc"] == 0, "steps": [step]})

    def _api_set_profile(self):
        body = self._check_post()
        if body is None:
            return
        if not isinstance(body, dict):
            self._err(400, "body must be a JSON object")
            return
        unknown = set(body) - {"name"}
        if unknown:
            self._err(400, f"unknown field(s): {', '.join(sorted(unknown))}")
            return
        name = body.get("name")
        if not isinstance(name, str) or name.strip().upper() not in KNOWN_PROFILE_NAMES:
            self._err(400, f"name must be one of {'/'.join(KNOWN_PROFILE_NAMES)}")
            return
        # Whole-request validation done; only now may the CLI run. The
        # daemon independently re-validates and rejects an unknown name.
        step = self._run_cli("set-profile", name.strip().upper())
        self._json(200, {"ok": step["rc"] == 0, "steps": [step]})

    def _api_set_fan_curve(self):
        body = self._check_post()
        if body is None:
            return
        if not isinstance(body, dict):
            self._err(400, "body must be a JSON object")
            return
        unknown = set(body) - {"points", "off"}
        if unknown:
            self._err(400, f"unknown field(s): {', '.join(sorted(unknown))}")
            return
        off = body.get("off", False)
        if not isinstance(off, bool):
            self._err(400, "off must be a boolean")
            return
        if off:
            step = self._run_cli("set-fan-curve", "--off")
            self._json(200, {"ok": step["rc"] == 0, "steps": [step]})
            return
        points = body.get("points")
        if points is None:
            self._err(400, "provide points (list of [temp, pwm]) or off=true")
            return
        # Client-side rules mirrored here so a bad curve never reaches the
        # daemon subprocess, but the daemon's own set-fan-curve is still
        # the sole authority: it re-parses and re-validates independently.
        problems = _validate_fan_curve_points(points)
        if problems:
            self._err(400, "invalid fan curve: " + "; ".join(problems))
            return
        curve_str = _points_to_curve_str([(float(t), float(p)) for t, p in points])
        step = self._run_cli("set-fan-curve", curve_str)
        self._json(200, {"ok": step["rc"] == 0, "steps": [step]})

    def _api_bench_run(self):
        body = self._check_post()
        if body is None:
            return
        with BENCH_LOCK:
            if BENCH_PROC and BENCH_PROC["proc"].poll() is None:
                self._err(409, "bench already running")
                return
            if not isinstance(body, dict):
                self._err(400, "body must be a JSON object")
                return
            allowed = {"label", "prompts", "max_tokens"}
            unknown = set(body) - allowed
            if unknown:
                self._err(400, f"unknown field(s): {', '.join(sorted(unknown))}")
                return
            label = body.get("label", "bench")
            prompts = body.get("prompts", 6)
            max_tokens = body.get("max_tokens", 512)
            if not _valid_bench_label(label):
                self._err(400, "label must be 1..64 chars of [A-Za-z0-9._-]")
                return
            if (not isinstance(prompts, int) or isinstance(prompts, bool)
                    or prompts < 1 or prompts > 6):
                self._err(400, "prompts must be int in [1, 6]")
                return
            if (not isinstance(max_tokens, int) or isinstance(max_tokens, bool)
                    or max_tokens < 1 or max_tokens > 4096):
                self._err(400, "max_tokens must be int in [1, 4096]")
                return
            cmd = [sys.executable, str(BENCH_SCRIPT), "run",
                   "--endpoint", BENCH_ENDPOINT, "--model", BENCH_MODEL,
                   "--label", label, "--prompts", str(prompts),
                   "--max-tokens", str(max_tokens)]
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True)
            except OSError as e:
                self._err(500, f"failed to start bench: {e}")
                return
            lines = deque(maxlen=100)
            threading.Thread(target=_bench_reader, args=(proc, lines),
                             daemon=True).start()
            BENCH_PROC.clear()
            BENCH_PROC.update({"proc": proc, "label": label,
                               "started": time.time(), "lines": lines})
            self._json(200, {"ok": True, "pid": proc.pid})


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    global TOKEN, GPU, CONFIG
    ap = argparse.ArgumentParser(prog="r9700-ui")
    ap.add_argument("--port", type=int, default=7970)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--insecure-lan", action="store_true",
                    help="allow binding 0.0.0.0 (NOT recommended)")
    ap.add_argument("--open", action="store_true",
                    help="xdg-open the URL after start")
    args = ap.parse_args()

    if args.host == "0.0.0.0" and not args.insecure_lan:
        sys.exit("error: refusing to bind 0.0.0.0 without --insecure-lan")

    CONFIG = _load_config()
    pci = _discover_pci(CONFIG)
    if pci:
        GPU = GpuSysfs(pci)
    else:
        print("warning: R9700 PCI device not found; sensor reads will return null",
              file=sys.stderr)

    TOKEN = secrets.token_hex(16)
    # #4: print token on its own line; URL without the token in the log.
    url_bare = f"http://{args.host}:{args.port}/"
    url_auth = f"{url_bare}?t={TOKEN}"
    print(f"TOKEN: {TOKEN}", flush=True)
    print(url_bare, flush=True)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    if args.open:
        # --open still passes the full authenticated URL to the browser.
        subprocess.Popen(["xdg-open", url_auth], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

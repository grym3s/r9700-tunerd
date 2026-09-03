#!/usr/bin/env python3
"""r9700-ui.py — local UI backend for r9700-tunerd (R9700 / RDNA4).

Binds 127.0.0.1:7970.  All privileged ops go through
  sudo -n /usr/local/sbin/r9700-tunerd …
Never opens /dev/dri, never writes sysfs, never wakes the card.
"""
import argparse
import errno
import json
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
UI_HTML = Path(__file__).resolve().parent.parent / "ui" / "index.html"
DAEMON_CLI = "/usr/local/sbin/r9700-tunerd"
BENCH_ENDPOINT = "http://127.0.0.1:1234/v1"
BENCH_MODEL = "qwen/qwen3.8-27b@q4_k_m"

TOKEN = ""
GPU = None
CONFIG = {}
BENCH_LOCK = threading.Lock()
BENCH_PROC = {}


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
    vendor = cfg.get("VENDOR", "0x1002").lstrip("0x")
    device = cfg.get("DEVICE", "0x7551").lstrip("0x")
    subv = cfg.get("SUBSYSTEM_VENDOR", "0x1043").lstrip("0x")
    subd = cfg.get("SUBSYSTEM_DEVICE", "0x0626").lstrip("0x")
    for dev in sorted(Path("/sys/bus/pci/devices").iterdir()):
        try:
            if (dev.joinpath("vendor").read_text().strip().lstrip("0x") == vendor
                    and dev.joinpath("device").read_text().strip().lstrip("0x") == device
                    and dev.joinpath("subsystem_vendor").read_text().strip().lstrip("0x") == subv
                    and dev.joinpath("subsystem_device").read_text().strip().lstrip("0x") == subd):
                return dev
        except (OSError, FileNotFoundError):
            continue
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

    def _find_hwmon(self):
        hroot = self.pci / "hwmon"
        if not hroot.is_dir():
            return None
        for child in sorted(hroot.iterdir()):
            if child.name.startswith("hwmon"):
                return child
        return None

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
        if not self.hwmon or self.runtime_status() != "active":
            return None
        return self._read(self.hwmon / name)


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


# ─── Status payload ──────────────────────────────────────────────────────────

def _build_status(include_journal=True):
    ts = datetime.now(timezone.utc).isoformat()
    pci = str(GPU.pci) if GPU else None
    # Runtime-PM guard: these three reads are safe in any power state.
    rs = GPU.runtime_status() if GPU else None
    ps = GPU.power_state() if GPU else None
    susp = GPU.runtime_suspended_time() if GPU else None
    suspended_ms = int(susp) if susp is not None else None

    tuned = {
        "offset_mv": int(CONFIG.get("VOLTAGE_OFFSET_MV", "0")),
        "cap_w": int(CONFIG.get("POWER_LIMIT_W", "300")),
    }

    # Live sensors — ONLY when active (runtime-PM guard: never touch hwmon/pp_*
    # while suspended; EBUSY on individual reads → null field).
    live = None
    if rs == "active":
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
        sclk = GPU.read_active("pp_dpm_sclk")
        live["sclk_mhz"] = _parse_dpm_clock(sclk) if sclk else None
        mclk = GPU.read_active("pp_dpm_mclk")
        live["mclk_mhz"] = _parse_dpm_clock(mclk) if mclk else None
        busy = GPU.read_active("gpu_busy_percent")
        live["gpu_busy"] = int(busy) if busy is not None else None

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
    try:
        state_text = STATE_FILE.read_text().strip()
    except OSError:
        pass

    payload = {
        "ts": ts, "pci": pci, "runtime_status": rs, "power_state": ps,
        "suspended_ms": suspended_ms, "control": "ui",
        "tuned": tuned, "live": live, "ranges": ranges,
        "service": svc, "state_file": state_text,
    }
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
        agg = r.get("aggregates", {})
        pre = r.get("pre_run", {})
        results.append({
            "label": r.get("label"), "timestamp": r.get("timestamp"),
            "offset_mv": pre.get("vddgfx_offset_mv"),
            "cap_w": pre.get("power_cap_w"),
            "gen_tok_s": agg.get("mean_gen_tok_s"),
            "agg_tok_s": agg.get("agg_tok_s"),
            "mean_power_w": agg.get("mean_power_w"),
            "tok_s_per_w": agg.get("tok_s_per_w"),
            "max_junction_c": agg.get("max_junction_c"),
            "errors": r.get("errors", []),
            "d3cold_s": r.get("d3cold_s"),
            "stable": agg.get("offset_stable", True) and agg.get("cap_stable", True),
            "warmup_s": r.get("warmup_s"),
        })
    results.sort(key=lambda x: x.get("timestamp") or "", reverse=True)
    return results


# ─── HTTP handler ────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "r9700-ui/1.0"

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {self.address_string()} {fmt % args}",
              flush=True)

    def _auth_ok(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        tok = q.get("t", [None])[0] or self.headers.get("X-Token")
        return tok is not None and tok == TOKEN

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
        try:
            length = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError):
            self._err(400, "invalid JSON body")
            return None

    # ── GET ──

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._serve_ui()
        elif path == "/api/status":
            self._api_status()
        elif path == "/api/events":
            self._api_events()
        elif path == "/api/bench":
            self._json(200, _list_bench())
        elif path == "/api/bench/status":
            self._api_bench_status()
        elif path == "/api/profiles":
            self._api_profiles()
        elif path.startswith("/api/"):
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
                time.sleep(2)
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
        # Balanced/Performance are placeholders until Phase 4 measures them.
        profiles = [
            {"name": "Stock", "offset_mv": 0, "cap_w": 300, "measured": False},
            {"name": "Efficiency", "offset_mv": -25, "cap_w": 210, "measured": False},
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

    # ── POST ──

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if not path.startswith("/api/"):
            self._err(404, "not found")
            return
        if not self._auth_ok():
            self._err(403, "invalid or missing token")
            return
        if path == "/api/set":
            self._api_set()
        elif path == "/api/reset":
            self._api_reset()
        elif path == "/api/apply":
            self._api_apply()
        elif path == "/api/bench/run":
            self._api_bench_run()
        else:
            self._err(404, "unknown endpoint")

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
        steps = []
        offset = body.get("offset_mv")
        cap = body.get("cap_w")
        if offset is not None:
            if (not isinstance(offset, int) or isinstance(offset, bool)
                    or offset > 0 or offset < -500):
                self._err(400, "offset_mv must be int in [-500, 0]")
                return
            steps.append(self._run_cli("set-undervolt", str(offset)))
        if cap is not None:
            if (not isinstance(cap, int) or isinstance(cap, bool)
                    or cap < 50 or cap > 1000):
                self._err(400, "cap_w must be int in [50, 1000]")
                return
            steps.append(self._run_cli("set-power-cap", str(cap)))
        if not steps:
            self._err(400, "provide offset_mv and/or cap_w")
            return
        ok = all(s["rc"] == 0 for s in steps)
        self._json(200, {"ok": ok, "steps": steps})

    def _api_reset(self):
        step = self._run_cli("reset")
        self._json(200, {"ok": step["rc"] == 0, "steps": [step]})

    def _api_apply(self):
        step = self._run_cli("apply")
        self._json(200, {"ok": step["rc"] == 0, "steps": [step]})

    def _api_bench_run(self):
        body = self._check_post()
        if body is None:
            return
        with BENCH_LOCK:
            if BENCH_PROC and BENCH_PROC["proc"].poll() is None:
                self._err(409, "bench already running")
                return
            label = body.get("label", "bench")
            prompts = body.get("prompts", 6)
            max_tokens = body.get("max_tokens", 512)
            if not isinstance(label, str) or not label:
                self._err(400, "label must be a non-empty string")
                return
            if not isinstance(prompts, int) or prompts < 1 or prompts > 6:
                self._err(400, "prompts must be int in [1, 6]")
                return
            if not isinstance(max_tokens, int) or max_tokens < 1:
                self._err(400, "max_tokens must be positive int")
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
    url = f"http://{args.host}:{args.port}/?t={TOKEN}"
    print(url, flush=True)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    if args.open:
        subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

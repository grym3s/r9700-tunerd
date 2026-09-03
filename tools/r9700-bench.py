#!/usr/bin/env python3
"""r9700-bench.py — R9700 real-workload measurement harness.

Reads GPU state under a local AI inference workload. Never writes tuning.
Operator sets offset/cap via r9700-tunerd before running this tool.
"""

import argparse
import csv
import errno
import json
import os
import re
import sys
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median

CONF_PATH = Path("/etc/r9700-tunerd.conf")
DEFAULT_PCI_IDS = "1002:7551:1043:0626"

# Fixed deterministic prompts (temperature=0 makes output reproducible)
PROMPTS = [
    "Write a Python function implementing a thread-safe LRU cache with TTL expiry. Include type hints and docstrings.",
    "Explain the difference between B-tree and B+ tree index structures in PostgreSQL, and when each is preferred for range queries.",
    "Write a Rust function that parses a simplified JSON object (strings, numbers, booleans, nested objects, arrays) without external crates. Handle malformed input gracefully.",
    "Design a rate limiter using the token bucket algorithm. Provide a Python implementation with configurable burst size and refill rate, plus a unit test.",
    "Explain how RDNA4 Infinity Cache reduces memory bandwidth pressure compared to RDNA3. Include a comparison table of key parameters.",
    "Write a C program implementing a lock-free SPSC ring buffer using C11 atomics. Handle the wrap-around case correctly.",
]

# Warm-up prompt: intentionally trivial so JIT load is the only cost.
WARMUP_PROMPT = "Say hello."
WARMUP_MAX_TOKENS = 32

# Seconds of sampling excluded from aggregates (ramp window).
RAMP_S = 3.0

CSV_FIELDS = [
    "ts", "runtime_status", "power_state", "runtime_suspended_time_ms",
    "power_w", "edge_c", "junction_c", "mem_c", "fan_rpm",
    "sclk_mhz", "mclk_mhz", "vddgfx_offset_mv", "power_cap_w",
    "gpu_busy_percent",
]


# ─── PCI discovery ───────────────────────────────────────────────────────────

def discover_pci(conf_path: Path, pci_ids_override: str | None) -> Path:
    """Locate the R9700 by vendor/device/subsystem identity. Never by bus addr or card#."""
    if pci_ids_override:
        parts = pci_ids_override.split(":")
        if len(parts) != 4:
            sys.exit(f"error: --pci-ids must be VENDOR:DEVICE:SUBV:SUBD, got {pci_ids_override!r}")
        vendor, device, subv, subd = parts
    else:
        if not conf_path.exists():
            sys.exit(f"error: {conf_path} not found; use --pci-ids")
        cfg = {}
        for line in conf_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
        vendor = cfg.get("VENDOR", "0x1002").lstrip("0x")
        device = cfg.get("DEVICE", "0x7551").lstrip("0x")
        subv = cfg.get("SUBSYSTEM_VENDOR", "0x1043").lstrip("0x")
        subd = cfg.get("SUBSYSTEM_DEVICE", "0x0626").lstrip("0x")

    pci_root = Path("/sys/bus/pci/devices")
    for dev in sorted(pci_root.iterdir()):
        try:
            if (dev.joinpath("vendor").read_text().strip().lstrip("0x") == vendor
                    and dev.joinpath("device").read_text().strip().lstrip("0x") == device
                    and dev.joinpath("subsystem_vendor").read_text().strip().lstrip("0x") == subv
                    and dev.joinpath("subsystem_device").read_text().strip().lstrip("0x") == subd):
                return dev
        except (OSError, FileNotFoundError):
            continue
    sys.exit(f"error: no PCI device matching {vendor}:{device}:{subv}:{subd}")


# ─── Safe sysfs reader ───────────────────────────────────────────────────────

class GpuSysfs:
    """Reads R9700 sysfs respecting runtime-PM state.

    RDNA4/amdgpu quirk: reading hwmon or pp_od_* while the device is in
    D3cold returns EBUSY.  power/runtime_status and power_state are
    the ONLY files safe to read while suspended (proven not to trigger a
    runtime resume).  We gate every other read behind a status check and
    treat EBUSY/EAGAIN as a benign TOCTOU (device suspended between check
    and read).
    """

    def __init__(self, pci_path: Path):
        self.pci = pci_path
        self.hwmon = self._find_hwmon()

    def _find_hwmon(self) -> Path | None:
        """Select hwmon directory the same way the daemon's hwmon_dir() does:
        sorted children, first one whose name starts with 'hwmon'."""
        hroot = self.pci / "hwmon"
        if not hroot.is_dir():
            return None
        for child in sorted(hroot.iterdir()):
            if child.name.startswith("hwmon"):
                return child
        return None

    @staticmethod
    def _read(path: Path) -> str | None:
        """Read a sysfs file. Returns None on EBUSY/EAGAIN (device suspended mid-read)."""
        try:
            return path.read_text().strip()
        except OSError as e:
            if e.errno in (errno.EBUSY, errno.EAGAIN):
                return None
            raise

    def runtime_status(self) -> str | None:
        """Safe while suspended — does NOT wake the device."""
        return self._read(self.pci / "power" / "runtime_status")

    def power_state(self) -> str | None:
        """Safe while suspended. Path is <pci>/power_state (NOT under power/)."""
        return self._read(self.pci / "power_state")

    def runtime_suspended_time(self) -> str | None:
        """Accumulated ms in suspend state. Safe while suspended."""
        return self._read(self.pci / "power" / "runtime_suspended_time")

    def read_active(self, relpath: str) -> str | None:
        """Read a PCI-level sysfs file only if device is active."""
        if self.runtime_status() != "active":
            return None
        return self._read(self.pci / relpath)

    def read_hwmon(self, name: str) -> str | None:
        """Read a hwmon attribute only if device is active."""
        if not self.hwmon or self.runtime_status() != "active":
            return None
        return self._read(self.hwmon / name)


# ─── Parsers ─────────────────────────────────────────────────────────────────

def parse_dpm_clock(text: str) -> int | None:
    """Extract MHz from the '*' (current) line of pp_dpm_sclk / pp_dpm_mclk."""
    for line in text.splitlines():
        if "*" in line:
            m = re.search(r"(\d+)\s*mhz", line, re.I)
            if m:
                return int(m.group(1))
    return None


def parse_od_offset(text: str) -> int | None:
    """Extract VDDGFX offset in mV from pp_od_clk_voltage.

    Mirrors the daemon's parse_od(): look for the OD_VDDGFX_OFFSET section
    header, then grab the first '(-?\\d+) mV' on the following line.
    """
    in_offset = False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("OD_VDDGFX_OFFSET"):
            in_offset = True
            continue
        if s.startswith("OD_"):
            in_offset = False
            continue
        if in_offset and s:
            m = re.search(r"(-?\d+)\s*mV", s, re.I)
            if m:
                return int(m.group(1))
    return None


# ─── Baseline reader ─────────────────────────────────────────────────────────

def read_baseline(gpu: GpuSysfs, timeout: float = 3.0) -> tuple[int | None, float | None]:
    """Read VDDGFX offset and power cap from sysfs, retrying until the device
    is active and both values are readable, or *timeout* seconds elapse.

    Called after warm-up so the watcher has had time to restore the offset
    following a D3cold wake.
    """
    deadline = time.monotonic() + timeout
    offset: int | None = None
    cap: float | None = None
    while time.monotonic() < deadline:
        if gpu.runtime_status() == "active":
            if offset is None:
                od = gpu.read_active("pp_od_clk_voltage")
                if od:
                    offset = parse_od_offset(od)
            if cap is None:
                cap_raw = gpu.read_hwmon("power1_cap")
                if cap_raw is not None:
                    cap = int(cap_raw) / 1_000_000.0
            if offset is not None and cap is not None:
                break
        time.sleep(0.5)
    return offset, cap


# ─── Sampler ─────────────────────────────────────────────────────────────────

class Sampler:
    """Background thread that records one sysfs snapshot per interval.

    Produces exactly one row per interval whether the card is active or
    suspended.  While suspended it reads ONLY runtime_status, power_state,
    and runtime_suspended_time (the three files proven safe in D3cold).
    The thread never dies: any exception is caught, an error row is
    written, and sampling continues.
    """

    def __init__(self, gpu: GpuSysfs, interval: float = 1.0,
                 csv_writer=None, csv_file=None):
        self.gpu = gpu
        self.interval = interval
        self.csv_writer = csv_writer
        self.csv_file = csv_file
        self.rows: list[dict] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def _sample_once(self) -> dict:
        ts = time.time()
        status = self.gpu.runtime_status()
        pstate = self.gpu.power_state()
        susp_ms = self.gpu.runtime_suspended_time()
        row: dict = {"ts": ts, "runtime_status": status, "power_state": pstate}
        if susp_ms is not None:
            row["runtime_suspended_time_ms"] = int(susp_ms)

        if status == "suspended":
            # Only safe-while-suspended files; do NOT touch hwmon or pp_*.
            return row

        if status != "active":
            # Transitional (suspending/resuming) or unreadable — record and move on.
            return row

        # ── Active: full sensor read ──
        # Power: hwmon reports µW; prefer power1_average, fall back to power1_input
        pw = self.gpu.read_hwmon("power1_average") or self.gpu.read_hwmon("power1_input")
        if pw is not None:
            row["power_w"] = int(pw) / 1_000_000.0

        # Temperatures: m°C → °C
        for key, attr in [("edge_c", "temp1_input"),
                          ("junction_c", "temp2_input"),
                          ("mem_c", "temp3_input")]:
            v = self.gpu.read_hwmon(attr)
            if v is not None:
                row[key] = int(v) / 1000.0

        v = self.gpu.read_hwmon("fan1_input")
        if v is not None:
            row["fan_rpm"] = int(v)

        # DPM clocks (PCI-level, not hwmon)
        sclk = self.gpu.read_active("pp_dpm_sclk")
        if sclk:
            row["sclk_mhz"] = parse_dpm_clock(sclk)
        mclk = self.gpu.read_active("pp_dpm_mclk")
        if mclk:
            row["mclk_mhz"] = parse_dpm_clock(mclk)

        # Overdrive offset (PCI-level pp_od_clk_voltage)
        od = self.gpu.read_active("pp_od_clk_voltage")
        if od:
            row["vddgfx_offset_mv"] = parse_od_offset(od)

        # Power cap: amdgpu hwmon reports µW (daemon divides by 1_000_000)
        cap = self.gpu.read_hwmon("power1_cap")
        if cap is not None:
            row["power_cap_w"] = int(cap) / 1_000_000.0

        # GPU utilisation
        busy = self.gpu.read_active("gpu_busy_percent")
        if busy is not None:
            row["gpu_busy_percent"] = int(busy)

        return row

    def _loop(self):
        while not self._stop.is_set():
            try:
                row = self._sample_once()
            except Exception as e:
                # PCI device vanished, unexpected sysfs error, etc.
                # Record and continue — the thread must never die.
                row = {"ts": time.time(), "runtime_status": "error",
                       "power_state": str(e)}
            with self._lock:
                self.rows.append(row)
            if self.csv_writer:
                try:
                    self.csv_writer.writerow(row)
                    if self.csv_file:
                        self.csv_file.flush()
                except Exception:
                    pass  # never let a CSV write kill the sampler
            self._stop.wait(self.interval)

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)


# ─── D3cold wait ─────────────────────────────────────────────────────────────

def wait_for_d3cold(gpu: GpuSysfs, timeout: float = 60.0) -> float | None:
    """Poll safe files until D3cold or timeout. Returns elapsed seconds or None."""
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if gpu.runtime_status() == "suspended":
            if gpu.power_state() == "D3cold":
                return time.monotonic() - start
        time.sleep(0.5)
    return None


# ─── HTTP ────────────────────────────────────────────────────────────────────

def send_chat_request(endpoint: str, model: str, prompt: str,
                      max_tokens: int = 512, timeout: int = 300):
    """POST /chat/completions. Returns (data_dict|None, wall_s, error_str|None)."""
    url = endpoint.rstrip("/") + "/chat/completions"
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": max_tokens,
        "stream": False,
    }).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        return data, time.monotonic() - t0, None
    except (urllib.error.URLError, urllib.error.HTTPError,
            json.JSONDecodeError, OSError) as e:
        return None, time.monotonic() - t0, str(e)


def _extract_metrics(data: dict) -> dict:
    """Extract token counts and throughput from either LM Studio or llama.cpp
    response.  Records raw 'timings' and 'stats' for later analysis."""
    r: dict = {}

    # Token counts (both backends expose 'usage')
    usage = data.get("usage", {})
    r["prompt_tokens"] = usage.get("prompt_tokens", 0)
    r["completion_tokens"] = usage.get("completion_tokens", 0)

    # Raw sections for later analysis
    if "timings" in data:
        r["raw_timings"] = data["timings"]
    if "stats" in data:
        r["raw_stats"] = data["stats"]

    # Throughput: llama.cpp uses timings.predicted_per_second / prompt_per_second
    if "timings" in data:
        t = data["timings"]
        r["gen_tok_s"] = t.get("predicted_per_second")
        r["prompt_tok_s"] = t.get("prompt_per_second")
        # Speculative decoding metrics (llama.cpp)
        if "draft_n" in t and r.get("draft_tokens") is None:
            r["draft_tokens"] = t["draft_n"]
        if "draft_accepted" in t and r.get("accepted_tokens") is None:
            r["accepted_tokens"] = t["draft_accepted"]

    # Throughput: LM Studio uses stats.tokens_per_second
    if "stats" in data:
        s = data["stats"]
        if r.get("gen_tok_s") is None:
            r["gen_tok_s"] = s.get("tokens_per_second")
        # LM Studio may report draft/accepted token counts
        if "draft_tokens" in s:
            r["draft_tokens"] = s["draft_tokens"]
        if "accepted_tokens" in s:
            r["accepted_tokens"] = s["accepted_tokens"]

    return r


# ─── Subcommands ─────────────────────────────────────────────────────────────

def cmd_sample(args):
    gpu = GpuSysfs(discover_pci(CONF_PATH, args.pci_ids))
    if os.geteuid() == 0:
        print("warning: running as root; all reads are world-readable", file=sys.stderr)

    out = open(args.out, "w", newline="") if args.out else sys.stdout
    writer = csv.DictWriter(out, fieldnames=CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    out.flush()

    sampler = Sampler(gpu, interval=args.interval, csv_writer=writer, csv_file=out)
    sampler.start()
    try:
        if args.duration:
            time.sleep(args.duration)
        elif args.until_idle:
            # Stop after the first row where status is suspended AND
            # power_state is D3cold.
            while True:
                time.sleep(1)
                with sampler._lock:
                    if not sampler.rows:
                        continue
                    last = sampler.rows[-1]
                    if (last.get("runtime_status") == "suspended"
                            and last.get("power_state") == "D3cold"):
                        break
        else:
            time.sleep(30)
    except KeyboardInterrupt:
        pass
    finally:
        sampler.stop()
        out.flush()
        if args.out:
            out.close()


def cmd_run(args):
    gpu = GpuSysfs(discover_pci(CONF_PATH, args.pci_ids))
    if os.geteuid() == 0:
        print("warning: running as root; all reads are world-readable", file=sys.stderr)

    # ── Warm-up: load model into VRAM, let watcher restore offset ──
    warmup_s: float | None = None
    if not args.no_warmup:
        t0 = time.monotonic()
        send_chat_request(args.endpoint, args.model, WARMUP_PROMPT,
                          max_tokens=WARMUP_MAX_TOKENS)
        warmup_s = round(time.monotonic() - t0, 3)
        # Settle: model resident + watcher offset restore (~2 s) + margin.
        time.sleep(3.0)

    # ── Baseline: read offset/cap from sysfs (retry up to 3 s) ──
    baseline_offset, baseline_cap = read_baseline(gpu, timeout=3.0)
    baseline_source = "sysfs (post-warmup)"
    if baseline_offset is None and baseline_cap is None:
        baseline_source = "unavailable (timeout)"

    # ── Pre-run metadata ──
    pre = {
        "kernel": Path("/proc/sys/kernel/osrelease").read_text().strip(),
        "amdgpu_version": None,
        "vddgfx_offset_mv": baseline_offset,
        "power_cap_w": baseline_cap,
    }
    amdgpu_ver = Path("/sys/module/amdgpu/version")
    if amdgpu_ver.exists():
        pre["amdgpu_version"] = amdgpu_ver.read_text().strip()

    # ── Output paths ──
    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    ts_str = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    label = args.label or "bench"
    json_path = out_dir / f"{ts_str}_{label}.json"
    csv_path = out_dir / f"{ts_str}_{label}.csv"

    # ── Start sampler ──
    csv_file = open(csv_path, "w", newline="")
    csv_writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS, extrasaction="ignore")
    csv_writer.writeheader()
    csv_file.flush()
    sampler_start_ts = time.time()  # reference for ramp window
    sampler = Sampler(gpu, interval=args.interval, csv_writer=csv_writer, csv_file=csv_file)
    sampler.start()

    # ── Send measured requests ──
    n = min(args.prompts, len(PROMPTS))
    results = []
    for i in range(n):
        data, wall, err = send_chat_request(args.endpoint, args.model,
                                            PROMPTS[i], args.max_tokens)
        r = {"index": i, "wall_s": round(wall, 3), "error": err}
        if data is not None:
            if not isinstance(data, dict):
                r["error"] = f"unexpected JSON response type: {type(data).__name__}"
            else:
                metrics = _extract_metrics(data)
                r.update(metrics)
                # Fallback: compute tok/s from completion_tokens / wall
                if r.get("gen_tok_s") is None and r.get("completion_tokens", 0) > 0 and wall > 0:
                    r["gen_tok_s"] = round(r["completion_tokens"] / wall, 2)
                if r.get("gen_tok_s") is not None:
                    r["gen_tok_s"] = round(r["gen_tok_s"], 2)
        results.append(r)

    # ── Stop sampler (workload done; do NOT keep GPU awake) ──
    sampler.stop()
    csv_file.close()

    # ── Split active rows into ramp (first RAMP_S) and steady-state ──
    active_rows = [row for row in sampler.rows if row.get("runtime_status") == "active"]
    ramp_rows = [row for row in active_rows if row["ts"] - sampler_start_ts < RAMP_S]
    steady_rows = [row for row in active_rows if row["ts"] - sampler_start_ts >= RAMP_S]

    # ── Aggregates (steady-state only) ──
    ok = [r for r in results if not r["error"]]
    errors = [r for r in results if r["error"]]
    gen_list = [r["gen_tok_s"] for r in ok if r.get("gen_tok_s") is not None]
    prompt_list = [r["prompt_tok_s"] for r in ok if r.get("prompt_tok_s") is not None]
    total_comp = sum(r.get("completion_tokens", 0) for r in ok)
    total_wall = sum(r["wall_s"] for r in ok)
    agg_tok_s = total_comp / total_wall if total_wall > 0 else 0.0

    # Draft acceptance (speculative decoding)
    total_draft = sum(r.get("draft_tokens", 0) for r in ok)
    total_accepted = sum(r.get("accepted_tokens", 0) for r in ok)
    draft_acceptance = (total_accepted / total_draft) if total_draft > 0 else None

    # Steady-state sensor aggregates
    powers = [row["power_w"] for row in steady_rows if "power_w" in row]
    junctions = [row["junction_c"] for row in steady_rows if "junction_c" in row]
    sclk_vals = [row["sclk_mhz"] for row in steady_rows if "sclk_mhz" in row]

    # ── Ramp aggregates (first RAMP_S of sampling) ──
    ramp_powers = [row["power_w"] for row in ramp_rows if "power_w" in row]
    ramp_junctions = [row["junction_c"] for row in ramp_rows if "junction_c" in row]
    ramp_sclk = [row["sclk_mhz"] for row in ramp_rows if "sclk_mhz" in row]
    ramp = {
        "n_samples": len(ramp_rows),
        "mean_power_w": round(mean(ramp_powers), 2) if ramp_powers else None,
        "max_power_w": round(max(ramp_powers), 2) if ramp_powers else None,
        "max_junction_c": round(max(ramp_junctions), 1) if ramp_junctions else None,
        "min_sclk_mhz": min(ramp_sclk) if ramp_sclk else None,
        "max_sclk_mhz": max(ramp_sclk) if ramp_sclk else None,
    }

    # ── Stability check (steady-state samples vs baseline) ──
    offset_stable = True
    cap_stable = True
    offset_mismatch_count = 0
    offset_mismatch_first_s: float | None = None
    offset_mismatch_last_s: float | None = None
    cap_mismatch_count = 0
    cap_mismatch_first_s: float | None = None
    cap_mismatch_last_s: float | None = None

    if baseline_offset is not None:
        for row in steady_rows:
            if "vddgfx_offset_mv" in row and row["vddgfx_offset_mv"] != baseline_offset:
                offset_mismatch_count += 1
                dt = round(row["ts"] - sampler_start_ts, 3)
                if offset_mismatch_first_s is None:
                    offset_mismatch_first_s = dt
                offset_mismatch_last_s = dt
        offset_stable = (offset_mismatch_count == 0)

    if baseline_cap is not None:
        for row in steady_rows:
            if "power_cap_w" in row and abs(row["power_cap_w"] - baseline_cap) >= 0.1:
                cap_mismatch_count += 1
                dt = round(row["ts"] - sampler_start_ts, 3)
                if cap_mismatch_first_s is None:
                    cap_mismatch_first_s = dt
                cap_mismatch_last_s = dt
        cap_stable = (cap_mismatch_count == 0)

    # ── Energy (trapezoidal integration over consecutive steady-state rows;
    #    skip pairs whose gap exceeds 3× interval — a suspend/resume boundary) ──
    energy_j = 0.0
    for i in range(1, len(steady_rows)):
        prev = steady_rows[i - 1]
        curr = steady_rows[i]
        if "power_w" not in prev or "power_w" not in curr:
            continue
        dt = curr["ts"] - prev["ts"]
        if dt > 3 * args.interval:
            continue
        energy_j += 0.5 * (prev["power_w"] + curr["power_w"]) * max(dt, 0.1)

    # ── D3cold wait ──
    d3cold_s = wait_for_d3cold(gpu, timeout=60)

    # ── Build result ──
    result = {
        "timestamp": ts_str,
        "label": label,
        "endpoint": args.endpoint,
        "model": args.model,
        "warmup_s": warmup_s,
        "pre_run": pre,
        "baseline_source": baseline_source,
        "requests": results,
        "aggregates": {
            "mean_gen_tok_s": round(mean(gen_list), 2) if gen_list else None,
            "median_gen_tok_s": round(median(gen_list), 2) if gen_list else None,
            "mean_prompt_tok_s": round(mean(prompt_list), 2) if prompt_list else None,
            "agg_tok_s": round(agg_tok_s, 2),
            "total_completion_tokens": total_comp,
            "total_wall_s": round(total_wall, 2),
            "mean_power_w": round(mean(powers), 2) if powers else None,
            "max_power_w": round(max(powers), 2) if powers else None,
            "tok_per_joule": round(total_comp / energy_j, 4) if energy_j > 0 else None,
            "tok_s_per_w": round(agg_tok_s / mean(powers), 4) if (powers and agg_tok_s is not None) else None,
            "max_junction_c": round(max(junctions), 1) if junctions else None,
            "min_sclk_mhz": min(sclk_vals) if sclk_vals else None,
            "max_sclk_mhz": max(sclk_vals) if sclk_vals else None,
            "offset_stable": offset_stable,
            "cap_stable": cap_stable,
            "offset_mismatch_count": offset_mismatch_count,
            "offset_mismatch_first_s": offset_mismatch_first_s,
            "offset_mismatch_last_s": offset_mismatch_last_s,
            "cap_mismatch_count": cap_mismatch_count,
            "cap_mismatch_first_s": cap_mismatch_first_s,
            "cap_mismatch_last_s": cap_mismatch_last_s,
            "energy_j": round(energy_j, 1),
            "draft_acceptance": round(draft_acceptance, 4) if draft_acceptance is not None else None,
            "draft_tokens_total": total_draft,
            "accepted_tokens_total": total_accepted,
        },
        "ramp": ramp,
        "errors": [r["error"] for r in errors],
        "d3cold_s": round(d3cold_s, 2) if d3cold_s is not None else None,
        "n_samples": len(sampler.rows),
    }
    json_path.write_text(json.dumps(result, indent=2))

    # ── Summary ──
    a = result["aggregates"]
    print(f"\n{'='*60}")
    print(f"  R9700 bench  {label}  {ts_str}")
    print(f"{'='*60}")
    print(f"  Model:          {args.model}")
    if warmup_s is not None:
        print(f"  Warm-up:        {warmup_s} s")
    else:
        print(f"  Warm-up:        skipped (--no-warmup)")
    print(f"  Offset:         {pre['vddgfx_offset_mv']} mV  (stable: {offset_stable}, source: {baseline_source})")
    print(f"  Cap:            {pre['power_cap_w']} W   (stable: {cap_stable}, source: {baseline_source})")
    if offset_mismatch_count > 0:
        print(f"  Offset drift:   {offset_mismatch_count} sample(s)  "
              f"first={offset_mismatch_first_s}s  last={offset_mismatch_last_s}s")
    if cap_mismatch_count > 0:
        print(f"  Cap drift:      {cap_mismatch_count} sample(s)  "
              f"first={cap_mismatch_first_s}s  last={cap_mismatch_last_s}s")
    print(f"  Gen tok/s:      mean={a['mean_gen_tok_s']}  median={a['median_gen_tok_s']}")
    print(f"  Prompt tok/s:   mean={a['mean_prompt_tok_s']}")
    print(f"  Agg tok/s:      {a['agg_tok_s']}  ({total_comp} tok / {a['total_wall_s']} s)")
    print(f"  Power:          mean={a['mean_power_w']} W  max={a['max_power_w']} W")
    print(f"  Efficiency:     {a['tok_s_per_w']} tok/s/W  {a['tok_per_joule']} tok/J")
    print(f"  Max junction:   {a['max_junction_c']} °C")
    print(f"  SCLK range:     {a['min_sclk_mhz']}–{a['max_sclk_mhz']} MHz")
    if ramp["n_samples"] > 0:
        print(f"  Ramp ({RAMP_S:.0f}s):     n={ramp['n_samples']}  "
              f"mean_P={ramp['mean_power_w']} W  max_jc={ramp['max_junction_c']} °C  "
              f"SCLK={ramp['min_sclk_mhz']}–{ramp['max_sclk_mhz']} MHz")
    if total_draft > 0:
        print(f"  Draft accept:   {a['draft_acceptance']:.3f}  ({total_accepted}/{total_draft})")
    else:
        print(f"  Draft accept:   -")
    print(f"  Errors:         {len(errors)}")
    print(f"  D3cold:         {d3cold_s:.1f} s" if d3cold_s else "  D3cold:         TIMEOUT")
    print(f"{'='*60}")
    print(f"  JSON: {json_path}")
    print(f"  CSV:  {csv_path}")


def _fmt(val, spec: str = ".1f") -> str:
    """Format a value for the compare table; print '-' if None."""
    if val is None:
        return "-"
    return format(val, spec)


def cmd_compare(args):
    d = Path(args.dir).expanduser()
    if not d.is_dir():
        sys.exit(f"error: {d} is not a directory")
    files = sorted(d.glob("*.json"))
    if not files:
        sys.exit(f"error: no JSON files in {d}")

    rows = []
    for f in files:
        try:
            rows.append(json.loads(f.read_text()))
        except (json.JSONDecodeError, OSError):
            continue
    if not rows:
        sys.exit("error: no valid JSON files found")

    rows.sort(key=lambda r: (r.get("aggregates", {}).get("tok_s_per_w") or 0), reverse=True)

    hdr = (f"{'label':<22} {'off_mV':>7} {'cap_W':>6} {'gen_t/s':>8} "
           f"{'agg_t/s':>8} {'mean_W':>7} {'t/s/W':>7} {'max_jc':>7} "
           f"{'warm_s':>7} {'stable':>6} {'err':>4} {'d3c_s':>6}")
    print(hdr)
    print("─" * len(hdr))
    for r in rows:
        agg = r.get("aggregates", {})
        pre = r.get("pre_run", {})
        off = pre.get("vddgfx_offset_mv")
        cap = pre.get("power_cap_w")
        warm = r.get("warmup_s")
        # stable = Y if both offset and cap are stable (or unavailable)
        o_stable = agg.get("offset_stable", True)
        c_stable = agg.get("cap_stable", True)
        stable_str = "Y" if (o_stable and c_stable) else "N"
        print(
            f"{r.get('label', '?'):<22} "
            f"{_fmt(off, 'd'):>7} "
            f"{_fmt(cap, '.0f'):>6} "
            f"{_fmt(agg.get('mean_gen_tok_s'), '.1f'):>8} "
            f"{_fmt(agg.get('agg_tok_s'), '.1f'):>8} "
            f"{_fmt(agg.get('mean_power_w'), '.1f'):>7} "
            f"{_fmt(agg.get('tok_s_per_w'), '.2f'):>7} "
            f"{_fmt(agg.get('max_junction_c'), '.1f'):>7} "
            f"{_fmt(warm, '.1f'):>7} "
            f"{stable_str:>6} "
            f"{len(r.get('errors', [])):>4} "
            f"{_fmt(r.get('d3cold_s'), '.1f'):>6}"
        )


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        prog="r9700-bench",
        description="R9700 real-workload measurement harness (read-only).")
    ap.add_argument("--pci-ids", default=None,
                    help=f"override PCI identity VENDOR:DEVICE:SUBV:SUBD (default: {DEFAULT_PCI_IDS})")
    sub = ap.add_subparsers(dest="cmd", required=True)

    # sample
    sp = sub.add_parser("sample", help="sample GPU state to CSV")
    sp.add_argument("--interval", type=float, default=1.0, help="seconds between samples")
    sp.add_argument("--duration", type=float, default=None, help="stop after N seconds")
    sp.add_argument("--until-idle", action="store_true",
                    help="stop when GPU reaches suspended + D3cold")
    sp.add_argument("--out", default=None, help="output CSV file (default: stdout)")
    sp.set_defaults(func=cmd_sample)

    # run
    rp = sub.add_parser("run", help="run workload + record")
    rp.add_argument("--endpoint", required=True, help="OpenAI-compatible base URL")
    rp.add_argument("--model", required=True, help="model id")
    rp.add_argument("--prompts", type=int, default=6, help="number of prompts (max 6)")
    rp.add_argument("--max-tokens", type=int, default=512)
    rp.add_argument("--concurrency", type=int, default=1,
                    help="(reserved; currently sequential only)")
    rp.add_argument("--label", default=None, help="label for output files")
    rp.add_argument("--out", default="~/r9700-bench", help="output directory")
    rp.add_argument("--interval", type=float, default=1.0, help="seconds between samples")
    rp.add_argument("--no-warmup", action="store_true",
                    help="skip warm-up request and 3 s settle (model already resident)")
    rp.set_defaults(func=cmd_run)

    # compare
    cp = sub.add_parser("compare", help="compare JSON results in a directory")
    cp.add_argument("dir", help="directory containing bench JSON files")
    cp.set_defaults(func=cmd_compare)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""r9700-matrix.py — Priority-4 stepping script (prepare-only).

Characterises the R9700 undervolt / power-cap space ONE STEP AT A TIME
with hard safety gates.  Run as root by the operator.

Usage:
    python3 tools/r9700-matrix.py --endpoint http://localhost:1234/v1 \
        --model qwen2.5-32b --dry-run

    python3 tools/r9700-matrix.py --report --out ~/r9700-bench
"""

import argparse
import csv
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DAEMON = "/usr/local/sbin/r9700-tunerd"


def daemon_cmd(*args: str) -> list[str]:
    """Daemon CLI invocation; goes through sudo -n when not root (narrow sudoers rule)."""
    import os
    prefix = [] if os.geteuid() == 0 else ["sudo", "-n"]
    return prefix + [DAEMON, *args]
BENCH_DEFAULT = "tools/r9700-bench.py"
MATRIX_CSV = "matrix.csv"
CONF_PATH = Path("/etc/r9700-tunerd.conf")

# ── Kernel-log gate: dangerous patterns (case-insensitive) ──────────────────
DANGEROUS_KERNEL = [
    re.compile(r"ring\s+timeout", re.I),
    re.compile(r"GPU\s+reset", re.I),
    re.compile(r"SMU\s+timeout", re.I),
    re.compile(r"\bAER\b"),
    re.compile(r"device\s+removed", re.I),
    re.compile(r"amdgpu.*\bfatal\b", re.I),
    re.compile(r"amdgpu.*\bhang\b", re.I),
]

# ── Kernel-log gate: benign OD re-upload lines (daemon watcher after D3cold) ─
BENIGN_KERNEL = [
    re.compile(r"Failed to upload overdrive table", re.I),
    re.compile(r"OD_UNSUPPORTED_FEATURE", re.I),
    re.compile(r"Failed to upload customized OD settings", re.I),
]

MATRIX_FIELDS = [
    "offset_mv", "cap_w", "gen_tok_s", "agg_tok_s", "mean_w",
    "tok_s_per_w", "max_junction_c", "errors", "d3cold_s", "verdict",
]


# ─── Helpers ─────────────────────────────────────────────────────────────────

def run_cmd(cmd: list[str], timeout: float = 30) -> tuple[int, str, str]:
    """Run a command; return (exit_code, stdout, stderr)."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"timeout after {timeout}s"
    except FileNotFoundError:
        return -1, "", f"command not found: {cmd[0]}"


def read_config() -> dict:
    """Read POWER_LIMIT_W and VOLTAGE_OFFSET_MV from /etc/r9700-tunerd.conf.

    Returns a dict that may contain 'cap_w' and 'offset_mv' (both int).
    """
    cfg: dict = {}
    if not CONF_PATH.exists():
        return cfg
    try:
        for line in CONF_PATH.read_text().splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip()
            if key == "POWER_LIMIT_W":
                try:
                    cfg["cap_w"] = int(val)
                except ValueError:
                    pass
            elif key == "VOLTAGE_OFFSET_MV":
                try:
                    cfg["offset_mv"] = int(val)
                except ValueError:
                    pass
    except OSError:
        pass
    return cfg


def parse_status(stdout: str) -> dict:
    """Extract current values and valid ranges from r9700-tunerd status.

    Handles two formats:

    Active:
        power1_cap_w=210 (min=210 default=300 max=330)
        vddgfx_offset_mv=-25 (range -200..0)

    Suspended:
        runtime_status=suspended
        sensors=skipped (not active; refusing to wake)
        cached_ranges: cap=210..330 W (default=300) vo=-200..0 mV (age=22s)

    Returns a dict that may contain:
        offset_mv, cap_w, offset_min, offset_max, cap_min, cap_max, suspended
    """
    info: dict = {}

    # Detect suspended state
    if re.search(r"runtime_status\s*=\s*suspended", stdout, re.I):
        info["suspended"] = True

    # ── Active format: current values + ranges ──
    # power1_cap_w=210 (min=210 default=300 max=330)
    m = re.search(
        r"power1_cap_w\s*=\s*(\d+)\s*\(min=(\d+)\s+default=\d+\s+max=(\d+)\)",
        stdout,
    )
    if m:
        info["cap_w"] = int(m.group(1))
        info["cap_min"] = int(m.group(2))
        info["cap_max"] = int(m.group(3))

    # vddgfx_offset_mv=-25 (range -200..0)
    m = re.search(
        r"vddgfx_offset_mv\s*=\s*(-?\d+)\s*\(range\s+(-?\d+)\.\.(-?\d+)\)",
        stdout,
    )
    if m:
        info["offset_mv"] = int(m.group(1))
        info["offset_min"] = int(m.group(2))
        info["offset_max"] = int(m.group(3))

    # ── Suspended format: cached_ranges ──
    # cached_ranges: cap=210..330 W (default=300) vo=-200..0 mV (age=22s)
    m = re.search(
        r"cached_ranges:\s*cap=(\d+)\.\.(\d+)\s*W.*?vo=(-?\d+)\.\.(-?\d+)\s*mV",
        stdout,
    )
    if m:
        info["cap_min"] = int(m.group(1))
        info["cap_max"] = int(m.group(2))
        info["offset_min"] = int(m.group(3))
        info["offset_max"] = int(m.group(4))

    return info


def check_kernel_logs(since_iso: str) -> list[str]:
    """Return dangerous kernel lines since *since_iso*, ignoring benign OD re-uploads.

    Dangerous patterns are checked FIRST: a line matching both a dangerous
    and a benign pattern is classified as dangerous (not skipped).
    """
    rc, out, _ = run_cmd(["journalctl", "-k", "--since", since_iso, "--no-pager"], timeout=15)
    if rc != 0:
        return []  # journalctl unavailable; don't block on it
    dangerous = []
    for line in out.splitlines():
        # Dangerous check takes priority over benign skip
        if any(p.search(line) for p in DANGEROUS_KERNEL):
            dangerous.append(line.strip())
        elif any(p.search(line) for p in BENIGN_KERNEL):
            continue  # benign-only line; skip
    return dangerous


def wait_d3cold(timeout: float = 60.0) -> bool:
    """Poll daemon status until D3cold is reported or timeout elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rc, out, _ = run_cmd(daemon_cmd("status"), timeout=10)
        if rc == 0 and "D3cold" in out:
            return True
        time.sleep(1.0)
    return False


def verify_readback(offset_mv: int, cap_w: int, timeout: float = 10.0) -> bool:
    """Verify the daemon reports the target offset/cap (retry up to *timeout*).

    Active card: compare the live sysfs readback in `status`.
    Suspended card (D3cold): the daemon validated against cached ranges and
    wrote the config; the watcher applies it on the next wake.  Verify the
    config instead — the harness's offset_stable/cap_stable gates then prove
    the live apply once the bench wakes the card.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rc, out, _ = run_cmd(daemon_cmd("status"), timeout=10)
        if rc == 0:
            info = parse_status(out)
            if info.get("suspended"):
                cfg = read_config()
                if cfg.get("offset_mv") == offset_mv and cfg.get("cap_w") == cap_w:
                    print("  verify: card suspended; config holds target, "
                          "watcher applies on wake", file=sys.stderr)
                    return True
            elif info.get("offset_mv") == offset_mv and info.get("cap_w") == cap_w:
                return True
        time.sleep(1.0)
    return False


def apply_and_verify(offset_mv: int, cap_w: int) -> bool:
    """Apply offset + cap via daemon CLI, then verify readback (retry ≤ 10 s)."""
    rc, _, err = run_cmd(daemon_cmd("set-undervolt", str(offset_mv)), timeout=15)
    if rc != 0:
        print(f"  FAIL: set-undervolt {offset_mv} → rc={rc} {err.strip()}", file=sys.stderr)
        return False
    rc, _, err = run_cmd(daemon_cmd("set-power-cap", str(cap_w)), timeout=15)
    if rc != 0:
        print(f"  FAIL: set-power-cap {cap_w} → rc={rc} {err.strip()}", file=sys.stderr)
        return False
    # Verify readback matches target (retry up to 10 s)
    if verify_readback(offset_mv, cap_w):
        return True
    print(f"  FAIL: verify timeout — readback ≠ target ({offset_mv} mV / {cap_w} W)",
          file=sys.stderr)
    return False


def restore_safe_point(offset_mv: int, cap_w: int) -> bool:
    """Restore the previous safe point and verify the readback."""
    print(f"  RESTORE: set-undervolt {offset_mv} mV, set-power-cap {cap_w} W", file=sys.stderr)
    rc, _, err = run_cmd(daemon_cmd("set-undervolt", str(offset_mv)), timeout=15)
    if rc != 0:
        print(f"  RESTORE FAIL: set-undervolt rc={rc} {err.strip()}", file=sys.stderr)
        return False
    rc, _, err = run_cmd(daemon_cmd("set-power-cap", str(cap_w)), timeout=15)
    if rc != 0:
        print(f"  RESTORE FAIL: set-power-cap rc={rc} {err.strip()}", file=sys.stderr)
        return False
    if verify_readback(offset_mv, cap_w):
        print("  RESTORE: verified OK", file=sys.stderr)
        return True
    print("  RESTORE: verify timeout — MANUAL CHECK REQUIRED", file=sys.stderr)
    return False


def load_existing(out_dir: Path) -> set[tuple[int, int]]:
    """Return set of (offset, cap) already present in matrix.csv."""
    csv_path = out_dir / MATRIX_CSV
    if not csv_path.exists():
        return set()
    measured = set()
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            try:
                measured.add((int(row["offset_mv"]), int(row["cap_w"])))
            except (ValueError, KeyError):
                continue
    return measured


def append_row(out_dir: Path, row: dict):
    """Append one row to matrix.csv (write header if file is new)."""
    csv_path = out_dir / MATRIX_CSV
    new = not csv_path.exists()
    with open(csv_path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=MATRIX_FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)


def find_bench_json(out_dir: Path, label: str) -> Path | None:
    """Find the most recent bench JSON matching the label."""
    candidates = sorted(out_dir.glob(f"*_{label}.json"), key=lambda p: p.stat().st_mtime)
    return candidates[-1] if candidates else None


def print_report(out_dir: Path):
    """Print matrix.csv sorted by tok/s/W; mark best row per cap."""
    csv_path = out_dir / MATRIX_CSV
    if not csv_path.exists():
        sys.exit(f"error: {csv_path} not found")
    rows = []
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            rows.append(row)
    if not rows:
        print("matrix.csv is empty")
        return

    def tsw(r):
        try:
            return float(r.get("tok_s_per_w") or 0)
        except ValueError:
            return 0.0

    rows.sort(key=tsw, reverse=True)
    best_per_cap: dict[int, int] = {}
    for i, r in enumerate(rows):
        cap = int(r["cap_w"])
        if cap not in best_per_cap:
            best_per_cap[cap] = i

    hdr = (f"{'off_mV':>7} {'cap_W':>6} {'gen_t/s':>8} {'agg_t/s':>8} "
           f"{'mean_W':>7} {'t/s/W':>7} {'max_jc':>7} {'err':>4} {'d3c_s':>6} {'verdict':>10}")
    print(hdr)
    print("─" * len(hdr))
    for i, r in enumerate(rows):
        mark = " ★" if i in best_per_cap.values() else ""
        print(
            f"{r['offset_mv']:>7} {r['cap_w']:>6} "
            f"{r.get('gen_tok_s', '-'):>8} {r.get('agg_tok_s', '-'):>8} "
            f"{r.get('mean_w', '-'):>7} {r.get('tok_s_per_w', '-'):>7} "
            f"{r.get('max_junction_c', '-'):>7} {r.get('errors', '0'):>4} "
            f"{r.get('d3cold_s', '-'):>6} {r.get('verdict', '-'):>10}{mark}"
        )
    print("\n★ = best tok/s/W per cap")


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        prog="r9700-matrix",
        description="R9700 undervolt/power-cap stepping script (prepare-only).")
    ap.add_argument("--endpoint", default=None, help="OpenAI-compatible base URL")
    ap.add_argument("--model", default=None, help="model id")
    ap.add_argument("--offsets", default="-25,-50,-75,-100",
                    help="comma-separated VDDGFX offsets in mV (negative)")
    ap.add_argument("--caps", default="210",
                    help="comma-separated power caps in W")
    ap.add_argument("--out", default="~/r9700-bench", help="output directory")
    ap.add_argument("--repeats", type=int, default=1, help="repeats per combo")
    ap.add_argument("--start-from", action="store_true",
                    help="skip combos already in matrix.csv")
    ap.add_argument("--dry-run", action="store_true",
                    help="print plan and commands, execute nothing")
    ap.add_argument("--allow-below-100", action="store_true",
                    help="permit offsets below -100 mV")
    ap.add_argument("--allow-missing-baseline", action="store_true",
                    help="do not fail gate if bench JSON is missing")
    ap.add_argument("--report", action="store_true",
                    help="print matrix.csv as sorted table and exit")
    ap.add_argument("--bench", default=BENCH_DEFAULT,
                    help=f"path to r9700-bench.py (default: {BENCH_DEFAULT})")
    args = ap.parse_args()

    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── --report mode: print table and exit ──
    if args.report:
        print_report(out_dir)
        return

    # ── Parse offsets and caps ──
    try:
        offsets = [int(x) for x in args.offsets.split(",") if x.strip()]
    except ValueError:
        sys.exit(f"error: --offsets must be comma-separated ints, got {args.offsets!r}")
    try:
        caps = [int(x) for x in args.caps.split(",") if x.strip()]
    except ValueError:
        sys.exit(f"error: --caps must be comma-separated ints, got {args.caps!r}")

    # ── Validate offsets: negative only, ≥ -100 unless overridden ──
    for off in offsets:
        if off > 0:
            sys.exit(f"error: offset {off} mV is positive; only undervolt (negative) allowed")
        if off < -100 and not args.allow_below_100:
            sys.exit(f"error: offset {off} mV below -100; use --allow-below-100 to override")

    # ── Read config for safe point (authoritative restore target) ──
    cfg = read_config()
    safe_offset = cfg.get("offset_mv", 0)
    safe_cap = cfg.get("cap_w", 210)
    if "offset_mv" not in cfg or "cap_w" not in cfg:
        print(f"  WARNING: could not read full config from {CONF_PATH}; "
              f"safe point defaults to offset={safe_offset} mV, cap={safe_cap} W",
              file=sys.stderr)

    # ── Get live ranges from daemon ──
    rc, status_out, status_err = run_cmd(daemon_cmd("status"), timeout=15)
    if rc != 0:
        sys.exit(f"error: r9700-tunerd status failed (rc={rc}): {status_err.strip()}")
    live = parse_status(status_out)

    if live.get("suspended"):
        print("GPU is suspended (D3cold). Using cached ranges; "
              "current values from config.")
        # When suspended, current values come from config, not status
        live["offset_mv"] = safe_offset
        live["cap_w"] = safe_cap
    else:
        print(f"Live: offset={live.get('offset_mv', '?')} mV  "
              f"cap={live.get('cap_w', '?')} W")

    if "offset_min" in live:
        print(f"  offset range: {live['offset_min']}..{live['offset_max']} mV")
    if "cap_min" in live:
        print(f"  cap range:    {live['cap_min']}..{live['cap_max']} W")

    # ── Validate against live ranges ──
    if "offset_min" in live:
        for off in offsets:
            if not (live["offset_min"] <= off <= live["offset_max"]):
                sys.exit(f"error: offset {off} outside live range "
                         f"{live['offset_min']}..{live['offset_max']}")
    if "cap_min" in live:
        for cap in caps:
            if not (live["cap_min"] <= cap <= live["cap_max"]):
                sys.exit(f"error: cap {cap} outside live range "
                         f"{live['cap_min']}..{live['cap_max']}")

    # ── Build plan: per cap, offsets least→most aggressive ──
    offsets_sorted = sorted(offsets, reverse=True)  # -25, -50, -75, -100
    plan: list[tuple[int, int, int]] = []
    for cap in caps:
        for off in offsets_sorted:
            for r in range(1, args.repeats + 1):
                plan.append((off, cap, r))

    # ── --start-from: skip already-measured combos ──
    if args.start_from:
        measured = load_existing(out_dir)
        plan = [(o, c, r) for (o, c, r) in plan if (o, c) not in measured]
        if not plan:
            print("All combos already measured; nothing to do.")
            return

    # ── Dry-run: print plan, execute nothing ──
    if args.dry_run:
        print(f"\n{'='*60}")
        print(f"  DRY-RUN PLAN  ({len(plan)} steps)")
        print(f"{'='*60}")
        print(f"  Endpoint:  {args.endpoint}")
        print(f"  Model:     {args.model}")
        print(f"  Offsets:   {offsets_sorted}")
        print(f"  Caps:      {caps}")
        print(f"  Repeats:   {args.repeats}")
        print(f"  Output:    {out_dir}")
        print(f"  Bench:     {args.bench}")
        print(f"  Safe point (config): offset={safe_offset} mV, cap={safe_cap} W")
        print(f"{'─'*60}")
        for i, (off, cap, r) in enumerate(plan, 1):
            label = f"vo{off}_cap{cap}_r{r}"
            print(f"  {i:2d}. {DAEMON} set-undervolt {off}  &&  {DAEMON} set-power-cap {cap}")
            print(f"      {DAEMON} status   (verify readback)")
            print(f"      python3 {args.bench} run --endpoint … --label {label} --out {out_dir}")
            print(f"      gates → append {MATRIX_CSV} → wait D3cold")
        print(f"{'='*60}")
        print("  DRY-RUN: no commands executed.")
        return

    # ── Require endpoint/model for real run ──
    if not args.endpoint or not args.model:
        sys.exit("error: --endpoint and --model required for real run")

    # ── Safe point is the CONFIG values (not parsed status) ──
    # This ensures a suspended card cannot produce a wrong restore target.
    first_gen_by_cap: dict[int, float] = {}  # cap → first step's gen tok/s

    try:
        for step_idx, (off, cap, r) in enumerate(plan, 1):
            label = f"vo{off}_cap{cap}_r{r}"
            print(f"\n[step {step_idx}/{len(plan)}] offset={off} mV  cap={cap} W  r={r}")

            # (1) Record current safe point (always config values)
            print(f"  safe point: offset={safe_offset} mV, cap={safe_cap} W")

            # (2) Apply via daemon CLI + (3) verify readback
            if not apply_and_verify(off, cap):
                raise RuntimeError(f"apply/verify failed for {off} mV / {cap} W")

            # (4) Run harness
            step_start_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
            bench_cmd = [
                sys.executable, args.bench, "run",
                "--endpoint", args.endpoint,
                "--model", args.model,
                "--label", label,
                "--out", str(out_dir),
            ]
            print(f"  bench: {' '.join(bench_cmd)}")
            rc, _, bench_err = run_cmd(bench_cmd, timeout=600)

            # (5) GATES — all must pass or we restore + exit
            gate_failures: list[str] = []

            # Gate 1: harness exit 0
            if rc != 0:
                gate_failures.append(f"harness exit code {rc}")

            # Locate bench JSON for metric gates
            json_path = find_bench_json(out_dir, label)
            bench_data = None
            if json_path:
                try:
                    bench_data = json.loads(json_path.read_text())
                except (json.JSONDecodeError, OSError) as e:
                    gate_failures.append(f"cannot read bench JSON: {e}")

            # Gate 7: missing baseline (no bench JSON at all)
            if bench_data is None and not args.allow_missing_baseline:
                gate_failures.append(
                    "missing baseline: no bench JSON found "
                    "(use --allow-missing-baseline to override)")
            elif bench_data is None and args.allow_missing_baseline:
                print("  WARNING: missing baseline (bench JSON not found); "
                      "continuing per --allow-missing-baseline", file=sys.stderr)

            if bench_data:
                agg = bench_data.get("aggregates", {})
                errors_list = bench_data.get("errors", [])
                d3cold_s = bench_data.get("d3cold_s")
                gen_tok_s = agg.get("mean_gen_tok_s")

                # Gate 2: zero harness errors
                if len(errors_list) > 0:
                    gate_failures.append(
                        f"{len(errors_list)} harness error(s): {errors_list[:3]}")

                # Gate 3a: the live value the harness measured after wake IS the target
                pre = bench_data.get("pre_run", {})
                if pre.get("vddgfx_offset_mv") != off:
                    gate_failures.append(
                        f"live offset after wake {pre.get('vddgfx_offset_mv')} != target {off}")
                if pre.get("power_cap_w") is not None and abs(pre["power_cap_w"] - cap) >= 0.1:
                    gate_failures.append(
                        f"live cap after wake {pre.get('power_cap_w')} != target {cap}")

                # Gate 3: offset and cap reported stable
                if not agg.get("offset_stable", True):
                    gate_failures.append("offset not stable during run")
                if not agg.get("cap_stable", True):
                    gate_failures.append("cap not stable during run")

                # Gate 5: d3cold_s not null (GPU actually reached D3cold)
                if d3cold_s is None:
                    gate_failures.append("d3cold_s is null (GPU did not reach D3cold)")

                # Gate 6: gen tok/s ≥ 70 % of first step at same cap
                if gen_tok_s is not None:
                    if cap in first_gen_by_cap:
                        threshold = first_gen_by_cap[cap] * 0.70
                        if gen_tok_s < threshold:
                            gate_failures.append(
                                f"gen {gen_tok_s:.1f} < 70% of first "
                                f"({first_gen_by_cap[cap]:.1f}) at cap {cap} W")
                    else:
                        first_gen_by_cap[cap] = gen_tok_s

            # Gate 4: no dangerous kernel lines since step start
            dangerous = check_kernel_logs(step_start_iso)
            if dangerous:
                gate_failures.append(
                    f"{len(dangerous)} dangerous kernel line(s): {dangerous[:3]}")

            # ── Evaluate ──
            if gate_failures:
                print(f"\n  GATE FAILURES ({len(gate_failures)}):", file=sys.stderr)
                for gf in gate_failures:
                    print(f"    ✗ {gf}", file=sys.stderr)
                restore_safe_point(safe_offset, safe_cap)
                print(f"\n  ABORTED at step {step_idx}. Safe point restored to "
                      f"offset={safe_offset} mV, cap={safe_cap} W.", file=sys.stderr)
                sys.exit(1)

            # (6) Append row to matrix.csv
            if bench_data:
                agg = bench_data.get("aggregates", {})
                row = {
                    "offset_mv": off, "cap_w": cap,
                    "gen_tok_s": agg.get("mean_gen_tok_s", ""),
                    "agg_tok_s": agg.get("agg_tok_s", ""),
                    "mean_w": agg.get("mean_power_w", ""),
                    "tok_s_per_w": agg.get("tok_s_per_w", ""),
                    "max_junction_c": agg.get("max_junction_c", ""),
                    "errors": len(bench_data.get("errors", [])),
                    "d3cold_s": bench_data.get("d3cold_s", ""),
                    "verdict": "PASS",
                }
            else:
                row = {f: "" for f in MATRIX_FIELDS}
                row.update({"offset_mv": off, "cap_w": cap,
                            "errors": -1, "verdict": "NO_DATA"})
            append_row(out_dir, row)
            print(f"  ✓ PASS — row appended to {out_dir / MATRIX_CSV}")

            # Wait for D3cold between steps (max 60 s)
            if step_idx < len(plan):
                print("  waiting for D3cold …", end=" ", flush=True)
                if wait_d3cold(timeout=60):
                    print("OK")
                else:
                    print("TIMEOUT (continuing)")

    except KeyboardInterrupt:
        print(f"\n\n  Ctrl-C. Restoring safe point …", file=sys.stderr)
        restore_safe_point(safe_offset, safe_cap)
        sys.exit(130)
    except Exception as e:
        print(f"\n\n  EXCEPTION: {e}", file=sys.stderr)
        print(f"  Restoring safe point …", file=sys.stderr)
        restore_safe_point(safe_offset, safe_cap)
        sys.exit(1)
    finally:
        # If we reached here without an explicit sys.exit, the safe point
        # is already at the last successful step — nothing to do.
        pass

    print(f"\n{'='*60}")
    print(f"  COMPLETE: {len(plan)} step(s) measured.")
    print(f"  Matrix:   {out_dir / MATRIX_CSV}")
    print(f"  Run --report to see the sorted table.")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()

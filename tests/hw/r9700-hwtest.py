#!/usr/bin/env python3
"""r9700-hwtest.py — hardware-in-the-loop validation for r9700-tunerd.

Run as root by the human operator.  Stdlib only.  Never run by a service.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

CONF = Path("/etc/r9700-tunerd.conf")
PCI_ROOT = Path("/sys/bus/pci/devices")
# Not /tmp: with fs.protected_regular=1 root cannot append to a file another
# user created in the sticky /tmp, which is exactly what an earlier dry run does.
LOG = Path("/var/log/r9700-hwtest.log")
SERVICE = "r9700-tunerd.service"

# MUST stay identical to r9700-tunerd KERNEL_FATAL / KERNEL_OD_NOISE.
# A unit test asserts equivalence between these and the daemon's constants.
KERNEL_FATAL_RE = re.compile(
    r"ring timeout|GPU reset|SMU timeout|PCIe AER|AER:|device.*removed|amdgpu:.*fatal",
    re.I,
)
KERNEL_OD_NOISE_RE = re.compile(
    r"Failed to upload overdrive table|OD_UNSUPPORTED_FEATURE",
    re.I,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    print(msg, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


def read_conf() -> dict[str, str]:
    d: dict[str, str] = {}
    for line in CONF.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        d[k.strip()] = v.strip()
    return d


def discover(cfg: dict[str, str]) -> Path:
    """Match by vendor/device/subsystem identity — never bus address or card#."""
    want = (
        cfg["VENDOR"].lower(), cfg["DEVICE"].lower(),
        cfg["SUBSYSTEM_VENDOR"].lower(), cfg["SUBSYSTEM_DEVICE"].lower(),
    )
    hits = []
    for d in sorted(PCI_ROOT.iterdir()):
        try:
            ids = tuple(
                (d / f).read_text().strip().lower()
                for f in ("vendor", "device", "subsystem_vendor", "subsystem_device")
            )
        except OSError:
            continue
        if ids == want:
            hits.append(d)
    if len(hits) != 1:
        raise SystemExit(f"discovery: expected 1, got {len(hits)}: {hits}")
    return hits[0]


def render_node(pci: Path) -> Path:
    for c in sorted((pci / "drm").iterdir()):
        if c.name.startswith("renderD"):
            return Path("/dev/dri") / c.name
    raise SystemExit(f"no renderD* under {pci}/drm")


def card_node(pci: Path) -> Path | None:
    for c in sorted((pci / "drm").iterdir()):
        if re.fullmatch(r"card\d+", c.name):
            return Path("/dev/dri") / c.name
    return None


def rt_status(pci: Path) -> str:
    """Safe while suspended — polling this file does NOT wake the device."""
    return (pci / "power" / "runtime_status").read_text().strip().lower()


def pwr_state(pci: Path) -> str:
    """Safe while suspended."""
    return (pci / "power_state").read_text().strip()


def read_autosuspend_delay_ms(pci: Path) -> int:
    """Read power/autosuspend_delay_ms; fallback 5000 ms."""
    try:
        return int((pci / "power" / "autosuspend_delay_ms").read_text().strip())
    except (OSError, ValueError):
        return 5000


def read_vo(pci: Path) -> int | None:
    """GUARD: pp_od_clk_voltage returns EBUSY while suspended."""
    if rt_status(pci) != "active":
        return None
    text = (pci / "pp_od_clk_voltage").read_text(errors="replace")
    in_off = False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("OD_VDDGFX_OFFSET"):
            in_off = True; continue
        if s.startswith("OD_"):
            in_off = False; continue
        if in_off:
            m = re.search(r"(-?\d+)\s*mV", s, re.I)
            if m:
                return int(m.group(1))
    return None


def read_cap_w(pci: Path) -> int | None:
    """GUARD: hwmon sysfs also returns EBUSY while suspended."""
    if rt_status(pci) != "active":
        return None
    try:
        h = next((pci / "hwmon").iterdir())
        return int((h / "power1_cap").read_text().strip()) // 1_000_000
    except (StopIteration, ValueError, OSError):
        return None


def wait_d3cold(pci: Path, timeout: float) -> tuple[str, str, float, float]:
    """Poll until suspended+D3cold. Returns (status, power_state, t_suspended, t_d3cold)."""
    t0 = time.monotonic()
    t_susp: float | None = None
    while time.monotonic() - t0 < timeout:
        st = rt_status(pci)
        if st == "suspended" and t_susp is None:
            t_susp = time.monotonic() - t0
        ps = pwr_state(pci)
        if st == "suspended" and ps == "D3cold":
            return st, ps, t_susp or 0.0, time.monotonic() - t0
        time.sleep(0.25)
    st, ps = rt_status(pci), pwr_state(pci)
    return st, ps, t_susp or 0.0, time.monotonic() - t0


def wait_active(pci: Path, timeout: float) -> bool:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if rt_status(pci) == "active":
            return True
        time.sleep(0.1)
    return False


def settle(pci: Path, timeout: float = 30.0) -> bool:
    """Wait until runtime_status==suspended AND power_state==D3cold.

    Polls every 0.5 s, reading only those two sysfs files.
    Returns True if settled within timeout, False otherwise.
    """
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        st = rt_status(pci)
        ps = pwr_state(pci)
        if st == "suspended" and ps == "D3cold":
            log(f"  settled in {time.monotonic() - t0:.1f} s")
            return True
        time.sleep(0.5)
    st = rt_status(pci)
    ps = pwr_state(pci)
    log(f"  NOT settled after {time.monotonic() - t0:.1f} s ({st}/{ps})")
    return False


def open_render(pci: Path) -> int:
    """O_RDWR|O_CLOEXEC so the fd is not leaked into child processes."""
    return os.open(str(render_node(pci)), os.O_RDWR | os.O_CLOEXEC)


def fuser_nodes(pci: Path) -> str:
    paths = [str(p) for p in (render_node(pci), card_node(pci)) if p and p.exists()]
    if not paths:
        return ""
    try:
        r = subprocess.run(["fuser", "-v"] + paths,
                           capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        return "TIMEOUT"
    return (r.stdout + r.stderr).strip()


def journal_tunerd(since: str) -> str:
    try:
        r = subprocess.run(
            ["journalctl", "-t", "r9700-tunerd", "--since", since, "--no-pager"],
            capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return "JOURNALCTL_TIMEOUT"
    return r.stdout


def journal_kernel(since: str) -> str:
    try:
        r = subprocess.run(
            ["journalctl", "-k", "--since", since, "--no-pager"],
            capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return "JOURNALCTL_TIMEOUT"
    return r.stdout


def tunerd_pid() -> int | None:
    try:
        r = subprocess.run(
            ["pgrep", "-f", "/usr/local/sbin/r9700-tunerd watch"],
            capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        return None
    pids = r.stdout.split()
    return int(pids[0]) if pids else None


def ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def scan_kernel(journal_text: str) -> list[str]:
    """Return kernel lines matching KERNEL_FATAL_RE, excluding KERNEL_OD_NOISE_RE."""
    bad = []
    for line in journal_text.splitlines():
        if KERNEL_OD_NOISE_RE.search(line):
            continue
        if KERNEL_FATAL_RE.search(line):
            bad.append(line.strip())
    return bad


def count_wakes(journal_text: str) -> int:
    """Count wake events from the watcher journal.

    handle_wake always logs 'R9700 runtime active' (one per wake).
    The counter-detected variant adds an extra line containing
    'resume detected via' before handle_wake; we exclude that to
    avoid double-counting.
    """
    n = 0
    for line in journal_text.splitlines():
        if "R9700 runtime active" in line and "resume detected via" not in line:
            n += 1
    return n


# ── subcommands ───────────────────────────────────────────────────────────────

def cmd_idle(args: argparse.Namespace) -> int:
    cfg = read_conf()
    pci = discover(cfg)
    log(f"idle: pci={pci.name} timeout={args.timeout}s")
    st, ps, t_susp, t_d3 = wait_d3cold(pci, args.timeout)
    ok = st == "suspended" and ps == "D3cold"
    log(f"  runtime_status={st} power_state={ps} "
        f"t_susp={t_susp:.1f}s t_d3cold={t_d3:.1f}s")
    fu = fuser_nodes(pci)
    log(f"  fuser:\n{fu or '    (none)'}")
    tuner_holds = "r9700-tunerd" in fu
    log(f"  tuner_holds_node={tuner_holds}")
    verdict = ok and not tuner_holds
    log(f"  {'PASS' if verdict else 'FAIL'}")
    return 0 if verdict else 1


def cmd_cycles(args: argparse.Namespace) -> int:
    cfg = read_conf()
    pci = discover(cfg)
    vo_t = int(cfg["VOLTAGE_OFFSET_MV"])
    cap_t = int(cfg["POWER_LIMIT_W"])
    n = args.count
    gap = args.gap
    log(f"cycles: count={n} vo={vo_t} cap={cap_t} gap={gap}s")
    settled = True
    if not args.no_settle:
        settled = settle(pci)
        if not settled:
            log("  WARNING: GPU did not settle; continuing (first cycle may not be a D3cold wake)")
    t0 = ts()
    rows: list[tuple] = []
    all_ok = True
    for i in range(1, n + 1):
        log(f"  cycle {i}/{n}")
        fd = open_render(pci)
        t_wake = time.monotonic()
        vo_at: float | None = None
        vo = cap = None
        try:
            deadline = t_wake + 5.0
            while time.monotonic() < deadline:
                if rt_status(pci) == "active":
                    vo = read_vo(pci)
                    cap = read_cap_w(pci)
                    if vo == vo_t:
                        vo_at = time.monotonic() - t_wake
                        break
                time.sleep(0.1)
            stayed = True
            hold_end = time.monotonic() + 3.0
            while time.monotonic() < hold_end:
                v = read_vo(pci)
                if v != vo_t:
                    stayed = False
                time.sleep(0.4)
        finally:
            os.close(fd)
        st, ps, t_susp, t_d3 = wait_d3cold(pci, 30)
        d3ok = st == "suspended" and ps == "D3cold"
        lat_ok = vo_at is not None and vo_at <= 3.5
        cap_ok = cap == cap_t
        row_ok = lat_ok and stayed and cap_ok and d3ok
        if not row_ok:
            all_ok = False
        rows.append((i, vo_at, vo, cap, stayed, d3ok, t_susp, t_d3))
        log(f"    lat={vo_at} vo={vo} cap={cap} stayed={stayed} "
            f"d3={d3ok} t_susp={t_susp:.1f}s t_d3cold={t_d3:.1f}s")
        if gap > 0 and i < n:
            time.sleep(gap)
    # Journal: count wake events (handles both state-transition and
    # counter-detected wakes); zero cap-applied lines.
    j = journal_tunerd(t0)
    wakes = count_wakes(j)
    cap_applied = len(re.findall(r"power cap applied", j))
    log(f"  journal: wakes={wakes} cap_applied={cap_applied}")
    j_ok = wakes == n and cap_applied == 0
    if not j_ok:
        if not settled and wakes == n - 1 and cap_applied == 0:
            j_ok = True
            log(f"  NOTE: settled=False, first-cycle wake not logged (wakes={wakes}/{n}); treating as PASS_WITH_NOTE")
        else:
            all_ok = False
    log(f"  settled={settled}")
    log("  Cycle | vo_lat_s | vo | cap_W | stayed | D3cold | t_susp | t_d3cold")
    for r in rows:
        log(f"  {r[0]} | {r[1]} | {r[2]} | {r[3]} | {r[4]} | {r[5]} | {r[6]:.1f} | {r[7]:.1f}")
    verdict = all_ok and j_ok
    if verdict and not settled and wakes == n - 1:
        log(f"  PASS_WITH_NOTE")
    else:
        log(f"  {'PASS' if verdict else 'FAIL'}")
    return 0 if verdict else 1


def cmd_storm(args: argparse.Namespace) -> int:
    cfg = read_conf()
    pci = discover(cfg)
    n, hold_ms = args.count, args.hold_ms
    delay_ms = read_autosuspend_delay_ms(pci)
    cap_s = delay_ms / 1000.0 + 5.0
    log(f"storm: count={n} hold_ms={hold_ms} autosuspend_delay={delay_ms}ms cap={cap_s:.1f}s")
    pid0 = tunerd_pid()
    if pid0 is None:
        log("  FAIL: watcher not running"); return 1
    if not args.no_settle:
        if not settle(pci):
            log("  WARNING: GPU did not settle; continuing")
    t0 = ts()
    for i in range(1, n + 1):
        fd = open_render(pci)
        try:
            time.sleep(hold_ms / 1000.0)
        finally:
            os.close(fd)
        if i < n:
            # Wait for suspended (poll 0.25 s, cap from sysfs).
            t_start = time.monotonic()
            t_susp: float | None = None
            d3cold_reached = False
            while time.monotonic() - t_start < cap_s:
                st = rt_status(pci)
                if st == "suspended":
                    t_susp = time.monotonic() - t_start
                    d3cold_reached = (pwr_state(pci) == "D3cold")
                    break
                time.sleep(0.25)
            if t_susp is None:
                log(f"    iter {i}: TIMEOUT waiting for suspended ({cap_s:.1f}s)")
            else:
                log(f"    iter {i}: t_susp={t_susp:.2f}s d3cold={d3cold_reached}")
            # Immediately reopen (next iteration) — the hard case for the watcher.
    st, ps, t_susp, t_d3 = wait_d3cold(pci, 60)
    d3ok = st == "suspended" and ps == "D3cold"
    pid1 = tunerd_pid()
    pid_ok = pid0 == pid1
    j = journal_tunerd(t0)
    tb = "Traceback" in j
    kj = journal_kernel(t0)
    bad = scan_kernel(kj)
    k_ok = not bad
    wakes = count_wakes(j)
    wake_ok = (wakes == n)
    ok = d3ok and pid_ok and not tb and k_ok and wake_ok
    log(f"  d3cold={d3ok}({t_d3:.1f}s) pid_stable={pid_ok} tb={tb} "
        f"kerr={len(bad)} wakes={wakes}/{n}")
    for bl in bad[:5]:
        log(f"    K: {bl}")
    log(f"  {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


def cmd_config_typo(args: argparse.Namespace) -> int:
    cfg = read_conf()
    pci = discover(cfg)
    poll = float(cfg.get("POLL_INTERVAL_S", "2"))
    wait_s = poll * 3 + 1
    bak = Path("/tmp/r9700-tunerd.conf.bak")
    log(f"config-typo: poll={poll}s wait={wait_s:.0f}s")
    pid0 = tunerd_pid()
    if pid0 is None:
        log("  FAIL: watcher not running"); return 1
    if not args.no_settle:
        if not settle(pci):
            log("  WARNING: GPU did not settle; continuing")
    t0 = ts()
    ok = True
    try:
        shutil.copy2(CONF, bak)
        lines = [l for l in CONF.read_text().splitlines()
                 if not l.strip().startswith("VOLTAGE_OFFSET_MV")]
        lines.append("VOLTAGE_OFFSET_MV=abc")
        CONF.write_text("\n".join(lines) + "\n")
        log(f"  wrote VOLTAGE_OFFSET_MV=abc, waiting {wait_s:.0f}s")
        time.sleep(wait_s)
        pid_mid = tunerd_pid()
        j1 = journal_tunerd(t0)
        warn = bool(re.search(r"config|invalid|parse|error|warn", j1, re.I))
        log(f"  pid_stable={pid0 == pid_mid} warn={warn}")
        if pid0 != pid_mid or not warn:
            ok = False
        shutil.copy2(bak, CONF)
        log(f"  restored, waiting {wait_s:.0f}s")
        time.sleep(wait_s)
        j2 = journal_tunerd(t0)
        new_part = j2[len(j1):]
        no_new_err = not re.search(r"invalid|parse.*error|config.*error", new_part, re.I)
        log(f"  post-restore no_new_err={no_new_err}")
        if not no_new_err:
            ok = False
        r = subprocess.run([sys.executable, __file__, "cycles", "--count", "1"],
                           capture_output=True, text=True, timeout=400)
        log(r.stdout)
        if r.returncode != 0:
            ok = False
    except subprocess.TimeoutExpired:
        log("  FAIL: self-reinvocation timed out (400 s)")
        ok = False
    finally:
        if bak.exists():
            shutil.copy2(bak, CONF)
            bak.unlink(missing_ok=True)
    log(f"  {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


def cmd_sigterm(args: argparse.Namespace) -> int:
    cfg = read_conf()
    pci = discover(cfg)
    vo_t = int(cfg["VOLTAGE_OFFSET_MV"])
    log(f"sigterm: vo_target={vo_t}")
    pid_old = tunerd_pid()
    if pid_old is None:
        log("  FAIL: watcher not running"); return 1
    log(f"  old_pid={pid_old}")
    if not args.no_settle:
        if not settle(pci):
            log("  WARNING: GPU did not settle; continuing")
    fd = open_render(pci)
    try:
        if not wait_active(pci, 5):
            log("  FAIL: GPU did not become active"); return 1
        log("  GPU active → systemctl restart")
        try:
            subprocess.run(["systemctl", "restart", SERVICE],
                           check=True, capture_output=True, timeout=30)
        except subprocess.TimeoutExpired:
            log("  FAIL: systemctl restart timed out (30 s)")
            return 1
        t0 = time.monotonic()
        while time.monotonic() - t0 < 5:
            if not Path(f"/proc/{pid_old}").exists():
                break
            time.sleep(0.2)
        old_gone = not Path(f"/proc/{pid_old}").exists()
        t0 = time.monotonic()
        pid_new: int | None = None
        while time.monotonic() - t0 < 5:
            pid_new = tunerd_pid()
            if pid_new and pid_new != pid_old:
                break
            time.sleep(0.2)
        log(f"  old_gone={old_gone} new_pid={pid_new}")
        t0 = time.monotonic()
        restored = False
        while time.monotonic() - t0 < 5:
            if read_vo(pci) == vo_t:
                restored = True
                break
            time.sleep(0.2)
        log(f"  offset_restored={restored}")
    finally:
        os.close(fd)
    ok = old_gone and pid_new is not None and pid_new != pid_old and restored
    log(f"  {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


def cmd_reboot_check(args: argparse.Namespace) -> int:
    cfg = read_conf()
    pci = discover(cfg)
    log("reboot-check: post-reboot acceptance")
    res: list[tuple[str, bool, str]] = []
    try:
        en = subprocess.run(["systemctl", "is-enabled", SERVICE],
                            capture_output=True, text=True, timeout=30).stdout.strip()
        ac = subprocess.run(["systemctl", "is-active", SERVICE],
                            capture_output=True, text=True, timeout=30).stdout.strip()
    except subprocess.TimeoutExpired:
        log("  FAIL: systemctl timed out"); return 1
    res.append(("service enabled+active", en == "enabled" and ac == "active", f"{en}/{ac}"))
    try:
        ld = subprocess.run(["systemctl", "is-active", "lactd"],
                            capture_output=True, text=True, timeout=30).stdout.strip()
    except subprocess.TimeoutExpired:
        ld = "TIMEOUT"
    res.append(("lactd inactive", ld in ("inactive", "failed", "unknown"), ld))
    fu = fuser_nodes(pci)
    res.append(("no tuner DRM handles", "r9700-tunerd" not in fu, fu[:80] or "(none)"))
    try:
        r1 = subprocess.run([sys.executable, __file__, "idle", "--timeout", "90"],
                            capture_output=True, text=True, timeout=400)
    except subprocess.TimeoutExpired:
        r1 = subprocess.CompletedProcess([], 1, stdout="TIMEOUT", stderr="")
    res.append(("boot→D3cold", r1.returncode == 0,
                r1.stdout.strip().splitlines()[-1] if r1.stdout else ""))
    try:
        r2 = subprocess.run([sys.executable, __file__, "cycles", "--count", "1"],
                            capture_output=True, text=True, timeout=400)
    except subprocess.TimeoutExpired:
        r2 = subprocess.CompletedProcess([], 1, stdout="TIMEOUT", stderr="")
    res.append(("wake→offset→cap→D3cold", r2.returncode == 0,
                r2.stdout.strip().splitlines()[-1] if r2.stdout else ""))
    try:
        r3 = subprocess.run([sys.executable, __file__, "idle", "--timeout", "90"],
                            capture_output=True, text=True, timeout=400)
    except subprocess.TimeoutExpired:
        r3 = subprocess.CompletedProcess([], 1, stdout="TIMEOUT", stderr="")
    res.append(("stop→D3cold", r3.returncode == 0,
                r3.stdout.strip().splitlines()[-1] if r3.stdout else ""))
    try:
        r = subprocess.run(["journalctl", "-k", "-b", "--no-pager"],
                           capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        r = subprocess.CompletedProcess([], 0, stdout="", stderr="TIMEOUT")
    bad = scan_kernel(r.stdout)
    res.append(("no amdgpu failures (beyond OD)", not bad,
                (bad[0][:70] if bad else "none")))
    log("  Criterion | OK | Detail")
    all_ok = True
    for name, ok, detail in res:
        log(f"  {name} | {'Y' if ok else 'N'} | {detail}")
        if not ok:
            all_ok = False
    log(f"  {'PASS' if all_ok else 'FAIL'}")
    return 0 if all_ok else 1


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description="r9700-tunerd hardware-in-the-loop validation (run as root)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("idle", help="wait for D3cold, check fuser holders")
    p.add_argument("--timeout", type=float, default=90, help="max seconds to wait")
    p = sub.add_parser("cycles", help="N wake→restore→hold→D3cold cycles")
    p.add_argument("--count", type=int, default=5)
    p.add_argument("--gap", type=float, default=0,
                   help="extra seconds in D3cold between cycles (relaxed pattern)")
    p.add_argument("--no-settle", action="store_true",
                   help="skip settle (wait for D3cold) before first wake")
    p = sub.add_parser("storm", help="short-wake storm (open/close render node)")
    p.add_argument("--count", type=int, default=10)
    p.add_argument("--hold-ms", type=int, default=300)
    p.add_argument("--no-settle", action="store_true",
                   help="skip settle (wait for D3cold) before first wake")
    p = sub.add_parser("config-typo", help="inject bad config, verify graceful handling")
    p.add_argument("--no-settle", action="store_true",
                   help="skip settle (wait for D3cold) before first wake")
    p = sub.add_parser("sigterm", help="restart service while GPU active")
    p.add_argument("--no-settle", action="store_true",
                   help="skip settle (wait for D3cold) before first wake")
    sub.add_parser("reboot-check", help="full post-reboot acceptance run")
    args = ap.parse_args()
    if os.geteuid() != 0:
        print("ERROR: must run as root", file=sys.stderr)
        return 1
    return {
        "idle": cmd_idle,
        "cycles": cmd_cycles,
        "storm": cmd_storm,
        "config-typo": cmd_config_typo,
        "sigterm": cmd_sigterm,
        "reboot-check": cmd_reboot_check,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())

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
LOG = Path("/tmp/r9700-hwtest.log")
SERVICE = "r9700-tunerd.service"

# Known-benign kernel messages on every RDNA4 resume (OD table re-upload fails
# because the firmware rejects it; GPU keeps working).  We filter these out of
# the "real error" scan so the storm / reboot-check tests don't false-positive.
BENIGN_KERNEL = (
    "Failed to upload overdrive table",
    "OD_UNSUPPORTED_FEATURE",
    "Failed to upload customized OD settings",
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


def read_vo(pci: Path) -> int | None:
    """GUARD: pp_od_clk_voltage returns EBUSY while suspended, so we must
    confirm runtime_status==active before touching it."""
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


def wait_d3cold(pci: Path, timeout: float) -> tuple[str, str, float]:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        st, ps = rt_status(pci), pwr_state(pci)
        if st == "suspended" and ps == "D3cold":
            return st, ps, time.monotonic() - t0
        time.sleep(0.5)
    return rt_status(pci), pwr_state(pci), time.monotonic() - t0


def wait_active(pci: Path, timeout: float) -> bool:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if rt_status(pci) == "active":
            return True
        time.sleep(0.1)
    return False


def open_render(pci: Path) -> int:
    """O_RDWR|O_CLOEXEC so the fd is not leaked into child processes."""
    return os.open(str(render_node(pci)), os.O_RDWR | os.O_CLOEXEC)


def fuser_nodes(pci: Path) -> str:
    paths = [str(p) for p in (render_node(pci), card_node(pci)) if p and p.exists()]
    if not paths:
        return ""
    r = subprocess.run(["fuser", "-v"] + paths, capture_output=True, text=True)
    return (r.stdout + r.stderr).strip()


def journal_tunerd(since: str) -> str:
    r = subprocess.run(
        ["journalctl", "-t", "r9700-tunerd", "--since", since, "--no-pager"],
        capture_output=True, text=True)
    return r.stdout


def journal_kernel(since: str) -> str:
    r = subprocess.run(
        ["journalctl", "-k", "--since", since, "--no-pager"],
        capture_output=True, text=True)
    return r.stdout


def tunerd_pid() -> int | None:
    r = subprocess.run(
        ["pgrep", "-f", "/usr/local/sbin/r9700-tunerd watch"],
        capture_output=True, text=True)
    pids = r.stdout.split()
    return int(pids[0]) if pids else None


def ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ── subcommands ───────────────────────────────────────────────────────────────

def cmd_idle(args: argparse.Namespace) -> int:
    cfg = read_conf()
    pci = discover(cfg)
    log(f"idle: pci={pci.name} timeout={args.timeout}s")
    st, ps, elapsed = wait_d3cold(pci, args.timeout)
    ok = st == "suspended" and ps == "D3cold"
    log(f"  runtime_status={st} power_state={ps} elapsed={elapsed:.1f}s")
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
    log(f"cycles: count={n} vo={vo_t} cap={cap_t}")
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
            # Poll until watcher restores the offset (max 5 s hard deadline).
            deadline = t_wake + 5.0
            while time.monotonic() < deadline:
                if rt_status(pci) == "active":
                    vo = read_vo(pci)
                    cap = read_cap_w(pci)
                    if vo == vo_t:
                        vo_at = time.monotonic() - t_wake
                        break
                time.sleep(0.1)
            # Hold 3 s: offset must stay at target.
            stayed = True
            samples: list[int | None] = []
            hold_end = time.monotonic() + 3.0
            while time.monotonic() < hold_end:
                v = read_vo(pci)
                samples.append(v)
                if v != vo_t:
                    stayed = False
                time.sleep(0.4)
        finally:
            os.close(fd)  # GUARD: always release the render node
        st, ps, d3t = wait_d3cold(pci, 30)
        d3ok = st == "suspended" and ps == "D3cold"
        lat_ok = vo_at is not None and vo_at <= 3.5
        cap_ok = cap == cap_t
        row_ok = lat_ok and stayed and cap_ok and d3ok
        if not row_ok:
            all_ok = False
        rows.append((i, vo_at, vo, cap, stayed, d3ok))
        log(f"    lat={vo_at} vo={vo} cap={cap} stayed={stayed} d3={d3ok} ({d3t:.1f}s)")
    # Journal: exactly n restore lines, zero cap-applied lines.
    j = journal_tunerd(t0)
    restored = len(re.findall(rf"VDDGFX offset restored: {vo_t} mV", j))
    cap_applied = len(re.findall(r"power cap applied", j))
    log(f"  journal: restored={restored} cap_applied={cap_applied}")
    j_ok = restored == n and cap_applied == 0
    if not j_ok:
        all_ok = False
    log("  Cycle | vo_lat_s | vo | cap_W | stayed | D3cold")
    for r in rows:
        log(f"  {r[0]} | {r[1]} | {r[2]} | {r[3]} | {r[4]} | {r[5]}")
    verdict = all_ok and j_ok
    log(f"  {'PASS' if verdict else 'FAIL'}")
    return 0 if verdict else 1


def cmd_storm(args: argparse.Namespace) -> int:
    cfg = read_conf()
    pci = discover(cfg)
    n, hold_ms = args.count, args.hold_ms
    log(f"storm: count={n} hold_ms={hold_ms}")
    pid0 = tunerd_pid()
    if pid0 is None:
        log("  FAIL: watcher not running"); return 1
    t0 = ts()
    for i in range(1, n + 1):
        fd = open_render(pci)
        try:
            time.sleep(hold_ms / 1000.0)
        finally:
            os.close(fd)
        if i < n:
            time.sleep(2.0)
    st, ps, d3t = wait_d3cold(pci, 60)
    d3ok = st == "suspended" and ps == "D3cold"
    pid1 = tunerd_pid()
    pid_ok = pid0 == pid1
    j = journal_tunerd(t0)
    tb = "Traceback" in j
    # Kernel: flag ring-timeout / GPU-reset / AER; ignore known OD noise.
    kj = journal_kernel(t0)
    bad = [l.strip() for l in kj.splitlines()
           if not any(b in l for b in BENIGN_KERNEL)
           and re.search(r"ring.*timeout|GPU.*reset|AER:", l, re.I)]
    k_ok = not bad
    ok = d3ok and pid_ok and not tb and k_ok
    log(f"  d3cold={d3ok}({d3t:.1f}s) pid_stable={pid_ok} tb={tb} kerr={len(bad)}")
    for bl in bad[:5]:
        log(f"    K: {bl}")
    log(f"  {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


def cmd_config_typo(args: argparse.Namespace) -> int:
    cfg = read_conf()
    pci = discover(cfg)
    poll = float(cfg.get("POLL_INTERVAL_S", "2"))
    wait_s = poll * 3 + 1  # small buffer past 3 intervals
    bak = Path("/tmp/r9700-tunerd.conf.bak")
    log(f"config-typo: poll={poll}s wait={wait_s:.0f}s")
    pid0 = tunerd_pid()
    if pid0 is None:
        log("  FAIL: watcher not running"); return 1
    t0 = ts()
    ok = True
    try:
        shutil.copy2(CONF, bak)
        # Inject bad value
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
        # Restore
        shutil.copy2(bak, CONF)
        log(f"  restored, waiting {wait_s:.0f}s")
        time.sleep(wait_s)
        j2 = journal_tunerd(t0)
        new_part = j2[len(j1):]
        no_new_err = not re.search(r"invalid|parse.*error|config.*error", new_part, re.I)
        log(f"  post-restore no_new_err={no_new_err}")
        if not no_new_err:
            ok = False
        # Prove the watcher still works after config recovery
        r = subprocess.run([sys.executable, __file__, "cycles", "--count", "1"],
                           capture_output=True, text=True)
        log(r.stdout)
        if r.returncode != 0:
            ok = False
    finally:
        # ALWAYS restore — a crash mid-test must not leave a broken config.
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
    fd = open_render(pci)
    try:
        if not wait_active(pci, 5):
            log("  FAIL: GPU did not become active"); return 1
        log("  GPU active → systemctl restart")
        subprocess.run(["systemctl", "restart", SERVICE],
                       check=True, capture_output=True)
        # Old PID must exit within 5 s
        t0 = time.monotonic()
        while time.monotonic() - t0 < 5:
            if not Path(f"/proc/{pid_old}").exists():
                break
            time.sleep(0.2)
        old_gone = not Path(f"/proc/{pid_old}").exists()
        # New PID must appear within 5 s
        t0 = time.monotonic()
        pid_new: int | None = None
        while time.monotonic() - t0 < 5:
            pid_new = tunerd_pid()
            if pid_new and pid_new != pid_old:
                break
            time.sleep(0.2)
        log(f"  old_gone={old_gone} new_pid={pid_new}")
        # New watcher must restore offset within 5 s (treats active GPU as wake)
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
    en = subprocess.run(["systemctl", "is-enabled", SERVICE],
                        capture_output=True, text=True).stdout.strip()
    ac = subprocess.run(["systemctl", "is-active", SERVICE],
                        capture_output=True, text=True).stdout.strip()
    res.append(("service enabled+active", en == "enabled" and ac == "active", f"{en}/{ac}"))
    ld = subprocess.run(["systemctl", "is-active", "lactd"],
                        capture_output=True, text=True).stdout.strip()
    res.append(("lactd inactive", ld in ("inactive", "failed", "unknown"), ld))
    fu = fuser_nodes(pci)
    res.append(("no tuner DRM handles", "r9700-tunerd" not in fu, fu[:80] or "(none)"))
    r1 = subprocess.run([sys.executable, __file__, "idle", "--timeout", "90"],
                        capture_output=True, text=True)
    res.append(("boot→D3cold", r1.returncode == 0,
                r1.stdout.strip().splitlines()[-1] if r1.stdout else ""))
    r2 = subprocess.run([sys.executable, __file__, "cycles", "--count", "1"],
                        capture_output=True, text=True)
    res.append(("wake→offset→cap→D3cold", r2.returncode == 0,
                r2.stdout.strip().splitlines()[-1] if r2.stdout else ""))
    r3 = subprocess.run([sys.executable, __file__, "idle", "--timeout", "90"],
                        capture_output=True, text=True)
    res.append(("stop→D3cold", r3.returncode == 0,
                r3.stdout.strip().splitlines()[-1] if r3.stdout else ""))
    # Scan this boot's kernel log for real GPU failures only: informational
    # amdgpu lines are normal at boot, so match the failure signatures, not the
    # driver name, and ignore the known-benign OD re-upload messages.
    r = subprocess.run(["journalctl", "-k", "-b", "--no-pager"],
                       capture_output=True, text=True)
    bad = [l.strip() for l in r.stdout.splitlines()
           if not any(b in l for b in BENIGN_KERNEL)
           and re.search(r"ring.*timeout|GPU.*reset|AER:|amdgpu.*(fatal|hang|failed to (init|load|resume))",
                         l, re.I)]
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
    # Append-only: subcommands such as config-typo and reboot-check spawn other
    # subcommands of this same script, and truncating here would wipe the
    # parent's evidence. Rotate the file by hand if it grows.
    ap = argparse.ArgumentParser(
        description="r9700-tunerd hardware-in-the-loop validation (run as root)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("idle", help="wait for D3cold, check fuser holders")
    p.add_argument("--timeout", type=float, default=90, help="max seconds to wait")
    p = sub.add_parser("cycles", help="N wake→restore→hold→D3cold cycles")
    p.add_argument("--count", type=int, default=5)
    p = sub.add_parser("storm", help="short-wake storm (open/close render node)")
    p.add_argument("--count", type=int, default=10)
    p.add_argument("--hold-ms", type=int, default=300)
    sub.add_parser("config-typo", help="inject bad config, verify graceful handling")
    sub.add_parser("sigterm", help="restart service while GPU active")
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

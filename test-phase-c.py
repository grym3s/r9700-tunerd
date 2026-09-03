#!/usr/bin/python3
"""Phase C: -25 mV VDDGFX path + D3cold survival. Watcher must stay disabled."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

WANT = ("0x1002", "0x7551", "0x1043", "0x0626")
TUNERD = "/usr/local/sbin/r9700-tunerd"
CONF = Path("/etc/r9700-tunerd.conf")
PCI_ROOT = Path("/sys/bus/pci/devices")
LOG = Path("/tmp/r9700-phase-c.log")
KERNEL_BEFORE = Path("/tmp/r9700-phase-c-kbefore.txt")
RESULTS: dict[str, str] = {}


def out(msg: str) -> None:
    line = msg if msg.endswith("\n") else msg + "\n"
    sys.stdout.write(line)
    sys.stdout.flush()
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line)


def read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace").strip()


def discover() -> Path:
    hits = []
    for d in sorted(PCI_ROOT.iterdir()):
        try:
            ids = (
                read_text(d / "vendor").lower(),
                read_text(d / "device").lower(),
                read_text(d / "subsystem_vendor").lower(),
                read_text(d / "subsystem_device").lower(),
            )
        except OSError:
            continue
        if ids == WANT:
            hits.append(d)
    if len(hits) != 1:
        raise SystemExit(f"R9700 discovery failed: {[h.name for h in hits]}")
    return hits[0]


def runtime_status(pci: Path) -> str:
    try:
        return read_text(pci / "power" / "runtime_status").lower()
    except OSError as e:
        return f"err:{e}"


def power_state(pci: Path) -> str:
    try:
        return read_text(pci / "power_state")
    except OSError:
        return "unknown"


def render_node(pci: Path) -> Path | None:
    drm = pci / "drm"
    if not drm.is_dir():
        return None
    for c in sorted(drm.iterdir()):
        if c.name.startswith("renderD"):
            return Path("/dev/dri") / c.name
    return None


def hwmon_dir(pci: Path) -> Path | None:
    h = pci / "hwmon"
    if not h.is_dir():
        return None
    for c in sorted(h.iterdir()):
        if c.name.startswith("hwmon"):
            return c
    return None


def parse_od(text: str) -> dict:
    cur = mn = mx = None
    sclk = mclk = []
    in_vo = in_range = False
    section = ""
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("OD_VDDGFX_OFFSET"):
            in_vo, in_range, section = True, False, "vo"
            continue
        if s.startswith("OD_RANGE"):
            in_vo, in_range, section = False, True, "range"
            continue
        if s.startswith("OD_SCLK") or s.startswith("OD_MCLK") or s.startswith("OD_"):
            in_vo = False
            in_range = s.startswith("OD_RANGE")
            section = s.split(":")[0]
            continue
        if in_vo and s:
            m = re.search(r"(-?\d+)\s*mV", s, re.I)
            if m:
                cur = int(m.group(1))
        if in_range and "VDDGFX_OFFSET" in s.upper().replace(" ", ""):
            nums = [int(x) for x in re.findall(r"(-?\d+)\s*mV", s, flags=re.I)]
            if len(nums) >= 2:
                mn, mx = nums[0], nums[1]
        if "SCLK" in s.upper() and "MHz" in s:
            sclk.append(s)
        if "MCLK" in s.upper() and "MHz" in s:
            mclk.append(s)
    return {"vo": cur, "vo_min": mn, "vo_max": mx, "sclk": sclk, "mclk": mclk, "raw": text}


def guarded_active(pci: Path) -> None:
    st = runtime_status(pci)
    if st != "active":
        raise RuntimeError(f"refusing sysfs tune/sensor access: runtime_status={st}")


def snapshot(pci: Path, label: str) -> dict:
    st = runtime_status(pci)
    ps = power_state(pci)
    ctrl = read_text(pci / "power" / "control")
    data = {
        "label": label,
        "pci": pci.name,
        "runtime_status": st,
        "power_state": ps,
        "control": ctrl,
    }
    out(f"--- snapshot {label} pci={pci.name} status={st} power_state={ps} control={ctrl}")
    if st != "active":
        # Fan: try, but do not treat EBUSY as a wake attempt beyond the read.
        h = hwmon_dir(pci)
        if h is not None:
            fp = h / "fan1_input"
            try:
                data["fan_rpm"] = int(read_text(fp))
                out(f"fan1_input={data['fan_rpm']} RPM")
            except OSError as e:
                data["fan_rpm"] = f"unreadable:{e}"
                out(f"fan1_input unreadable ({e})")
        out("sensors skipped (not active)")
        return data
    guarded_active(pci)
    h = hwmon_dir(pci)
    if h is not None:
        for name in (
            "power1_cap",
            "power1_cap_min",
            "power1_cap_max",
            "power1_cap_default",
            "power1_average",
            "power1_input",
            "temp1_input",
            "temp2_input",
            "temp3_input",
            "fan1_input",
        ):
            p = h / name
            if p.exists():
                try:
                    data[name] = read_text(p)
                except OSError as e:
                    data[name] = f"err:{e}"
        cap = data.get("power1_cap")
        if cap and cap.isdigit():
            data["cap_w"] = int(cap) / 1_000_000
            out(f"power_cap={data['cap_w']:.0f} W "
                f"(min={int(data.get('power1_cap_min','0'))/1e6:.0f} "
                f"max={int(data.get('power1_cap_max','0'))/1e6:.0f})")
        for tkey, tlabel in (("temp1_input", "temp1"), ("temp2_input", "temp2"), ("temp3_input", "temp3")):
            v = data.get(tkey)
            if v and str(v).isdigit():
                out(f"{tlabel}={int(v)/1000:.1f} C")
        pav = data.get("power1_average") or data.get("power1_input")
        if pav and str(pav).isdigit():
            out(f"gpu_power={int(pav)/1e6:.1f} W")
        if "fan1_input" in data and str(data["fan1_input"]).isdigit():
            data["fan_rpm"] = int(data["fan1_input"])
            out(f"fan1_input={data['fan_rpm']} RPM")
    try:
        guarded_active(pci)
        od = parse_od(read_text(pci / "pp_od_clk_voltage"))
        data["vo"] = od["vo"]
        data["vo_range"] = (od["vo_min"], od["vo_max"])
        out(f"VDDGFX_OFFSET={od['vo']} mV range={od['vo_min']}..{od['vo_max']}")
        for s in od["sclk"][:8]:
            out(f"  {s}")
        for s in od["mclk"][:8]:
            out(f"  {s}")
    except Exception as e:
        data["vo"] = f"err:{e}"
        out(f"pp_od_clk_voltage error: {e}")
    for clk in ("pp_dpm_sclk", "pp_dpm_mclk", "pp_dpm_fclk", "pp_dpm_socclk"):
        p = pci / clk
        if p.exists():
            try:
                guarded_active(pci)
                txt = read_text(p)
                data[clk] = txt
                brief = " | ".join(x.strip() for x in txt.splitlines()[:6])
                out(f"{clk}: {brief}")
            except OSError as e:
                out(f"{clk} error: {e}")
    busy = pci / "gpu_busy_percent"
    if busy.exists():
        try:
            data["gpu_busy"] = read_text(busy)
            out(f"gpu_busy_percent={data['gpu_busy']}")
        except OSError:
            pass
    return data


def kernel_snapshot(path: Path) -> None:
    r = subprocess.run(
        ["journalctl", "-k", "--no-pager", "-n", "400"],
        capture_output=True,
        text=True,
    )
    path.write_text(r.stdout, encoding="utf-8")


def kernel_delta() -> str:
    r = subprocess.run(
        ["journalctl", "-k", "--no-pager", "-n", "400"],
        capture_output=True,
        text=True,
    )
    before = KERNEL_BEFORE.read_text(encoding="utf-8") if KERNEL_BEFORE.exists() else ""
    # crude: new lines
    bset = set(before.splitlines())
    new = [ln for ln in r.stdout.splitlines() if ln not in bset]
    pat = re.compile(
        r"amdgpu|ring timeout|GPU reset|AER|PCIe|SMU|BACO|overdrive|pp_od|VDDGFX|fatal|hung",
        re.I,
    )
    hits = [ln for ln in new if pat.search(ln)]
    out(f"kernel new matching lines: {len(hits)}")
    for ln in hits[:80]:
        out("  K " + ln)
    return "\n".join(hits)


def wait_d3cold(pci: Path, seconds: int = 90) -> tuple[str, str]:
    last = ("", "")
    t0 = time.time()
    saw_d3hot = False
    while time.time() - t0 < seconds:
        last = (runtime_status(pci), power_state(pci))
        if last[1] == "D3hot":
            saw_d3hot = True
        if last[0] == "suspended" and last[1] == "D3cold":
            out(f"reached D3cold after {time.time()-t0:.1f}s (saw_d3hot={saw_d3hot})")
            return last
        time.sleep(1)
    out(f"did not reach D3cold in {seconds}s; last={last[0]}/{last[1]} saw_d3hot={saw_d3hot}")
    return last


def which(cmd: str) -> str | None:
    from shutil import which as w
    return w(cmd)


def run_compute(pci: Path, render: Path, seconds: int = 8) -> str:
    """Harmless short exercise targeted at this PCI GPU, not the iGPU."""
    env = os.environ.copy()
    env["MESA_VK_DEVICE_SELECT"] = "1002:7551"
    env["DRI_PRIME"] = f"pci-{pci.name.replace(':', '_')}"
    env["ROCR_VISIBLE_DEVICES"] = "0"
    methods = []
    # vulkaninfo queries the selected ICD/device
    vk = which("vulkaninfo")
    if vk:
        r = subprocess.run(
            ["timeout", str(seconds), vk, "--summary"],
            capture_output=True,
            text=True,
            env=env,
        )
        methods.append(f"vulkaninfo rc={r.returncode}")
        snippet = (r.stdout + r.stderr)[-1500:]
        out("vulkaninfo snippet:\n" + snippet)
    roc = which("rocminfo")
    if roc:
        r = subprocess.run(
            ["timeout", str(min(seconds, 8)), roc],
            capture_output=True,
            text=True,
            env=env,
        )
        methods.append(f"rocminfo rc={r.returncode}")
        # keep short
        names = [ln for ln in r.stdout.splitlines() if re.search(r"Marketing Name|gfx|Name:", ln)]
        out("rocminfo names:\n" + "\n".join(names[:30]))
    cl = which("clinfo")
    if cl:
        r = subprocess.run(
            ["timeout", "8", cl, "-l"],
            capture_output=True,
            text=True,
            env=env,
        )
        methods.append(f"clinfo rc={r.returncode}")
        out("clinfo -l:\n" + (r.stdout + r.stderr)[:800])
    # pyopencl vector add on non-APU AMD device if present
    try:
        import pyopencl as cl  # type: ignore
        for plat in cl.get_platforms():
            for dev in plat.get_devices():
                name = dev.name.lower()
                if "gfx115" in name or "strix" in name or "8060" in name:
                    continue
                if "amd" in plat.vendor.lower() or "advanced micro" in plat.vendor.lower():
                    ctx = cl.Context([dev])
                    q = cl.CommandQueue(ctx)
                    src = """
                    __kernel void add(__global float* a, __global float* b, __global float* c) {
                        int i = get_global_id(0); c[i] = a[i] + b[i];
                    }
                    """
                    prg = cl.Program(ctx, src).build()
                    import numpy as np  # may fail
                    n = 1 << 20
                    a = np.random.rand(n).astype("float32")
                    b = np.random.rand(n).astype("float32")
                    c = np.empty_like(a)
                    mf = cl.mem_flags
                    a_buf = cl.Buffer(ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=a)
                    b_buf = cl.Buffer(ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=b)
                    c_buf = cl.Buffer(ctx, mf.WRITE_ONLY, c.nbytes)
                    t1 = time.time()
                    while time.time() - t1 < seconds:
                        prg.add(q, (n,), None, a_buf, b_buf, c_buf)
                        q.finish()
                    methods.append(f"pyopencl {dev.name}")
                    out(f"pyopencl ran on {dev.name}")
                    break
    except Exception as e:
        methods.append(f"pyopencl skip ({e.__class__.__name__})")
    if not any("pyopencl" in m and "skip" not in m for m in methods) and not vk and not roc:
        methods.append("render-hold-only (no compute tool found)")
        time.sleep(seconds)
    else:
        # keep the device active a bit after queries
        time.sleep(2)
    desc = ", ".join(methods) if methods else "none"
    out(f"compute methods: {desc}")
    return desc


class Wake:
    def __init__(self, pci: Path):
        self.pci = pci
        self.render = render_node(pci)
        self.fd = None

    def __enter__(self):
        if self.render is None or not self.render.exists():
            raise RuntimeError("no render node under discovered PCI device")
        out(f"wake via {self.render} (from PCI {self.pci.name}, not a hardcoded cardN)")
        self.fd = os.open(self.render, os.O_RDWR | os.O_CLOEXEC)
        t0 = time.time()
        while time.time() - t0 < 5:
            if runtime_status(self.pci) == "active":
                break
            time.sleep(0.1)
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        out("released render fd")


def tunerd(*args: str) -> subprocess.CompletedProcess:
    cmd = [TUNERD, *args]
    out("+ " + " ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True)
    out(r.stdout)
    if r.stderr.strip():
        out(r.stderr)
    if r.returncode != 0:
        out(f"tunerd exit {r.returncode}")
    return r


def drm_fds_of_pid(pid: int) -> list[str]:
    found = []
    fd = Path(f"/proc/{pid}/fd")
    if not fd.is_dir():
        return found
    for p in fd.iterdir():
        try:
            t = os.readlink(p)
        except OSError:
            continue
        if "/dev/dri/" in t:
            found.append(t)
    return found


def classify(vo, cap_w) -> str:
    vo_ok = vo == -25
    cap_ok = cap_w is not None and abs(float(cap_w) - 210) < 0.5
    if vo_ok and cap_ok:
        return "A (undervolt survives, power cap survives)"
    if vo_ok and not cap_ok:
        return "B (undervolt survives, power cap lost)"
    if (not vo_ok) and cap_ok:
        return "C (undervolt lost, power cap survives)"
    return "D (both lost)"


def main() -> int:
    LOG.write_text("", encoding="utf-8")
    out("=== Phase C start ===")
    enabled = subprocess.run(["systemctl", "is-enabled", "r9700-tunerd.service"], capture_output=True, text=True)
    active = subprocess.run(["systemctl", "is-active", "r9700-tunerd.service"], capture_output=True, text=True)
    out(f"watcher enabled={enabled.stdout.strip()} active={active.stdout.strip()}")
    if enabled.stdout.strip() == "enabled" or active.stdout.strip() == "active":
        out("REFUSING: watcher is not disabled")
        return 2

    pci = discover()
    RESULTS["Stable R9700 discovery"] = f"PASS {pci.name} 1002:7551/1043:0626"
    out(f"discovered {pci.name}")

    kernel_snapshot(KERNEL_BEFORE)

    # --- wake + baseline BEFORE any voltage change ---
    with Wake(pci) as w:
        snap0 = snapshot(pci, "baseline-before-vo")
        RESULTS["210 W apply/readback"] = (
            "PASS" if snap0.get("cap_w") is not None and abs(snap0["cap_w"] - 210) < 0.5
            else f"SEE baseline cap={snap0.get('cap_w')}"
        )
        # apply -25 via tunerd (also keeps POWER_LIMIT_W from conf)
        r = tunerd("set-undervolt", "-25")
        if r.returncode != 0:
            RESULTS["-25 mV apply/readback"] = "FAIL tunerd set-undervolt"
            out("STOP: set-undervolt failed")
            return 1
        snap1 = snapshot(pci, "after-set-undervolt")
        vo = snap1.get("vo")
        cap = snap1.get("cap_w")
        if vo != -25:
            RESULTS["-25 mV apply/readback"] = f"FAIL readback={vo}"
            out("STOP: VDDGFX_OFFSET readback is not -25 mV")
            return 1
        RESULTS["-25 mV apply/readback"] = "PASS readback=-25 mV"
        if cap is None or abs(cap - 210) >= 0.5:
            RESULTS["210 W apply/readback"] = f"FAIL after vo cap={cap}"
            out("STOP: power cap readback is not 210 W")
            return 1
        RESULTS["210 W apply/readback"] = "PASS readback=210 W"
        compute = run_compute(pci, w.render)
        snapc = snapshot(pci, "during-compute")
        vo2 = snapc.get("vo")
        cap2 = snapc.get("cap_w")
        busy = snapc.get("gpu_busy")
        RESULTS["Short compute stability"] = (
            f"ran ({compute}); vo={vo2} cap={cap2} busy={busy}"
        )
        self_drm = drm_fds_of_pid(os.getpid())
        out(f"test process drm fds during hold={self_drm}")

    # after apply, tuner process is gone; this test process must drop drm
    time.sleep(0.3)
    leftover = drm_fds_of_pid(os.getpid())
    out(f"test process drm fds after release={leftover}")
    pgrep = subprocess.run(["pgrep", "-a", "r9700-tunerd"], capture_output=True, text=True)
    out("tunerd procs: " + (pgrep.stdout.strip() or "none"))

    st, ps = wait_d3cold(pci, 90)
    snap_idle = snapshot(pci, "idle-after-first-apply")
    RESULTS["Returns to D3cold"] = (
        "PASS" if st == "suspended" and ps == "D3cold" else f"FAIL {st}/{ps}"
    )
    fan = snap_idle.get("fan_rpm")
    if isinstance(fan, int):
        RESULTS["0 RPM restored"] = "PASS" if fan == 0 else f"FAIL {fan} RPM"
    else:
        RESULTS["0 RPM restored"] = f"unreadable while {ps} ({fan})"

    survival_codes = []
    for i in range(1, 4):
        out(f"=== wake/sleep cycle {i} ===")
        with Wake(pci) as w:
            snap = snapshot(pci, f"cycle-{i}-immediate")
            code = classify(snap.get("vo"), snap.get("cap_w"))
            survival_codes.append((i, code, snap.get("vo"), snap.get("cap_w")))
            out(f"cycle {i} survival: {code} vo={snap.get('vo')} cap={snap.get('cap_w')}")
            if i == 1:
                RESULTS["Undervolt survives D3cold"] = "PASS" if snap.get("vo") == -25 else f"FAIL vo={snap.get('vo')}"
                capv = snap.get("cap_w")
                RESULTS["Power cap survives D3cold"] = (
                    "PASS" if capv is not None and abs(float(capv) - 210) < 0.5
                    else f"FAIL cap={capv}"
                )
                RESULTS["D3cold survival class"] = code
            if snap.get("vo") != -25 or snap.get("cap_w") is None or abs(float(snap.get("cap_w", -1)) - 210) >= 0.5:
                out("settings lost; manual apply while active")
                tunerd("apply")
                snap_r = snapshot(pci, f"cycle-{i}-after-restore")
                if snap_r.get("vo") != -25 or abs(float(snap_r.get("cap_w") or -1) - 210) >= 0.5:
                    out("STOP: apply did not restore -25 mV / 210 W")
                    RESULTS[f"cycle {i} restore"] = "FAIL"
                    kernel_delta()
                    print_table()
                    return 1
                out("restore confirmed")
            run_compute(pci, w.render, seconds=6)
            snapshot(pci, f"cycle-{i}-compute")
            key = {1: "Second wake stability", 2: "Third wake stability"}.get(i)
            # cycle 1 is first post-D3cold wake; cycles 2/3 are second/third
            if i == 2:
                RESULTS["Second wake stability"] = code + f" vo={snap.get('vo')} cap={snap.get('cap_w')}"
            if i == 3:
                RESULTS["Third wake stability"] = code + f" vo={snap.get('vo')} cap={snap.get('cap_w')}"
        st, ps = wait_d3cold(pci, 90)
        snapshot(pci, f"cycle-{i}-idle")
        if i == 1:
            RESULTS["Second wake stability"] = RESULTS.get("Second wake stability", "")  # filled on i==2
        out(f"cycle {i} returned {st}/{ps}")

    # Map: cycle1 = first after D3cold (survival), cycle2 = second wake, cycle3 = third
    if "Second wake stability" not in RESULTS or not RESULTS["Second wake stability"]:
        if len(survival_codes) >= 2:
            i, code, vo, cap = survival_codes[1]
            RESULTS["Second wake stability"] = code + f" vo={vo} cap={cap}"
    if "Third wake stability" not in RESULTS or not RESULTS["Third wake stability"]:
        if len(survival_codes) >= 3:
            i, code, vo, cap = survival_codes[2]
            RESULTS["Third wake stability"] = code + f" vo={vo} cap={cap}"

    hits = kernel_delta()
    if hits.strip():
        RESULTS["Kernel/amdgpu errors"] = "SEE hits above"
    else:
        RESULTS["Kernel/amdgpu errors"] = "none in delta"

    # final idle
    st, ps = runtime_status(pci), power_state(pci)
    out(f"final {st}/{ps}")
    leftover = drm_fds_of_pid(os.getpid())
    out(f"final test drm fds={leftover or 'none'}")
    pgrep = subprocess.run(["pgrep", "-a", "r9700-tunerd"], capture_output=True, text=True)
    out("final tunerd procs: " + (pgrep.stdout.strip() or "none"))
    print_table()
    return 0


def print_table() -> None:
    order = [
        "Stable R9700 discovery",
        "210 W apply/readback",
        "-25 mV apply/readback",
        "Short compute stability",
        "Returns to D3cold",
        "0 RPM restored",
        "Undervolt survives D3cold",
        "Power cap survives D3cold",
        "D3cold survival class",
        "Second wake stability",
        "Third wake stability",
        "Kernel/amdgpu errors",
    ]
    out("=== TABLE ===")
    for k in order:
        out(f"{k} | {RESULTS.get(k, 'n/a')}")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        out(f"FATAL {type(e).__name__}: {e}")
        print_table()
        raise

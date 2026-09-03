#!/usr/bin/python3
"""Five-cycle manual watcher test. Does not enable at boot."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

WANT = ("0x1002", "0x7551", "0x1043", "0x0626")
PCI_ROOT = Path("/sys/bus/pci/devices")
LOG = Path("/tmp/r9700-watcher-test.log")


def out(msg: str) -> None:
    print(msg, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


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
        raise SystemExit(f"discovery failed {hits}")
    return hits[0]


def runtime_status(pci: Path) -> str:
    return read_text(pci / "power" / "runtime_status").lower()


def power_state(pci: Path) -> str:
    return read_text(pci / "power_state")


def render_node(pci: Path) -> Path:
    for c in sorted((pci / "drm").iterdir()):
        if c.name.startswith("renderD"):
            return Path("/dev/dri") / c.name
    raise SystemExit("no render node")


def card_node(pci: Path) -> Path | None:
    for c in sorted((pci / "drm").iterdir()):
        if re.fullmatch(r"card\d+", c.name):
            return Path("/dev/dri") / c.name
    return None


def read_vo(pci: Path):
    if runtime_status(pci) != "active":
        return None
    text = read_text(pci / "pp_od_clk_voltage")
    in_off = False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("OD_VDDGFX_OFFSET"):
            in_off = True
            continue
        if s.startswith("OD_"):
            in_off = False
            continue
        if in_off:
            m = re.search(r"(-?\d+)\s*mV", s, re.I)
            if m:
                return int(m.group(1))
    return None


def read_cap_w(pci: Path):
    if runtime_status(pci) != "active":
        return None
    h = next((pci / "hwmon").iterdir())
    return int(read_text(h / "power1_cap")) / 1_000_000


def wait_d3cold(pci: Path, seconds: int = 30) -> tuple[str, str]:
    t0 = time.time()
    last = ("", "")
    while time.time() - t0 < seconds:
        last = (runtime_status(pci), power_state(pci))
        if last[0] == "suspended" and last[1] == "D3cold":
            return last
        time.sleep(0.5)
    return last


def tunerd_pids() -> list[int]:
    r = subprocess.run(["pgrep", "-f", "/usr/local/sbin/r9700-tunerd watch"], capture_output=True, text=True)
    pids = []
    for ln in r.stdout.split():
        try:
            pids.append(int(ln))
        except ValueError:
            pass
    return pids


def fuser_nodes(paths: list[Path]) -> str:
    args = ["fuser", "-v"] + [str(p) for p in paths if p and p.exists()]
    r = subprocess.run(args, capture_output=True, text=True)
    return (r.stdout + r.stderr).strip()


def journal_since(since: str) -> str:
    r = subprocess.run(
        ["journalctl", "-t", "r9700-tunerd", "--since", since, "--no-pager"],
        capture_output=True,
        text=True,
    )
    return r.stdout


def main() -> int:
    LOG.write_text("", encoding="utf-8")
    pci = discover()
    out(f"pci={pci.name}")
    render = render_node(pci)
    card = card_node(pci)
    out(f"render={render} card={card}")

    pids = tunerd_pids()
    if not pids:
        out("FAIL: watcher not running")
        return 1
    out(f"watcher pids={pids}")

    st, ps = runtime_status(pci), power_state(pci)
    out(f"A idle {st}/{ps}")
    if st != "suspended":
        out("NOTE: GPU not suspended at start; waiting")
        wait_d3cold(pci, 40)
        st, ps = runtime_status(pci), power_state(pci)
        out(f"A after wait {st}/{ps}")
    idle_ok = st == "suspended" and ps == "D3cold"

    fu_idle = fuser_nodes([p for p in (card, render) if p])
    out("fuser idle:\n" + (fu_idle or "(none)"))
    watcher_in_fuser = any(str(p) in fu_idle for p in pids) or "r9700-tunerd" in fu_idle
    out(f"watcher in fuser idle={watcher_in_fuser}")

    t0 = time.strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for i in range(1, 6):
        out(f"=== cycle {i} ===")
        fd = os.open(render, os.O_RDWR | os.O_CLOEXEC)
        t_wake = time.time()
        vo = None
        cap = None
        vo_at = None
        try:
            deadline = t_wake + 5.0
            while time.time() < deadline:
                if runtime_status(pci) == "active":
                    vo = read_vo(pci)
                    cap = read_cap_w(pci)
                    if vo == -25:
                        vo_at = time.time() - t_wake
                        break
                time.sleep(0.1)
            # hold a couple seconds; vo must stay -25
            stayed = True
            writes_ok = True
            hold_end = time.time() + 3.0
            samples = []
            while time.time() < hold_end:
                v = read_vo(pci)
                samples.append(v)
                if v != -25:
                    stayed = False
                time.sleep(0.4)
            out(f"cycle {i} vo_latency_s={vo_at} vo={vo} cap={cap} samples={samples}")
        finally:
            os.close(fd)
        st2, ps2 = wait_d3cold(pci, 25)
        out(f"cycle {i} idle {st2}/{ps2}")
        rows.append({
            "cycle": i,
            "vo_latency": vo_at,
            "vo": vo,
            "cap": cap,
            "stayed": stayed,
            "d3cold": st2 == "suspended" and ps2 == "D3cold",
        })

    j = journal_since(t0)
    out("--- journal ---")
    out(j)
    restored = len(re.findall(r"VDDGFX offset restored: -25 mV", j))
    cap_writes = len(re.findall(r"power cap applied:", j))
    already_cap = len(re.findall(r"power cap already", j))
    out(f"journal restored_vo={restored} cap_applied={cap_writes} cap_already={already_cap}")

    fu_after = fuser_nodes([p for p in (card, render) if p])
    out("fuser after:\n" + (fu_after or "(none)"))
    watcher_hold = "r9700-tunerd" in fu_after

    cpu = ""
    for p in tunerd_pids():
        r = subprocess.run(["ps", "-p", str(p), "-o", "pid,pcpu,pmem,rss,etime,cmd", "--no-headers"], capture_output=True, text=True)
        cpu += r.stdout.strip() + "\n"
        # threadless /proc stat
        try:
            stat = Path(f"/proc/{p}/stat").read_text().split()
            utime = int(stat[13]); stime = int(stat[14])
            hz = os.sysconf("SC_CLK_TCK")
            cpu += f" proc_stat utime+stime={(utime+stime)/hz:.2f}s rss_kb={Path(f'/proc/{p}/status').read_text()}\n"
        except Exception as e:
            cpu += f" stat err {e}\n"
    # trim rss dump
    cpu_brief = []
    for p in tunerd_pids():
        r = subprocess.run(["ps", "-p", str(p), "-o", "pid=,pcpu=,rss=,etime=", "--no-headers"], capture_output=True, text=True)
        cpu_brief.append(r.stdout.strip())
    out("cpu: " + " | ".join(cpu_brief))

    out("=== TABLE ===")
    out("Cycle | vo_latency_s | vo | cap_W | stayed_-25 | D3cold")
    all_ok = idle_ok and not watcher_in_fuser and not watcher_hold and restored == 5
    for row in rows:
        out(
            f"{row['cycle']} | {row['vo_latency']} | {row['vo']} | {row['cap']} | {row['stayed']} | {row['d3cold']}"
        )
        if not (row["vo"] == -25 and row["stayed"] and row["d3cold"] and row["vo_latency"] is not None):
            all_ok = False
        if row["vo_latency"] is not None and row["vo_latency"] > 3.2:
            out(f"NOTE cycle {row['cycle']} latency {row['vo_latency']:.2f}s > 3s target")
    out(f"idle_d3cold={idle_ok} watcher_fuser={watcher_in_fuser or watcher_hold} restored_logs={restored} cap_writes_during_test={cap_writes}")
    out("PASS" if all_ok and cap_writes == 0 else ("PASS_WITH_NOTES" if all_ok else "FAIL"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())

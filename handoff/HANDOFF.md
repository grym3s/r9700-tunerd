# r9700-tunerd — full handoff (2026-09-04)

Owner: Richard Garnett (grymes). Machine: `Strix-AI`, Arch/Omarchy, Hyprland
(Wayland), kernel 7.1.9-arch1-2. Written by the Claude Code orchestrator at the
end of the 2026-09-03/04 build session. Everything below was measured on the
live machine unless marked otherwise.

Companion documents in this folder:

| File | What |
|---|---|
| `AGENTS.md` | The Hermes agent team (Forge / Ray / Vale / Halo): models, endpoints, configs, how to connect, how the orchestrator drove them |
| `tools/ask_qwen.py` | The one-shot helper used to give Ray/Halo tasks with thinking on and line-numbered attachments |
| `tools/llama-halo.service` | Halo's llama.cpp user unit (copy of the live file) |
| `tools/lmstudio-qwen3.8-27b-dflash2.json` | Ray's LM Studio per-model config with DFlash2 enabled (copy of the live file) |
| `agent-work/` | Every task brief given to Ray/Halo and every reply that was acted on (task30 app spec, task31 Halo review, task40 evict-guard spec, …). This is where to look for "what did the agents do"; the work was not routed through Hermes sessions (see AGENTS.md, "How the orchestrator actually drove Ray and Halo") |

Repo docs that this handoff does not duplicate:
`docs/ARCHITECTURE.md`, `docs/AMDGPU-R9700-NOTES.md` (measured hardware facts),
`docs/TESTING.md`, `docs/ACCEPTANCE-2026-09-04.md`, `docs/MATRIX-2026-09-04.md`,
`docs/UI.md`, `docs/ROADMAP.md`, `README.md`.

---

## 1. What the project is

A runtime-PM-aware tuner for the ASUS Radeon AI PRO R9700 32 GB (AMD Navi 48,
RDNA4, PCI `0x1002:0x7551`, subsystem `0x1043:0x0626`) on Linux. It:

- watches the card's runtime-PM state without ever waking it, and re-applies
  the VDDGFX offset after every wake, because the offset does not survive
  D3cold (measured);
- applies and keeps a power cap (survives runtime suspend, resets to 300 W on
  cold boot, measured);
- validates every setting against the live `OD_RANGE` / `power1_cap_min/max`,
  or against a cached copy of those ranges while the card sleeps;
- exposes a local dashboard (token-guarded HTTP + SSE on 127.0.0.1:7970) and,
  as of today, a native GTK window that hosts it;
- ships a hardware test runner, a real-workload benchmark harness, and a gated
  undervolt stepping script.

Identity is always vendor/device/subsystem. Never card numbers, never PCI bus
addresses (the bus address is discovered at runtime and logged only).

## 2. Where everything lives

| Thing | Path |
|---|---|
| Repo (git, branch `main`) | `~/src/r9700-tunerd` |
| Installed daemon (byte-identical to repo) | `/usr/local/sbin/r9700-tunerd` |
| Config | `/etc/r9700-tunerd.conf` (today: `POWER_LIMIT_W=210`, `VOLTAGE_OFFSET_MV=-50`) |
| Watcher unit | `r9700-tunerd.service` (system, enabled, active) |
| Apply-on-udev unit | `r9700-tunerd-apply.service` |
| Runtime state | `/run/r9700-tunerd/state`, `/run/r9700-tunerd/ranges.json` |
| Dashboard server (user unit, transient) | `r9700-ui.service`, started with `systemd-run --user --unit r9700-ui --working-directory=$HOME/src/r9700-tunerd --collect python3 tools/r9700-ui.py` |
| Dashboard URL | `journalctl --user -u r9700-ui.service \| grep TOKEN` then `http://127.0.0.1:7970/?t=<token>` (token rotates per start) |
| Native app | `~/.local/bin/r9700-tuner`, menu entry "R9700 Tuner" (installed by `tools/install-app.sh`, no root) |
| Bench results | `~/r9700-bench/*.json` and `.csv`; `matrix.csv`; `superseded/` for runs the harness later invalidated |
| Hardware test log | `/var/log/r9700-hwtest.log` |
| Sudoers rule (installed by the owner, not in git) | `/etc/sudoers.d/r9700-tunerd` |
| Unit tests | `.venv/bin/python -m pytest -q tests/` (69 tests, all pass at HEAD) |

The sudoers rule grants the owner NOPASSWD for exactly: `install.sh`,
`tests/hw/r9700-hwtest.py *`, `/usr/local/sbin/r9700-tunerd *`, and
`systemctl daemon-reload|restart|start|stop|enable|status r9700-tunerd.service`.
The dashboard server, the matrix script, and the app all call the daemon CLI
through `sudo -n`. Nothing else needs root.

## 3. Standing rules from the owner (still in force)

- Never open `/dev/dri/card*` or `renderD*` from the tuner or its tools.
- Never use `amdgpu.runpm=0`; never busy-poll.
- While the card is suspended, read only `power/runtime_status`,
  `power_state`, `power/runtime_suspended_time`, `power/control` (all measured
  safe). Any hwmon / `pp_*` / `gpu_busy_percent` read takes a PM reference and
  re-arms the 5 s autosuspend, so a dashboard that polls sensors every 2 s
  holds the card awake forever (measured; that is why the UI server samples
  adaptively: 2 s when busy, 9 s when idle, nothing when asleep).
- Do not reboot without the owner's approval. Do not touch BIOS, kernel
  command line, initramfs, ROCm, Hyprland GPU settings. Do not re-enable LACT.
  Do not kill the owner's llama-server / LM Studio without approval.
- No fan control yet (Phase D placeholder in the config is commented out).
- Do not rewrite the working tuner; no big framework.
- The live hardware is authoritative over documentation and assumptions.
- Build with the cheap local agents (Ray, Halo). Forge reviews, Vale designs.
  Never use cloud models for construction (see `AGENTS.md`).
- "Do not test -50 mV yet" was lifted by the owner on 2026-09-04 ("run it"),
  and -50 mV / 210 W was then adopted ("ok go"). -75 and -100 mV were tested
  once each and are not adopted.

## 4. State of each priority

### Priority 1, core hardening: DONE, accepted on hardware
Config validation with ranges and keep-last-good, guarded main loop,
`require_runtime_pm()` (refuses unless `power/control=auto`, which is the
discriminator between the R9700 and the Strix iGPU; `boot_vga` must not be
used), atomic writes, journal-aware stderr, refuse VO write without
`OD_RANGE`, counter-based wake detection via `runtime_suspended_time`
(the 2 s poll misses the sub-2 s active window otherwise), cached ranges,
`set-undervolt` / `set-power-cap` subcommands, hardened systemd unit with
restart-storm limits. Tests: `tests/test_unit.py` (49).

### Priority 2, reboot acceptance: DONE
`docs/ACCEPTANCE-2026-09-04.md`. Boot → idle → D3cold; workload → active →
offset restored, cap 210 W; stop → D3cold; no tuner DRM handles. The runner's
`reboot-check` subcommand automates it.

### Priority 3, real-workload validation: DONE
`tools/r9700-bench.py` v3 (warm-up request, post-warm-up sysfs baseline, 3 s
ramp excluded, stability windows, energy integration, D3cold recovery time).
Trustworthy numbers at -25 mV / 210 W: 37.6–37.9 tok/s, ~205 W,
0.18 tok/s/W, Tj 80–81 °C, D3cold 15–20 s after the run. Two runs agreed
within 1 %.

### Priority 4, undervolt characterisation: FIRST PASS DONE
`docs/MATRIX-2026-09-04.md`, raw rows in `docs/results/matrix-2026-09-04-cap210.csv`.

| offset mV | gen tok/s | mean W | tok/s/W | Tj max °C | D3cold s | verdict |
|---:|---:|---:|---:|---:|---:|---|
| -25 | 37.8 | 205 | 0.182 | 75 | 20 | PASS |
| -50 | 39.6 | 207 | 0.189 | 79 | 21 | PASS ★ |
| -75 | 39.6 | 207 | 0.189 | 82 | 16 | PASS |
| -100 | 39.4 | 207 | 0.188 | 83 | 22 | PASS |

Zero kernel faults across the run. The whole gain is at -50 mV (+4.8 %); the
card is power-bound at 210 W so deeper offsets buy nothing. Tj rises with
each step but the steps ran back to back with ~20 s of sleep between them,
so thermal history is confounded with the offset. One workload, one run per
point, ~80 s steady state each: not a stability proof.

**Adopted: -50 mV / 210 W** (config written, watcher applies on every wake,
verified live). Previous safe point was -25 mV:
`sudo -n /usr/local/sbin/r9700-tunerd set-undervolt -25` goes back.

Not done: caps 230/250/300 W, randomised repeats with cool-down, longer
soak, other workloads. `tools/r9700-matrix.py --caps 250,300 --offsets -25,-50`
is the natural next pass; it gates every step and restores the config safe
point on any failure. Balanced/Performance profiles in the UI stay at -25 mV
until -50 mV is measured at their caps.

### Priority 5, GPU tuning UI: v1 DONE, native app WIP
- `tools/r9700-ui.py` v2: stdlib-only server, token per start, 127.0.0.1
  only, adaptive sampling, SSE `/api/events`, `/api/status|set|apply|reset|
  bench|bench/run|bench/status|profiles`, 64 KiB body cap, chunked refused,
  `secrets.compare_digest`, token never logged. Tests: `tests/test_ui_server.py` (20).
- `ui/index.html`: single-file dark dashboard (state pill, power/thermals/
  clocks, live trace with sleep shading, sliders bounded by live/cached
  ranges, profiles, benchmark table + launcher, watcher journal).
- Dashboard hazard test (documented in `docs/TESTING.md`): with an SSE client
  connected, the card must still reach D3cold within ~20 s of going idle.
  PASS on the browser dashboard.
- Native app (`tools/r9700-app.py`, `tools/install-app.sh`, `contrib/
  r9700-tuner.desktop`, `contrib/r9700-tuner.svg`): GTK 3 + WebKit2GTK 4.1
  window that attaches to the running `r9700-ui.service` (reads the token
  from its journal) or spawns its own server; hardware acceleration off;
  navigation locked to 127.0.0.1; Ctrl+R / Ctrl+Q / F11; remembers window
  size; runtime self-check that the process holds no R9700 DRM handle.
  Built by Ray, reviewed by Halo, fixed by the orchestrator (see §6).
  **Status: installed and launches, but see the open issue in §5 before
  calling it released.**

## 5. Open issue: R9700 wake loop seen once while the app was open

Evidence, all from 2026-09-04 10:06–10:08 local:

- Daemon journal: `runtime active` / `runtime suspended` alternating every
  ~13 s while the app window was open.
- `inotifywait` on `/dev/dri/card1` (the R9700's primary node) showed an
  OPEN + CLOSE every 13 s, each one lining up with a wake. Opening a DRM
  primary node takes a PM reference, so this is sufficient to explain the
  wakes.
- With the app stopped: zero opens in 42 s, card stayed asleep.
- The opener is too brief to catch by scanning `/proc/*/fd` at 30 ms
  (2000 scans, no hit). Persistent holders are known and benign for
  suspend: Hyprland holds `card1` (2 fds, always), Ray's and Halo's
  llama-server hold `renderD128`.
- Bisection probes launched from the orchestrator's shell (bare GTK window,
  blank WebKit view, WebKit on the live dashboard, 32 s each): zero opens.
- The real app relaunched via `systemd-run --user` after the fixes below,
  36 s: zero opens. It did **not** reproduce.
- Confounders present during the one reproduction and absent afterwards: the
  screen was locked (hyprlock), and a stale Hyprland "Application Not
  Responding" dialog for an earlier crashed app instance was on screen
  (the compositor pings unresponsive windows). The orchestrator killed the
  dialog at ~10:10.

**Soak result (11:01–11:06, app window open throughout, launched via the
menu path):** one wake in five minutes, at the moment a second app instance
was launched (11:05:40, asleep again 11 s later); no 13 s loop. A single
short wake per app launch is expected: GTK/WebKit's EGL initialisation
enumerates DRM render nodes and touching `renderD128` takes a PM reference
for the 5 s autosuspend window. That is a launch cost, not a hold. The
13 s loop has not recurred since the stale ANR dialog was removed. Parent-
death cleanup of a spawned server was tested (SIGKILL the app, server
exited, port closed: PASS).

What to do next if the loop is ever seen again (cheapest first):

1. Launch the app from the menu, leave it open for 5 minutes, then
   `journalctl -t r9700-tunerd --since "5 min ago" | grep -E "active|suspended"`.
   A healthy result is one wake at most (the app's initial page load reads
   sensors only if the card is already active) followed by continuous sleep.
2. If wakes recur: `sudo pacman -S strace` (or `fatrace`) and run
   `fatrace -f O | grep dri` or strace the app tree; that names the opener
   in one run. Neither tool is installed today.
3. Repeat step 1 with the screen locked, to test the hyprlock hypothesis.
4. Only after step 1 is clean should the app be considered released; until
   then the browser dashboard is the safe path.

#### 5a. RESOLVED (15:10): the wake loops were desktop monitors, not the app

- **Mission Center** (`io.missioncenter.MissionCenter`, GPU page enabled,
  update interval setting 20) opens the R9700's render node and polls it.
  While its window is active it holds the card awake continuously (measured
  15:04:40 onward: card active, `missioncenter-m` is the only non-compositor
  holder of `renderD128`). It launched at 13:44:15 in boot -2, eleven
  seconds before the R9700 resume that never completed (the freeze in §5d):
  it was the process that opened the card into the 15.5 GB GTT trap. It
  was launched again at 13:57, 14:26 and 15:04. The 14:45–15:00 loop of 72
  wakes at 13 s happened with it running in the background.
- **Omarchy display/brightness panel** was open during both loop windows,
  which made it a suspect. Clean retest at 15:15 with Mission Center
  closed: `hyprctl monitors all -j`, `brightnessctl -l` and the full
  `omarchy-monitor-state` each left the card suspended. The panel is
  cleared. The morning loop is attributed to Mission Center as well (its
  helper process was present in the 10:38 process scans; exact launch time
  before 09:50 not recovered).
- The tuner app is cleared: at 10:06 the panel was open, at 14:45 Mission
  Center was running, and the app was not involved in either.

Rule for the owner: any GPU monitor (Mission Center, btop with GPU, nvtop,
amdgpu_top, LACT) that is pointed at the R9700 defeats runtime PM for as
long as it runs; the tuner's own dashboard is the only monitor built not
to. Closing Mission Center lets the card sleep again within 5 s.

## 5b. Second open issue: GTK main thread stuck in a futex wait (seen twice)

Two app instances ended with the main thread in `futex_do_wait` (19 threads,
no GLib poll): one launched from the orchestrator's sandboxed shell (never
showed a window), and one launched from the app menu at 10:45 that had
rendered the dashboard normally before it got stuck. In that state a
GLib-installed signal handler never runs, so SIGTERM was a no-op: Hyprland
raised an "Application Not Responding" dialog and systemd needed its 90 s
SIGKILL. A third instance launched at 10:58 stayed healthy (main thread in
`poll`). Reproduction is unknown; the suspects are a synchronous WebKit IPC
wait or a GLib/dconf lock on startup.

Mitigation shipped (commit `0ef5a98`): the app installs **no** signal
handlers, so the kernel's default disposition terminates it instantly even
when hung, and a server child it spawned is torn down by
`PR_SET_PDEATHSIG` rather than by parent code. Graceful cleanup (window size,
child SIGTERM) still runs on window close and Ctrl+Q via `atexit`.

Also shipped (commit `15578f7`): when attached to `r9700-ui.service` the app
re-reads the unit's journal every 10 s and reloads on a token change, so a
server restart no longer strands the window with 403s (verified: restart at
10:59:10, window reloaded at 10:59:14). The owner's screenshot with "Apply
error: invalid or missing token" and "range: live values not yet known" was
exactly that stranded state on a pre-fix window.

## 5c. Side issue: USB Wi-Fi dongle resets (not caused by the tuner)

Owner report 2026-09-04: "whenever the GPU spins up it breaks my USB Wi-Fi".
Dongle: Realtek RTL8822BU (`rtw88_8822bu`, `wlp201s0f4u1`), SuperSpeed port
`2-1` on xHCI `0000:c9:00.4` (the controller inside the Strix Halo display
complex), 5 GHz channel 40. The built-in MT7925 (`wlp195s0`) is unused
because the owner needs the dongle's range.

Kernel log: plugged in 00:45, clean through ~2,000 R9700 wakes, then three
faults 11:32–11:39: a register read timeout (-110) while the card was
asleep, a USB reset 3 s after a wake, and "device not accepting address"
(-71) plus a reset while the card had been active for two minutes. One of
three lines up with a wake; the USB controller never runtime-suspended.
Verdict: a marginal USB 3 link on that port, possibly EMI from the external
GPU enclosure (the R9700 is on an external PCIe 3.0 x4 link with its own
PSU), not the tuner and not the wake itself.

Plan agreed with the owner: extension cables are on order; then move the
dongle to a port on the other controller (`0000:cb:00.x`, where the T9 and
receivers are) and away from the enclosure. Recommended in addition (owner
runs it, needs root): keep the dongle in USB 2.0 mode and disable deep
power save, then replug:

    echo 'options rtw88_usb switch_usb_mode=0' | sudo tee /etc/modprobe.d/rtw88-usb.conf
    echo 'options rtw88_core disable_lps_deep=1' | sudo tee -a /etc/modprobe.d/rtw88-usb.conf

**Outcome (16:35):** the owner applied both modprobe options, reloaded the
module, moved the dongle to the other controller and replugged it. It now
enumerates at 480 Mb/s (USB 2.0). Zero faults in the first hour (15:33 →
16:33) against 27 fault-minutes earlier in the day, and the owner reports it
is faster than before (no more stalled transfers, and no SuperSpeed noise
next to the antenna). Considered fixed; the root cause was the rtw88
driver's USB 3 mode on this chip, not the GPU.

## 5d. The 2026-09-04 afternoon "GPU instability": BIOS UMA change, not the GPUs

Six boots on 2026-09-04. Journal evidence per boot:

| boot | up | ended | how | system RAM | iGPU carve-out |
|---|---|---|---|---|---|
| -5 | 00:45 | 12:30 | clean shutdown (user reboot) | 64 GB | 64 GB |
| -4 | 12:31 | 12:38 | clean shutdown (user reboot, BIOS change here) | 64 GB | 64 GB |
| -3 | 12:40 | 13:33 | clean shutdown (user reboot) | **32 GB** | **96 GB** |
| -2 | 13:34 | 13:47 | **hard reset after a freeze**: `ChatGPT` blocked >122 s in `amdgpu_driver_open_kms → rpm_resume` (R9700 D3cold resume never completed; daemon saw no "runtime active" after 13:35:51) | 32 GB | 96 GB |
| -1 | 13:48 | 13:53 | **hard reset after an OOM storm**: page-allocation failures, OOM killer took `lm-studio` ×2, both `llama-server`s, `Hermes`, `quickshell` | 32 GB | 96 GB |
| 0 | 13:54 | | running | 32 GB | 96 GB |

No amdgpu ring timeout, GPU reset, page fault, SMU/MES failure, or ECC
event in any boot. The undervolt (-50 mV) and cap (210 W) were applied
normally in every boot and leave no signature. The R9700 is not the cause.

Mechanism (measured, see kernel `Memory:` and `VRAM:` lines per boot): at
12:39 the BIOS UMA frame-buffer for the Strix Halo iGPU went from 64 GB to
96 GB, cutting system RAM from 64 GB to 32 GB. Two consequences:

1. amdgpu's GTT (system memory the dGPU can use) is capped at half of RAM:
   **15.5 GB now, ~31 GB before**. A dGPU evicts its VRAM contents into GTT
   on every runtime suspend (D3cold) and restores them on resume; that copy
   is why D3cold entry took 15–20 s with Ray's model resident. Ray's
   Qwen3.8-27B Q4_K_M occupies **17.7 GB of VRAM > 15.5 GB GTT**, so a
   suspend/resume cycle with the model loaded can no longer complete.
   Boot -2's freeze is exactly a resume that never finished.
2. Everything else (LM Studio, Hermes, Claude, ChatGPT, browsers, the page
   cache for two 20 GB model files) now has 32 GB minus the above. Boot -1
   is the OOM killer taking the GPU apps out one by one, then the desktop.

Fix (owner, BIOS rule): set the UMA carve-out back to 64 GB. Halo's Q6
model uses ~27 GB of it, so 64 GB is ample. Until that is done: do not load
Ray's model on the R9700, or the next sleep/wake will hang the machine
again. `lms ps` shows it is not loaded on the current boot.

Hardening to add to the daemon (next task for Ray, Halo reviews): at each
wake, if `mem_info_vram_used > mem_info_gtt_total` (both readable while
active), log a loud warning that the card cannot safely suspend with this
much VRAM in use, and surface it in the dashboard state pill. Do not change
runtime-PM behaviour automatically.

## 6. What was fixed in Ray's app before/after Halo's review

- `load-failed` handler had the wrong arity.
- Child stdout was read only until the token line; the server logs every
  request to stdout, so the pipe would fill and block the server. Now drained
  for the life of the child.
- `signal.signal` handlers cannot fire while `Gtk.main()` blocks in C;
  replaced with `GLib.unix_signal_add`.
- `GLib.set_prgname` moved before GTK initialises so the Wayland app_id is
  `r9700-tuner` (Hyprland rules / `StartupWMClass` match). Deprecated
  `set_wmclass` removed.
- `NEW_WINDOW_ACTION` (target=_blank) now opens externally instead of
  spawning an unmonitored WebKit window (Halo #4).
- `set_hardware_acceleration_policy` lives on `WebKitSettings`, not the view
  (crashed at start).
- `show_all()` was never called (window never appeared).
- Window-size save used non-existent `get_width()`; it raised on quit, so
  SIGTERM was ignored and systemd had to SIGKILL after 90 s. Now `get_size()`
  in a try/except that can never block shutdown.
- `atexit` cleanup registered before any GTK call so a failed start cannot
  orphan a spawned server (Halo #2).
- Installer bakes the absolute launcher path into `Exec=` because app
  launchers under Hyprland/Omarchy do not necessarily have `~/.local/bin` on
  PATH.
- Runtime self-check added: 1.5 s after start the app scans its own fds and
  replaces the page with a refusal if any points at a DRM node whose device
  is `0x1002:0x7551`.

Measured: even with acceleration off, GTK/Wayland/EGL opens the *display*
GPU's nodes (`card2`/`renderD129`, the Strix iGPU) in the app and the WebKit
web process. That is unavoidable for any Wayland client and irrelevant to
the R9700. Halo's review statement "no /dev/dri open path remains" is
therefore wrong in general and right for the R9700 specifically; the
self-check encodes the distinction.

## 7. How to run things

```bash
# unit tests
cd ~/src/r9700-tunerd && .venv/bin/python -m pytest -q tests/

# daemon status (safe while asleep: prints cached ranges instead of waking)
sudo -n /usr/local/sbin/r9700-tunerd status

# change settings (validated live when active, against cache when asleep)
sudo -n /usr/local/sbin/r9700-tunerd set-undervolt -50
sudo -n /usr/local/sbin/r9700-tunerd set-power-cap 210

# install a new build (never touches the watcher's enable state)
sudo -n ~/src/r9700-tunerd/install.sh && sudo -n systemctl restart r9700-tunerd.service

# hardware runner (idle / cycles / storm / config-typo / sigterm / reboot-check)
sudo -n ~/src/r9700-tunerd/tests/hw/r9700-hwtest.py cycles --cycles 5

# benchmark at the current setting (Ray's LM Studio endpoint)
python3 tools/r9700-bench.py run --endpoint http://127.0.0.1:1234/v1 \
    --model qwen/qwen3.8-27b@q4_k_m --label mylabel

# gated stepping (dry-run first; restores config safe point on any gate failure)
python3 tools/r9700-matrix.py --endpoint http://127.0.0.1:1234/v1 \
    --model qwen/qwen3.8-27b@q4_k_m --offsets -25,-50 --caps 250 --dry-run
python3 tools/r9700-matrix.py --report

# dashboard server as a user unit, then the app
systemd-run --user --unit r9700-ui --working-directory=$HOME/src/r9700-tunerd --collect python3 tools/r9700-ui.py
tools/install-app.sh && r9700-tuner
```

Do not run a benchmark while Ray is generating (same GPU); the numbers will
be contaminated. Halo's llama-server on the iGPU does not wake the R9700
during inference (measured).

## 8. Measured hardware facts worth re-reading before touching anything

Full list in `docs/AMDGPU-R9700-NOTES.md`. The ones that bit us:

- VDDGFX offset does not survive D3cold; the cap does, but resets on cold boot.
- Every resume logs benign "Failed to upload overdrive table /
  OD_UNSUPPORTED_FEATURE / Failed to upload customized OD settings". The
  matrix script's kernel gate whitelists exactly these.
- Autosuspend delay is 5 s; the active window after a wake can be under 2 s,
  so a 2 s poll misses wakes. Use the `runtime_suspended_time` counter.
- `fs.protected_regular` blocks root appending to a user-owned `/tmp` file;
  logs go to `/var/log`.
- `JOURNAL_STREAM` in the environment is inherited by children; detect the
  journal by `fstat` dev:ino of stderr, not by the variable.
- `pkill -f <pattern>` from a tool shell kills the tool shell if the pattern
  matches its own command line (happened twice). Use `systemd-run --user` for
  anything long-lived and kill by unit name.

## 9. Git

39 commits on `main`. Last accepted install is the build at the range-cache
commit; the daemon has not changed since. Latest commits:

```
c8e28a5 ui: Efficiency profile is the measured -50 mV / 210 W point
921b939 roadmap: record P4 first pass
cca5304 P4 first pass: undervolt matrix at 210 W ...; matrix script sudo + suspended-verify + live-target gate
95812ae ui server: reject negative Content-Length (Halo sign-off); 20 unit tests for the server (Ray)
```

The native app files and this handoff are committed together after this
document, labelled WIP for the app.

## 10. Next steps, in order

1. Resolve §5 (5-minute soak with the app open; strace if needed).
2. Matrix pass at 250 W and 300 W with -25/-50 mV; then set Balanced and
   Performance profiles from measured data.
3. Randomised repeats with cool-down at 210 W to de-confound Tj.
4. Longer soak at -50 mV / 210 W under the owner's real workload.
5. UI follow-ups from the roadmap: matrix results view, profile editor,
   tray/launcher integration.
6. **Custom fan curve (owner request, 2026-09-04).** The owner wants to set
   a custom fan curve from the app. Constraints that must hold, from the
   hardware notes and the daemon's design:
   - Active-only state machine. Fan control means writing `pwm1_enable=1`
     and `pwm1` in hwmon, and reading temperatures to drive it; every one
     of those reads/writes takes a PM reference. So the controller may run
     only while the card is already active (busy mode of the sampler), must
     hand control back to firmware (`pwm1_enable=2`) before the card is
     allowed to idle, and must never be the thing that keeps it awake. The
     daemon already has the wake/suspend edge detection to drive this.
   - Firmware fallback on every exit path: watcher stop, crash, config
     typo, SIGKILL. A udev/oneshot or the watcher's own startup must reset
     `pwm1_enable=2` if it finds it at 1 with no live controller.
   - Curve points validated against `pwm1_min/max` and `temp*_crit`; the
     config placeholder `FAN_POINT_n=temp:pct` and hysteresis keys already
     exist commented out in `/etc/r9700-tunerd.conf`.
   - UI: a curve editor (draggable points over the live junction-temperature
     trace) that only writes through the daemon CLI (`set-fan-curve`),
     with a "firmware auto" button. Vale designs it; Ray/Halo build it;
     Halo reviews the PM-safety.
   - Acceptance: the dashboard hazard test still passes with the controller
     enabled (card reaches D3cold within ~20 s of idle), and a reboot check.
   Not started. Do not enable any fan write path until the above is
   implemented and reviewed.
7. Owner feedback 2026-09-04: the live trace felt too slow. Shipped in
   the same session: busy-mode sampling and SSE ticks at 0.5 s (was 2 s),
   idle back-off unchanged at 9 s so an idle card still sleeps.
8. Add an OpenRouter/Anthropic key to `~/.hermes/.env` and the Forge/Vale
   profile `.env` files if the cloud agents are to be used (see `AGENTS.md`).

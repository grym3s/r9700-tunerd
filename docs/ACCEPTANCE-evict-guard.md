# Eviction guard — hardware acceptance, 2026-09-04

Build under test: main `ad27bcc` (eviction guard: Sonnet cards a–c, Haiku docs,
Forge review approved on kanban `t_68c95661`), installed on the live daemon at
17:05 and carried through the owner's cold shutdown at 17:12. Boot at 17:12:29.
Run by the Claude Code orchestrator with the owner present; evidence for kanban
`t_7fc5338a` (Forge signs off). Machine facts: BIOS carve-out 96 GB, 30 GiB
system RAM visible, amdgpu GTT 16.6 G (15.5 GiB), Ray's model 17.74 GB in
R9700 VRAM, `EVICT_GUARD=1`, `EVICT_GUARD_MARGIN=0.90`, `POLL_INTERVAL_S=2`.

Procedure: `docs/TESTING.md` §6 steps 1–10 plus the timing criteria on the card.
Halo's llama-server was stopped first (never two 27B servers on the 32 GB
configuration); LM Studio server started; the model was JIT-loaded by one
request to `qwen/qwen3.8-27b@q4_k_m`; unloaded with `lms unload --all`.

## Boot and reboot-check (17:13–17:18)

- Boot: oneshot re-applied the 210 W cap, watcher restored -50 mV, both logged
  once; first D3cold at 17:13:38.
- `reboot-check` at 17:17: all seven criteria Y (service enabled+active, lactd
  inactive, no tuner DRM handles, boot→D3cold, wake→offset→cap→D3cold,
  stop→D3cold, no amdgpu failures beyond OD). PASS.

## Hold / release timeline

| Time | Event | Source |
|---|---|---|
| 17:20:07 | card active for LM Studio start, `control=auto`, guard idle | sampler |
| 17:20:12 | JIT request sent to 127.0.0.1:1234 | curl |
| 17:20:14 | `evict-guard: holding dGPU awake — VRAM 16.0G > 90% of GTT 16.6G` | journal |
| 17:20:15 | `power/control=on` observed (≤ 1 poll after the log line) | sampler |
| 17:20:42 | `runtime_status=active`, `power_state=D0`, `control=on`; model listed 17.74 GB | sysfs, `lms ps` |
| 17:21:20–17:21:33 | 400-token generation on the held card, HTTP 200 in 12.7 s | curl |
| 17:22:27 | `lms unload --all` | lms |
| 17:22:29 | `evict-guard: released hold — VRAM 0.2G < 72% of GTT 16.6G` | journal |
| 17:22:29 | `power/control=auto` (+2 s after unload) | sampler |

Hold duration 135 s; the card stayed active/D0 for the whole hold, including
the generation. Kernel log 17:19–17:25: no amdgpu faults, resets, rpm_resume
hangs, or OOM lines (only the known OD upload warnings).

## Criteria

| Criterion | Result | Evidence |
|---|---|---|
| hold logged within one poll of the model becoming resident | PASS | log at 17:20:14, 2 s after the request |
| `power/control=on` while holding | PASS | 17:20:15 onward |
| card stays awake for the whole hold (dwell ≥ 30 s) | PASS | 135 s, D0 throughout |
| release logged after unload | PASS | 17:22:29, VRAM 0.2G < 72 % |
| `power/control=auto` after release | PASS | +2 s |
| D3cold within ~20 s of release | PASS (second run) | first run: card stayed active after release because Mission Center was open (documented hold culprit, HANDOFF.md §5a) **and** the orchestrator's own sampler called `r9700-tunerd status` every second, whose sensor reads re-arm autosuspend; D3cold came 17:30:00, ~30 s after both were removed. Clean second run (17:32–17:33, sysfs-only timing, table below): `control=auto` +0.8 s, D3cold +7.1 s after `lms unload --all`. |
| `r9700-tunerd status` shows `evict_guard=holding` | **FAIL** | printed `evict_guard=idle vram_used=0.0G` at 17:20:42 while holding. Defect 2 on kanban `t_35c35fc7`. |
| dashboard shows the held-awake pill | **FAIL (expected)** | `/api/status` had `control=on`, `runtime_status=active` but `daemon_state`/`eviction_risk` null: the runtime state dir is gone (Defect 1, `t_35c35fc7`), and main's dashboard has no held-awake pill yet (R0.1 branch `wt/r01-ui-evict-pill`, card `t_4cf3bb0b`). |
| zero kernel faults | PASS | see above |
| `cycles --count 5` after the run (17:30) | PASS | restore latency 0.5–2.2 s, -50 mV held, cap 210 W, D3cold 6.0–6.5 s every cycle, 5 journal wakes, 0 cap writes |
| `storm` after the run (17:31) | PASS | 10/10 real wakes detected, watcher PID stable, no traceback, 0 kernel errors, D3cold 7.8 s |

## Second hold/release run (clean timing, 17:32–17:33)

Halo stopped, LM Studio server started, no sampler or status calls; only
`power/control`, `power/runtime_status` and `power_state` were read.

| Time | Event |
|---|---|
| 17:32:50.6 | JIT request sent |
| 17:32:53 | `evict-guard: holding dGPU awake — VRAM 15.8G > 90% of GTT 16.6G` (while the model was still loading) |
| 17:33:16 | model resident, reply HTTP 200; `control=on`, `runtime_status=active` |
| 17:33:31.2 | `lms unload --all` |
| 17:33:32 | `evict-guard: released hold — VRAM 0.0G < 72% of GTT 16.6G`; `control=auto` at +0.8 s |
| 17:33:38.8 | `runtime_status=suspended`, `power_state=D3cold` at +7.1 s |

LM Studio server stopped and Halo's server restarted afterwards.

## Defects found

1. **Runtime state directory deleted at boot.** Both `r9700-tunerd.service` and
   `r9700-tunerd-apply.service` declare `RuntimeDirectory=r9700-tunerd` under
   `ProtectSystem=strict`. The oneshot finished 0.4 s after the watcher started
   and systemd removed `/run/r9700-tunerd`; `write_state()` swallows the
   `OSError`, so the state file and `ranges.json` are silently gone for the
   rest of the session (`ls /run/r9700-tunerd` missing at 17:24). Fix:
   `RuntimeDirectoryPreserve=yes` on both units and a warn-once on failed
   state writes. Kanban `t_35c35fc7`.
2. **Status label is process-local.** `cmd_status()` reads `_evict.holding` in
   the status process, never the daemon's state, so it can never print
   `holding`. Same card.
3. **Design question for Forge (not a code change by the orchestrator):** the
   stop path (`released hold on shutdown`, and `ExecStopPost=release-hold`)
   releases the hold even when a >GTT model is still resident, which recreates
   the D3cold eviction hazard the guard exists to prevent. The stop/crash tests
   in TESTING.md §6 steps 11–12 were therefore **not run with the model
   resident**. Decide whether stop should keep `control=on` while VRAM is above
   the margin (and log it), or whether the operator contract is "unload before
   stopping the daemon".

## Also measured: host PCIe link

Kernel enumeration at boot: `c5:00.0 ... limited by 16.0 GT/s PCIe x4 link at
0000:00:02.5`, i.e. the OCuLink host link trained at Gen4 x4 on the cold boot
(same on the three previous boots today). Every active-state sample afterwards
(17:20 during the model load, 17:21 during generation) read
`current_link_speed = 8.0 GT/s x4` on the upstream port `c5:00.0`, while the
card's own `pp_dpm_pcie` table only lists 2.5 and 16 GT/s levels. The link
therefore retrains to Gen3 after the first runtime-resume, exactly as the Codex
handoff recorded this afternoon; the cold shutdown restores Gen4 only until the
first D3cold exit. The 2.5 GT/s reading while suspended is the idle link state,
not a degradation. Internal links `c6:00.0`/`c7:00.0` stay 32 GT/s x16.

## Verdict (orchestrator; Forge signs off on kanban `t_7fc5338a`)

The guard does what it was built for on the live card: it holds within one
poll of the model crossing the margin (twice), the card never sleeps with the
model resident, and it releases within a second of the unload with D3cold
7 s later, across two runs, the 5-cycle and storm batteries, and zero kernel
faults. The two observability defects (status label, runtime state directory)
do not affect the hold/release path and are on `t_35c35fc7`; the stop-path
design question is for Forge. Operator lessons recorded: Mission Center and
any 1-s `status` poller hold the card awake and must be off for D3cold timing.

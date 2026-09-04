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
| D3cold within ~20 s of release | **PENDING** | card still active with `control=auto` at 17:24; `renderD128` held by Mission Center (`missioncenter-magpie`), a Battle.net `gpu-process`, and LM Studio's GPU zygote. Mission Center is the documented wake/hold culprit (HANDOFF.md §5a). Re-measure with it closed. |
| `r9700-tunerd status` shows `evict_guard=holding` | **FAIL** | printed `evict_guard=idle vram_used=0.0G` at 17:20:42 while holding. Defect 2 on kanban `t_35c35fc7`. |
| dashboard shows the held-awake pill | **FAIL (expected)** | `/api/status` had `control=on`, `runtime_status=active` but `daemon_state`/`eviction_risk` null: the runtime state dir is gone (Defect 1, `t_35c35fc7`), and main's dashboard has no held-awake pill yet (R0.1 branch `wt/r01-ui-evict-pill`, card `t_4cf3bb0b`). |
| zero kernel faults | PASS | see above |
| `cycles` / `storm` after the run | PENDING | need an idle card (Mission Center closed) |

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

Verdict so far: the hold and release paths work on the live card within one
poll, with no faults. Acceptance is incomplete until the D3cold-after-release
timing and the cycles/storm battery are measured with Mission Center closed,
and the two defects are fixed and re-verified.

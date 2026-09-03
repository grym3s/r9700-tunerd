# Reboot acceptance — 2026-09-04

Build under test: the `patch3-resume-detect` daemon (commit 408999a, installed
2026-09-03 23:43 local), runner v2 from main. Machine rebooted by the owner at
00:44:48 local.

## Boot behaviour (journal)

- 00:45:16 udev oneshot (`r9700-tunerd-apply`) and the watcher started together;
  the oneshot re-applied the power cap (a cold boot resets it to the 300 W
  default), the watcher restored -25 mV; both paths idempotent, both logged once.
- 00:45:37 card suspended; 00:45:47 woke for the boot-time llama-server, offset
  restored in 0.35 s; 00:45:59 suspended; further wakes likewise.
- lactd inactive. No amdgpu lines beyond the known OD re-upload warnings.

## `reboot-check` (00:49:09)

| Criterion | OK |
|---|---|
| service enabled+active | Y |
| lactd inactive | Y |
| no tuner DRM handles | Y (only llama-server holds renderD128) |
| boot→D3cold | Y |
| wake→offset→cap→D3cold | Y |
| stop→D3cold | Y |
| no amdgpu failures (beyond OD) | Y |

## Full battery on the same build

| Test | Result | Evidence |
|---|---|---|
| `cycles --count 5` | PASS | restore latency 0.6–2.3 s, offset held, cap 210 W, D3cold in 5.8–6.5 s every cycle, 5 wakes / 0 cap writes in journal |
| `storm` | PASS | 10/10 real D3cold wakes, watcher PID stable, no traceback, no kernel errors |
| `config-typo` | PASS | one warning, PID stable, recovery, restore still works |
| `sigterm` | PASS | restart while active: old PID gone, new PID restored offset |

## Also measured

- A Halo (Strix Halo llama-server, Vulkan, holds an fd on renderD128) inference
  request does **not** wake the R9700: the card stayed `suspended/D3cold` and
  `runtime_suspended_time` kept increasing through the request.
- Cold boot resets `power1_cap` to the 300 W default; runtime suspend does not.

Verdict: Priority 2 (reboot acceptance) accepted on this build.

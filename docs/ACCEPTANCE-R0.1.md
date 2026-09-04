# Release 0.1 — hardware acceptance, 2026-09-04

Build under test: main `f135208` = the five Forge-approved R0.1 branches
(dashboard-safety, app-lifecycle, ui-evict-pill, atomic-tuning, doctor; audit
`t_546da953`) merged on top of the state-dir fix `3b286f2`; the two hand-resolved
merge conflicts were reviewed and approved by Forge on `t_b0dc5ae8`. 201 unit
tests. Run by the Claude Code orchestrator with the owner present; evidence for
kanban `t_14f696cd`. All times local (WITA). Mission Center was closed; Halo's
llama-server (iGPU) running; no model on the R9700.

## Step 1 — install and restart (17:45)

`sudo -n install.sh && sudo -n systemctl restart r9700-tunerd.service`.
Installed binary byte-identical to the repo; watcher `enabled`/`active`
(unchanged); `status` after restart: `runtime_status=suspended`,
`power_state=D3cold`, `evict_guard=idle vram_used=unknown gtt_total=16.6G`.
**PASS**.

## Steps 5, 6, 7 — asleep path (17:45:38, card D3cold throughout)

`runtime_suspended_time` before 966575 ms, after 967065 ms (kept counting; the
watcher journal shows no wake between 17:45:08 and 17:46:03).

| Command | Result |
|---|---|
| `status --json` | `schema_version=1`, `runtime_status=suspended`, `live=null`, `ranges.source=cached` (age 538 s, cap 210–330 W, vo −200..0 mV), `tuned={offset_mv:-50,cap_w:210}`, `evict_guard={enabled:true,holding:false,vram_used:null,gtt_total:16628045824,margin:0.9}` |
| `set-tuning --offset-mv -50 --cap-w 210` (asleep) | rc 0, validated against cached ranges |
| `set-tuning --offset-mv -250 --cap-w 210` | rc 1, `-250 mV outside cached range -200..0 mV`, config unchanged |
| `set-tuning --offset-mv -50 --cap-w 500` | rc 1, `500 W outside cached range 210..330 W`, config unchanged (`POWER_LIMIT_W=210`, `VOLTAGE_OFFSET_MV=-50`) |
| `doctor` | rc 0; version repo=installed=0.1.0 match=True; identity match=True (1002/7551/1043/0626); `runtime_pm: control=auto runtime_status=suspended power_state=D3cold`; capabilities inventory by existence only; unit active+enabled; sudoers rule present; `state=SUSPENDED` |
| `doctor --json` | 12 top-level keys; `evict_guard` five-key contract identical to `status --json` |
| `doctor --bundle-preview` | `doctor.json 1360 B`, `r9700-tunerd.conf 782 B`, `journal.txt 17650 B` |
| `doctor --bundle <path>` | 2647 B tarball, three files; grep for 32-hex tokens and `/home/grymes`: none; journal is the tuner's own 200 lines only; conf header intact |

**PASS** for all three steps; the critical one (`doctor` asleep) left the card
asleep.

## Steps 2, 5, 6 — active path with the dashboard attached (17:46)

`r9700-ui.service` running; an SSE client (`curl -N /api/events?t=<token>`)
connected for the whole sequence (27 events received, still connected at the
end). A test process held `/dev/dri/renderD128` open 17:46:01–17:46:42.

| Time | Event |
|---|---|
| 17:46:03 | watcher: `runtime active`, offset restored −50 mV, cap already 210 W |
| 17:46:05 | `set-tuning --offset-mv -50 --cap-w 210` while active: rc 0 (live ranges) |
| 17:46:05 | `set-tuning --offset-mv -250 --cap-w 210`: rc 1, `-250 mV below live min -200 mV` |
| 17:46:06 | `status`: cap 210 W (min 210 / default 300 / max 330), offset −50 mV (range −200..0); `status --json`: `live={cap_w:210, offset_mv:-50, ...}`, `ranges.source=live` |
| 17:46:11 | card suspended while the idle fd stayed open (an open node alone does not hold it) |
| 17:46:43 | node closed → wake (runner pattern) |
| 17:46:49 | `runtime_status=suspended`, `power_state=D3cold` **7.2 s** after the last wake, SSE client still attached |

**Step 2 (dashboard hazard) PASS**: D3cold in 7.2 s with a live SSE client.
Steps 5/6 active variants PASS.

## Step 9 — cycles and storm on the installed build (17:47–17:49)

| Test | Result |
|---|---|
| `cycles --count 5` | PASS: restore latency 0.4–2.4 s, −50 mV held, cap 210 W, D3cold 6.0–6.5 s each, 5 journal wakes, 0 cap writes |
| `storm` | PASS: 10/10 wakes detected, PID stable, no traceback, 0 kernel errors, D3cold 7.8 s |

## Steps 3, 4, 8 — need the owner

| Step | Status |
|---|---|
| 3 native app five-minute soak (launch from the menu, ≤ 1 wake) | **PENDING** — owner launches the app |
| 4 app lifecycle (SIGKILL the app owning a spawned server → server exits, port closes; restart `r9700-ui.service` under an attached window → reload on token change, no 403s) | **PENDING** — after step 3 |
| 8 `reboot-check` | **PENDING** — owner approval for the reboot (last reboot-check PASS 7/7 at 17:17 on `ad27bcc`, before the R0.1 merges) |

Verdict so far: every step the orchestrator could run passed on the real card;
the two release blockers named on the card (step 2 dashboard hazard, step 7
doctor asleep) both PASS. Sign-off waits on steps 3, 4 and 8.

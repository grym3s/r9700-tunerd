# r9700-tunerd — handoff, 2026-09-04 afternoon/evening

Supplements `HANDOFF.md` (morning: priorities 1–4, UI, matrix) and
`CODEX-HANDOFF-2026-09-04.md` (the Codex session's stop state). Read those
for the measured hardware history; this file is the current state and the
next steps. Written by the Claude Code orchestrator at ~17:10, just before
the owner's cold shutdown.

## 1. State right now

| Item | State |
|---|---|
| `main` | `ad27bcc`, clean, **98 tests pass**, compiles |
| Installed daemon | == main (`ad27bcc`), **eviction guard live**, unit has `ExecStopPost=r9700-tunerd release-hold`, restart clean |
| Daemon status | `evict_guard=idle vram_used=0.0G gtt_total=16.6G margin=0.90` (16.6 G = 15.5 GiB, i.e. RAM/2 at the 96 GB carve-out) |
| Config | `-50 mV / 210 W` (adopted at 09:50 after the matrix), `EVICT_GUARD=1`, `EVICT_GUARD_MARGIN=0.90` |
| Ray's model | **not loaded** (must stay unloaded until the guard acceptance below) |
| Halo's server | running, iGPU, `-c 131072 -ctk q8_0 -ctv q8_0 --reasoning-budget 8192`, pinned to the iGPU by `MESA_VK_DEVICE_SELECT=1002:1586!` |
| BIOS carve-out | 96 GB (owner chose to keep it; the guard is the mitigation) |
| USB Wi-Fi | **fixed**: USB 2.0 mode + other controller; zero faults since 15:33 |
| License | MIT (`LICENSE`, commit `5cd8571`) |
| GitHub | `origin` = github.com/grym3s/r9700-tunerd, **nothing pushed**; publish only from a sanitized history |

## 2. What was done since the morning handoff (who did it)

1. **Root causes found, not guessed** (orchestrator, kernel/journal evidence):
   - Afternoon freezes/reboots = BIOS carve-out 64→96 GB at 12:39 → 32 GB
     system RAM → amdgpu GTT 15.5 GB < Ray's 17.7 GB VRAM → D3cold
     evict/resume cannot complete (boot -2 froze in `rpm_resume`), plus an
     OOM storm (boot -1). Zero amdgpu faults in any boot. `HANDOFF.md §5d`.
   - 13-s wake loops = **Mission Center** polling the R9700 (held the card
     awake / woke it; it also launched 11 s before the 13:44 freeze). The
     tuner app and the Omarchy brightness panel were cleared by direct
     tests. `HANDOFF.md §5a`.
   - USB Wi-Fi resets = the rtw88 driver's USB 3 mode on the RTL8822BU, not
     the GPU. Fix: `switch_usb_mode=0`, `disable_lps_deep=1`, module
     reload, replug into the other controller → 480 Mb/s, faster, zero
     faults for 90+ min. `HANDOFF.md §5c`.
2. **Eviction guard** (owner chose Option B, "automate it"): daemon polls
   per-process DRM VRAM accounting in `/proc/*/fdinfo` (no hardware touch),
   reads `mem_info_gtt_total` once when the card is already active, holds
   the card awake (`power/control=on`) while VRAM > 0.9×GTT, releases at
   0.8×margin, adopts an existing hold on restart, releases on stop and via
   `ExecStopPost`, new `release-hold` subcommand, `status` line, docs.
   Built on the Hermes board as four child cards (Sonnet a–c, Haiku d+e),
   **reviewed and approved by Forge** (`t_68c95661`), merged `ad27bcc`,
   installed. 29 new tests in `tests/test_evict_guard.py`.
3. **Release 0.1 branches built by Sonnet/Haiku, not yet integrated**:
   - `wt/r01-dashboard-safety` (Codex's `32aa430` cherry-picked onto
     main's status builder; 124 tests on that branch)
   - `wt/r01-app-lifecycle` (stdlib backend controller
     `tools/r9700_app_backend.py`, authenticated attach, child-death retry,
     PDEATHSIG race, escaped errors, http(s)-only nav; 128 tests)
   - `wt/r01-ui-evict-pill` (one pill logic: held-awake vs eviction-risk)
   - `wt/r01-atomic-tuning` (`set-tuning`, `status --json`, `reset` exit
     codes) — **in progress** at shutdown
   - `wt/r01-doctor` — queued
4. **Agent team changes** (all recorded in `AGENTS.md`):
   - Forge → `claude-opus-4-6` on the owner's Anthropic OAuth.
   - New profiles: **sonnet** (`claude-sonnet-5`, default builder), **haiku**
     (`claude-haiku-4-5-20251001`), **mercury** (`claude-opus-5`, project
     manager, `kanban.orchestrator_profile`).
   - Kanban: `max_in_progress_per_profile 1→3`, `max_in_progress 4→8`,
     `default_assignee sonnet`, decomposer moved off Ray's LM Studio to
     `anthropic/claude-opus-5`. Board `r9700-tunerd`, project
     `r9700-tunerd`, worktrees under `.worktrees/` (git-ignored).
   - Owner's standing rule saved to memory: **every agent task is a kanban
     card**; direct API calls are never used for project work again.
   - Halo's server got 128K context (q8 KV) and an 8192-token reasoning
     budget after a worker turn thought for 20,059 tokens.
5. **Native app** wrapper hardening from the morning is on main (token
   follow, no signal handlers, PDEATHSIG for a spawned server).

## 3. Mistakes to know about (so nobody repeats them)

- `git add -A` in the shared checkout committed a Codex session's
  uncommitted files under the orchestrator's name (`549be3f`, `5ca0071`).
  Documented in `HANDOFF.md §5d`; history not rewritten. **Use explicit
  paths when other agents share the checkout.**
- `pkill -f <pattern>` / `kill $(pgrep -f ...)` killed the orchestrator's
  own shell three times: any pattern that appears in the command line
  matches itself. Kill by unit name or by pid discovered in a *separate*
  command.
- The Hermes desktop app loads the profile roster at start; creating
  profiles while it runs left the UI unable to open chats until it was
  restarted (17:04, now sees all 8 profiles).
- One-shot CLI test prompts ("Reply with X OK") leave stray sessions in
  each profile's list; delete them from the UI if they bother you.
- The umbrella worktree `.worktrees/t_edfe740a` (Halo's 46-line partial)
  was removed by the orchestrator to free the shared branch. Codex's two
  worktrees (`~/src/r9700-tunerd-worktrees/{ray,halo}`) were **not** touched;
  their content has been re-implemented on the R0.1 branches.

## 4. Next steps, in order

### A. Immediately after the cold shutdown (orchestrator, owner present)
1. `sudo -n tests/hw/r9700-hwtest.py reboot-check` → boot → idle → D3cold,
   offset/cap restored on first wake. Also read the R9700 host link:
   `cat /sys/bus/pci/devices/0000:c5:00.0/current_link_speed` while the
   card is active (expect 16 GT/s x4 again after the cold cycle).
2. **Eviction-guard acceptance** (kanban `t_7fc5338a`, script is at the
   scratchpad `r9700/accept_guard.sh`, wiped by reboot — recreate from
   this description): stop Halo's server first (`systemctl --user stop
   llama-halo.service`; the 32 GB rule), confirm `lms ps` empty and
   `evict_guard=idle`; send one request to Ray's endpoint to JIT-load the
   model; expect within one poll: journal `evict-guard: holding`,
   `power/control=on`, status `evict_guard=holding`; dwell 30 s (card must
   stay active); `lms unload --all`; expect `evict-guard: released`,
   `control=auto`, `runtime_status=suspended` within ~20 s; zero kernel
   faults; dashboard shows the held-awake pill during the hold. Then
   cycles/storm hardware tests. Post evidence on `t_7fc5338a` and write
   `docs/ACCEPTANCE-evict-guard.md`. Restart Halo's server afterwards.
3. If any step fails: `sudo -n /usr/local/sbin/r9700-tunerd release-hold`,
   `lms unload --all`, and report; do not retry blindly.

### B. Release 0.1 integration (Hermes board)
4. Forge review card `t_546da953` audits all R0.1 branches; integration is
   the orchestrator's job (cherry-pick/merge onto main one branch at a
   time, full suite after each, never a wholesale merge of stale branches).
5. Hardware acceptance of the R0.1 stack (`t_14f696cd`): dashboard hazard
   test (card reaches D3cold within ~20 s with a client connected), app
   launch from the menu, `status --json` consumed by the UI, `doctor`
   PM-safe while asleep.
6. Then the roadmap: Release 0.2 (experiment runner v2, matrix UI, profile
   editor, cooled repeats at 210/250/300 W), 0.3 (PM-safe fan curve, owner
   request), 0.4 (installer, CI, public docs, **sanitized history, then
   push**), 0.5 (polish). Details in `CODEX-HANDOFF-2026-09-04.md`.

### C. Things only the owner can decide
- Whether Halo may be used as a builder again (currently benchmarking).
- Whether Ray may be used once the guard acceptance passes (it makes Ray
  safe by construction: the card never sleeps with the model resident).
- Carve-out: stay at 96 GB with the guard, or back to 64 GB.
- When to publish: after license (done), history sanitisation, secrets and
  path audit, and the R0.1 acceptance.

## 5. How to drive the team

```bash
hermes kanban --board r9700-tunerd list            # board
hermes kanban --board r9700-tunerd show <id>       # card, comments, events
hermes kanban --board r9700-tunerd log <id>        # worker transcript
hermes kanban --board r9700-tunerd create "<title>" --assignee sonnet \
  --workspace worktree:/home/grymes/src/r9700-tunerd --branch wt/<name> \
  --project r9700-tunerd --body "<self-contained brief>"
```
Assign construction to `sonnet`/`haiku`, reviews to `forge`, board
management to `mercury`. Builders must never: run sudo, touch the live
daemon, launch GTK, run `tests/hw/`, start LM Studio, or call
`http://127.0.0.1:1234`. Every card body should say so.

## 6. Watches that were running at shutdown (they do not survive it)
- Board watcher: status changes + 5-min digest.
- Kernel-log watch for dongle faults (`8822bu`, USB resets).
Re-arm both after boot if the session continues.

# Codex continuation handoff — 2026-09-04

This handoff records the state at the owner's `/stop` request after the afternoon
GPU/model-memory incidents. It supplements `HANDOFF.md`; it does not replace the
measured hardware history there.

## Owner direction at stop

- Stop all active agents and preserve their work.
- Do not use the local Hermes builders (Ray or Halo) for the next continuation.
- Use cheaper Codex agents for construction until the owner changes this direction.
- Add and validate useful features before publishing to GitHub.
- Do not push the current internal history as the public repository history.
- Do not run two local 27B model servers together on the current 32 GB system-RAM
  configuration.

All three Codex Luna subagents were interrupted. No agent remains running.

## Product objective

Turn `r9700-tunerd` from a machine-specific but well-tested tuner into a safe,
feature-rich public GitHub project without weakening its defining properties:

1. identify the exact ASUS R9700 by vendor/device/subsystem, never card number or
   fixed PCI address;
2. never open `/dev/dri` from the daemon, dashboard, native wrapper, or tools;
3. never wake the card merely to display status;
4. validate and read back every hardware mutation;
5. keep the local dashboard authenticated and bound to loopback;
6. preserve D3cold and firmware fallback behavior;
7. distinguish measured evidence from assumptions;
8. require independent review and hardware gates for PM-critical changes.

An empty public repository already exists at
`https://github.com/grym3s/r9700-tunerd`. The local `origin` points there, but
nothing has been pushed. Current history contains internal handoffs, machine paths,
and personal metadata, so publish later from a sanitized history. The owner still
needs to choose the open-source license.

## Critical current machine facts

- BIOS UMA carve-out is 96 GB, leaving only about 32 GB visible system RAM. Earlier
  working boots had a 64 GB carve-out and about 64 GB system RAM.
- With 32 GB system RAM, the R9700 GTT staging limit is about 15.5 GB. Ray's Q4
  model occupies about 17.7 GB R9700 VRAM, so R9700 D3cold eviction/resume is unsafe
  while that model is resident.
- Halo's Q6 model uses the Strix Halo iGPU. Ray uses the R9700 when the R9700 is
  present and LM Studio selects it correctly.
- At 14:20, Halo was loaded first and LM Studio then loaded Ray. The kernel entered
  a global OOM storm and killed ChatGPT, Claude, Hermes, LM Studio, Ray's
  llama-server, and Halo's llama-server. The Electron SIGBUS/SIGTRAP core dumps were
  secondary fallout.
- Disabling LM Studio's “Keep Model in Memory” means no `mlock`; it does not unload
  GPU/shared-memory allocations. It is not a mutual-exclusion mechanism.
- Halo's service has `Restart=on-failure`, so an OOM kill automatically reloads the
  model five seconds later.
- The live Halo unit was rolled back to llama.cpp's original/default load mode. It
  retains the stable iGPU pin:
  `MESA_VK_DEVICE_SELECT=1002:1586!`, then `--device Vulkan0` because the filter
  exposes the iGPU as the sole Vulkan device. No `--load-mode` and no idle-sleep
  flag are present in the live unit.
- Do not restart/load either model server without first listing all model processes
  and checking actual system memory. During Codex-only work, neither local model is
  needed.
- The R9700 host link booted at 16 GT/s x4, then a BACO/runtime-resume cycle left it
  at 8 GT/s x4. The internal GPU/switch links remained 32 GT/s x16. A cold shutdown
  is the safe recovery; do not force a live `setpci` retrain.
- USB Wi-Fi still has real transport resets (`-71`/`-110`). Quickshell separately
  crashed in Qt icon loading. These are not tuner defects, though OOM pressure can
  make the desktop fallout worse.

## Baseline and repository state

Main repository: `/home/grymes/AI Projects/r9700-tunerd`

- Branch: `main`
- HEAD at stop: `348c66e`
- Baseline gate run immediately before agent dispatch:
  `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider tests/`
  -> `69 passed in 8.66s`
- `r9700-tunerd`, UI, benchmark, matrix, and app Python compilation passed.

Main is intentionally dirty at stop:

1. `handoff/tools/llama-halo.service` contains an existing user-owned documentation
   update: iGPU PCI-ID pin, `Vulkan0`, 131072 context, and q8 KV cache. The live
   service currently uses 65536 context and no q8 flags. Do not overwrite or assume
   the copy is deployed; reconcile explicitly later.
2. `tools/r9700-ui.py` and `ui/index.html` contain partial warning-only eviction-risk
   UI edits left by the interrupted eviction agent. They replace the misleading
   `ACTIVE_HELD` wording from commit `d257764` with `EVICTION RISK / unsafe to
   suspend`. These changes are uncommitted and untested as a combined feature.

Preserve all three modifications. Inspect and attribute them before committing.

## Stopped Codex-agent work

### 1. Dashboard safety hardening — committed

- Worktree: `/home/grymes/AI Projects/r9700-tunerd/.worktrees/ray`
- Branch: `codex/ray-ui-hardening`
- Commit: `32aa430 ui: harden dashboard safety and status recovery`
- Files: `tools/r9700-ui.py`, `tests/test_ui_server.py`
- Commit size: 54 insertions, 11 deletions.
- Intended contract: authenticate every API route, validate complete mutations
  before subprocesses, reject bool-as-int and unknown fields, serialize mutations,
  refresh config state, bound benchmark input, fail closed on incomplete benchmark
  evidence, choose hwmon numerically with stale-cache recovery, and refuse ambiguous
  PCI identity.
- This branch began six commits behind current main. Current main also changed the
  UI status builder in `d257764`; do not merge the branch wholesale. Review the
  commit and cherry-pick/reconcile it on a fresh integration branch.
- The agent committed before `/stop`, but its final report was interrupted. Re-run
  targeted and full tests independently; do not rely on unrecorded agent results.

### 2. Native app lifecycle — uncommitted partial work

- Worktree: `/home/grymes/AI Projects/r9700-tunerd/.worktrees/halo`
- Branch: `codex/halo-app-lifecycle`
- Base/HEAD: `c1bbf27`
- Modified: `tools/r9700-app.py`
- New untracked: `tools/r9700_app_backend.py`, `tests/test_app_backend.py`
- Partial app edits include stdlib backend integration, authenticated dashboard
  identity checks, child monitoring/retry, PDEATHSIG parent-race checking,
  idempotent cleanup hooks, HTML escaping, and HTTP(S)-only external navigation.
- The worktree was interrupted mid-implementation. Read every new file and inspect
  the complete diff before continuing. Run its focused tests and compilation only
  after completing the controller/UI integration. Do not launch GTK or hardware as
  part of the builder gate.

### 3. Eviction-risk warning — uncommitted partial work

- Worktree: `/home/grymes/AI Projects/r9700-tunerd/.worktrees/t_edfe740a`
- Branch: `wt/evict-guard`
- Base/HEAD: `3101fd6`
- Modified: `r9700-tunerd` (46 insertions, 1 deletion).
- Main also has the two uncommitted UI files described above.
- The partial daemon code reads `mem_info_vram_used` and `mem_info_gtt_total` only
  after the active/runtime-PM guard inside `handle_wake`, logs when used VRAM exceeds
  GTT, persists `eviction_risk`, `vram_used`, and `gtt_total`, and reports cached
  state from `status` without live sensor reads.
- It has no tests yet and was interrupted before review/commit.
- Important contract resolution: `handoff/agent-work/task40-evict-guard-spec.md`
  proposes automatically writing `power/control=on` and later `auto`. The newer
  authoritative `HANDOFF.md` section 5d says warning/surfacing only and explicitly
  forbids automatic runtime-PM changes. The newer warning-only rule wins. Do not
  implement auto-hold, adoption, release-hold, or `ExecStopPost` without a new owner
  decision.
- Complete this as a small warning-only feature with tests proving that the new
  `mem_info_*` files are never read while suspended or from status/poll loops.

## Intended feature program

The detailed draft is on `codex/ray-ui-hardening` at
`handoff/PUBLIC_FEATURE_ROADMAP.md`. The continuation should bring a reconciled copy
onto main after review. Implementation order:

### Release 0.1 — trustworthy foundation

1. **Dashboard safety and correctness**
   - integrate/review commit `32aa430`;
   - token-gate every API route;
   - whole-request validation before side effects;
   - mutation lock and 409 on contention;
   - safe benchmark bounds and labels;
   - strict evidence semantics;
   - stale hwmon recovery and ambiguous-GPU refusal.

2. **Native app lifecycle**
   - finish the stdlib-only backend controller and tests;
   - authenticated attach, not port-open trust;
   - stale token/wrong listener refusal;
   - child death detection and one-child Retry;
   - bounded owned-child cleanup;
   - PDEATHSIG race closure;
   - escaped errors and scheme-restricted navigation.

3. **Warning-only VRAM/GTT eviction risk**
   - sample only during an already-active wake;
   - cache exact bytes in daemon state;
   - show a prominent “unsafe to suspend” warning;
   - never claim the card was held awake;
   - never change runtime PM automatically;
   - unit-test active/suspended/status paths.

4. **Atomic tuning and stable machine output**
   - add `set-tuning --offset-mv N --cap-w W`;
   - validate both against one range snapshot before mutation;
   - atomically replace both config values;
   - apply once and restore the prior config on failure;
   - add versioned `status --json`;
   - make `reset` nonzero if any reset step fails;
   - migrate UI and matrix to the atomic command.

5. **Public diagnostics**
   - `doctor` and `doctor --json` for platform, kernel, Python, systemd, amdgpu,
     exact identity, runtime PM, safe capability inventory, units, permissions, and
     installed/repo versions;
   - a previewable redacted support bundle;
   - no tokens, serials, unrelated logs, personal paths, or automatic upload;
   - diagnostics remain PM-safe while asleep.

### Release 0.2 — reproducible tuning and profiles

1. Experiment runner v2: seeded randomized ordering, repeats, resumable run IDs,
   cool-down after observed D3cold, provenance, strict failure gates, original-point
   restoration on success/failure/interruption, and visible incomplete rows.
2. Matrix results UI: authenticated normalized API, legacy CSV support, grouped
   cap/offset results, medians/spread, stability/kernel/D3cold evidence, and best
   badges only for complete PASS groups.
3. Evidence-backed profile editor: immutable built-ins, atomic 0600 user storage,
   create/clone/rename/delete/import/export, compatibility validation, explicit
   Apply, and measured badges only from complete evidence.
4. Benchmark ownership: process-group ownership, cancel, timeout, server shutdown
   cleanup, clear exit state, configured-target precondition, and no arbitrary shell.
5. Operator hardware gate: cooled repeats at 210/250/300 W for -25/-50 mV, a longer
   -50 mV soak, and a second workload before Balanced/Performance promotion.

### Release 0.3 — PM-safe custom fan curve

1. Read-only active-only capability/status probe.
2. Pure curve validator and simulator with supported anchors, increasing
   temperatures, nondecreasing PWM, measured minimums, critical margin, hysteresis,
   and failure injection.
3. Independent threat-model review proving firmware fallback on stop, exception,
   malformed config, rebind, device loss, and forced death.
4. Reviewed backend/UI with explicit Apply and Firmware Auto, requested/effective
   state, fallback reason, and controller health.
5. Owner-authorized hardware acceptance only. No fan write before that gate.

### Release 0.4 — GitHub-ready distribution

1. Safe installer/uninstaller with dry-run, preflight, exact hardware check,
   manifest backups, verified install, idempotent upgrade, rollback, and separate
   user-app installation.
2. Guided first-run `init-config`/`doctor`, ambiguity refusal, explicit config
   creation, and diagnostics-only mode for unsupported devices.
3. CI across supported Python versions, compilation, shell checks, fake-sysfs
   install smoke tests, semantic version output, release checklist, checksums, and
   compatibility matrix.
4. Public docs: README reconciliation, CONTRIBUTING, SECURITY, SUPPORT, changelog,
   issue templates, privacy/support-bundle documentation, and chosen license.
5. Create a sanitized public history and only then push to the already-created
   GitHub repository.

### Release 0.5 — polished daily use

- single-instance native app that focuses the existing window;
- optional Wayland-compatible tray only after dependency review;
- rate-limited local notifications for failures/recovery/completed experiments;
- keyboard/focus/screen-reader/reduced-motion/high-contrast improvements;
- local audit/history, annotations, comparisons, export, and restore points;
- capability-derived firmware/kernel warnings, never version guesses.

Later research: clock tuning, explicit multi-GPU support, versioned workload
plugins, and packaging beyond Arch/Omarchy. None should precede the safety and
distribution gates above.

## Recommended continuation sequence

1. Confirm no Codex or local agents are running and no Ray model is loaded.
2. Create a fresh `codex/integration-r01` branch/worktree from current main. Do not
   integrate directly into the dirty main checkout.
3. Copy/reconcile the three intentional main diffs into that integration branch,
   keeping the service-copy change separate from product code.
4. Independently review and integrate dashboard commit `32aa430`; run its focused
   tests, then the full suite.
5. Finish the warning-only eviction feature in its worktree, add tests, review the
   diff for suspended-path reads, then cherry-pick/reconcile it.
6. Finish the app lifecycle work in its worktree, run stdlib-only tests and compile,
   then integrate it.
7. Run the complete test/compile gate. Do not perform live PM tests, benchmark,
   reboot, BIOS, fan, or root actions without the relevant owner gate.
8. Dispatch atomic tuning and diagnostics as separate bounded tasks to low-cost
   Codex agents. Use isolated worktrees and never let agents edit main.
9. Reconcile stale documentation only after behavior is settled.
10. Keep GitHub empty until release hygiene, secrets/path audit, license choice, and
    sanitized-history review are complete.

## Agent policy for the next continuation

- User requested **NO local agents**. Do not call Hermes profiles, LM Studio, Halo,
  Ray, Qwen, Ollama, vLLM, or direct local model endpoints.
- If delegating, use lower-cost Codex agents (for example `gpt-5.6-luna` at medium
  reasoning) with one isolated worktree and one bounded contract each.
- Builders do not validate themselves. The primary Codex session reviews diffs and
  reruns tests before integration.
- Never run multiple agents against the same checkout.
- Never merge or push from a subagent.

## Final cautions

- The active external goal was usage-limited when this continuation began; its
  objective remains the feature-first public-release program. Do not mark it
  complete merely because agents were stopped.
- The saved task40 auto-hold design is obsolete unless the owner explicitly revives
  it.
- “Keep Model in Memory = off” is not enough to make concurrent 27B local models
  safe.
- Do not erase interrupted worktrees or reset dirty files. Everything listed above
  belongs to the owner until reviewed.

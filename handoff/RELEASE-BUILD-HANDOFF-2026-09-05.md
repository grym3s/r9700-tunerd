# Release/build continuation handoff — 2026-09-05

Read `docs/RELEASE_PLAN.md` for the durable feature roadmap and build method. This
file records the exact repository state used to write it.

## Current direction

- Features and validation come before GitHub publication.
- Do not use local model agents until the owner changes that instruction. If work is
  delegated, use lower-cost Codex agents in isolated worktrees.
- Do not start, stop, reload, or benchmark local model servers as part of software
  construction.
- Do not push the internal git history. The existing public repository remains a
  destination for a later sanitized history.

## Main and Release 0.1

- Repository: `/home/grymes/AI Projects/r9700-tunerd`
- Main HEAD audited: `c05b7bf`
- Working tree was clean at the start of this continuation.
- Release 0.1 implementation is merged: dashboard safety, app lifecycle, eviction
  guard, atomic tuning/status JSON, doctor, and redacted support bundle.
- `docs/ACCEPTANCE-R0.1.md` records PASS for install/restart, asleep and active
  command paths, dashboard hazard, cycles, and storm.
- Remaining 0.1 acceptance is explicitly pending:
  1. native-app five-minute soak launched from the menu;
  2. spawned-child death/Retry and attached-service token-rotation lifecycle test;
  3. `reboot-check` on the final 0.1 build with owner approval.
- Do not describe 0.1 as hardware accepted or released until those records exist.

## Release 0.2 branch state

The R0.2 implementation exists but is not on main. All test commands below were run
on 2026-09-05 using main's `.venv`, with bytecode/cache disabled; compilation also
passed for the daemon/UI/app entry points.

| Feature | Branch/tip | Software evidence | State |
|---|---|---:|---|
| Profiles | `wt/r02-profiles` @ `4622821` | 219 passed in 33.56 s | implemented, unmerged |
| Fan controller + stale-PWM release fix | `wt/r02-fan-curve` @ `242e564` | 234 passed in 33.51 s | implemented, unmerged, hardware-gated |
| Combined profiles/fan/matrix UI | `wt/r02-ui` @ `bf77fdd` | 281 passed in 47.19 s | implemented, unmerged, hardware-gated |
| Documentation | main @ `c05b7bf` | inspected | merged ahead of code |

Branch ancestry matters:

- Profiles and fan branches independently start from `8a336f1`.
- UI merge commit `243ef6c` combines profiles and fan, then `bf77fdd` adds UI.
- Main has one documentation commit not in those code branches.
- Do not merge a stale branch wholesale into main. Use a clean integration worktree
  from current main, merge/cherry-pick profiles first, fan second (including
  `242e564`), then reconcile the UI commit. Resolve docs against actual integrated
  behavior.

## Immediate continuation sequence

1. Preserve `main` and create a clean `codex/r02-integration` worktree from its
   current HEAD.
2. Review `4622821` against the profile section of `docs/RELEASE_PLAN.md`; integrate
   and run targeted plus complete tests.
3. Review `25146ea` and `242e564` as PM/thermal-critical code. Specifically audit
   active-only reads, all firmware-auto exit paths, shared `ExecStopPost` behavior,
   malformed configuration, rebind/device loss, and interaction with eviction-guard
   release. Integrate only after independent review; rerun the full suite.
4. Reconcile `bf77fdd` after backend integration. Audit authentication and mutation
   locks for every new API, ensure selecting a profile or matrix row never applies
   settings automatically, and keep failed/incomplete rows visible.
5. Run the complete integration software gate and record exact counts.
6. Keep fan control disabled in the installed config until the owner authorizes the
   hardware acceptance sequence.
7. Finish the three pending R0.1 owner-gated checks either before R0.2 installation
   or repeat them on the R0.2 candidate; do not use old acceptance evidence for a
   different commit.
8. Perform R0.2 hardware acceptance from the exact integration commit, with rollback
   ready and kernel/D3cold evidence captured.

## Subsequent construction cards

After R0.2 integration, create separate bounded tasks in this order:

1. Experiment runner v2 and durable provenance.
2. Benchmark process ownership, cancellation, and timeouts.
3. Versioned custom-profile store and import/export.
4. Installer/uninstaller preflight and rollback.
5. First-run `init-config` and diagnostics-only mode.
6. CI/release automation and compatibility matrix.
7. Public documentation, issue templates, privacy audit, and sanitized history.
8. Daily-use accessibility, notifications, single instance, and audit history.
9. Broader hardware diagnostics and explicit multi-GPU architecture.

Each task contract must include exact files in scope, non-goals, failure behavior,
test commands, and an explicit ban on hardware/root/service/model actions unless it
is the owner-run acceptance task.

## Hardware and model safety carried forward

- The owner keeps the 96 GB UMA carve-out. The eviction guard is the accepted
  mitigation for R9700 VRAM exceeding stageable GTT.
- Never rely on “Keep Model in Memory = off” as an unload or capacity guarantee.
- Check actual model processes and memory before any model-server operation.
- Mission Center and other GPU monitors can defeat runtime PM.
- The R9700 host link falls from 16 GT/s x4 to 8 GT/s x4 after the first D3cold
  cycle. Future diagnostics may report this; never force a live PCIe retrain.
- USB Wi-Fi was stabilized by USB 2.0 mode and moving controllers; do not attribute
  that transport fault to the tuner without new evidence.

## Publication gate

The GitHub repository must remain unpopulated until all of these are true:

- chosen release scope has complete software and required hardware evidence;
- installer/uninstaller and first-run diagnostics pass from a staged tree;
- README, architecture, testing, support, security, changelog, and compatibility
  documentation match the code;
- tracked files and history pass a secrets/personal-path/machine-data audit;
- sanitized history is reviewed independently;
- version, tag, checksums, and CI all agree.

MIT is the recorded license choice. Do not replace it without an owner decision.

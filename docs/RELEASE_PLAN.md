# Release plan

This document defines what each release must deliver and how a feature moves from
an idea to accepted code. A feature is not released merely because it exists on a
branch or passes unit tests: power-management and fan-control features also require
recorded acceptance on supported hardware.

## Product principles

Every release preserves these invariants:

1. Discover supported GPUs by PCI vendor, device, subsystem vendor, and subsystem
   device. Never depend on `cardN`, `renderDN`, or a fixed PCI address.
2. The daemon, dashboard, native wrapper, and tools never open `/dev/dri`.
3. Suspended-device paths read only the sysfs files proven not to wake the GPU.
4. Hardware changes are range-validated, applied atomically where possible, read
   back, logged, and recoverable.
5. Runtime PM remains observable: a dashboard, benchmark, or controller must not
   silently prevent D3cold.
6. The dashboard remains authenticated, loopback-only, and telemetry-free by
   default.
7. Experimental settings are labelled measured only when their complete evidence
   is retained.
8. A builder does not approve its own work. Power-management and thermal changes
   require independent review and an explicit hardware gate.

## Status vocabulary

- **Designed**: contract and safety boundaries exist.
- **Implemented**: code and tests exist on a branch.
- **Integrated**: reviewed code is on `main` and the full software gate passes.
- **Hardware accepted**: the documented live-hardware procedure passes on the exact
  build under test.
- **Released**: integrated, accepted, documented, versioned, and published from the
  sanitized public history.

## 0.1 — safety foundation

The 0.1 code is integrated on `main`:

- runtime-PM-aware tuning and wake restoration;
- VRAM/GTT eviction guard with hold, hysteresis, restart adoption, and forced-exit
  release;
- authenticated and serialized dashboard mutations;
- authenticated native-app attachment and owned-child lifecycle recovery;
- atomic `set-tuning`, versioned `status --json`, and meaningful reset exit codes;
- `doctor`, JSON diagnostics, and a redacted support bundle.

Release gate:

- complete the native-app five-minute wake soak;
- prove owned-child death, Retry, and attached-service token rotation on the live
  desktop;
- run `reboot-check` on the final 0.1 build with owner approval;
- update `docs/ACCEPTANCE-R0.1.md` with exact commit and observations;
- reconcile architecture/UI/testing documents with the accepted implementation.

## 0.2 — profiles, fan control, and results UI

### Evidence-backed profiles

- Ship immutable Efficiency, Balanced, and Performance profiles only from retained
  matrix evidence.
- `set-profile` uses the same atomic validation/application path as `set-tuning`.
- Explicit custom tuning changes the active profile to `CUSTOM`.
- `list-profiles` is read-only and safe while the GPU sleeps.

### PM-safe fan curve

- Keep fan control disabled by default.
- Read sensors and write the curve only while the GPU is already active.
- Validate anchor count, temperature ordering, PWM monotonicity, live limits, and
  hysteresis before applying.
- Return to firmware-auto on idle, normal stop, error, device loss, restart, and
  forced process death. `ExecStopPost` is the final recovery layer.
- Surface requested mode, effective mode, fallback reason, and controller health.

### Dashboard expansion

- Provide profile selection without applying merely by selection.
- Display normalized matrix results, including incomplete and failed runs.
- Mark a best result only among complete PASS groups.
- Provide a fan-curve editor with Preview, explicit Apply, and Firmware Auto.
- Keep every new API authenticated, bounded, validated, and serialized.

Release gate:

- integrate profiles, fan, then UI on an integration branch with the full suite
  after each step;
- independent PM/thermal threat-model review;
- owner-authorized fan hardware tests for normal stop, SIGKILL, malformed config,
  rebind/device loss where safely reproducible, and reboot recovery;
- dashboard hazard test with the fan feature enabled;
- cooled profile verification before Balanced or Performance is labelled measured.

## 0.3 — reproducible experiments and custom profiles

### Experiment runner v2

- Seeded randomized point order and a requested repeat count.
- Resumable run IDs and append-only step records.
- Proven D3cold cool-down between points.
- Provenance: tuner commit/version, kernel, firmware/card identity, workload model,
  endpoint class, prompt-set version, seed, and timestamps.
- Capture the kernel cursor before Apply so apply-time faults are included.
- Treat missing kernel evidence, D3cold timeout, setting drift, or incomplete output
  as failure in rigorous mode.
- Restore the original safe point on success, failure, interruption, and timeout
  unless the operator explicitly chooses to keep a tested point.

### Benchmark ownership

- Launch workloads in an owned process group.
- Support cancellation, maximum runtime, server-shutdown cleanup, exit status, and
  a durable final state.
- Refuse a launch when requested sliders differ from the configured target.
- Accept local endpoint/model/prompt configuration without arbitrary shell command
  execution.

### Custom profile store

- Store versioned custom profiles atomically with mode `0600`.
- Create, clone, rename, update, delete, import, and export custom profiles.
- Preserve corrupt storage for recovery and ignore it safely.
- Validate imported profiles against identity, ranges, and schema version.
- Allow a complete PASS matrix row to seed a profile, but never auto-apply it.

Release gate:

- interruption and timeout fault-injection tests;
- reproducibility test using the same seed and fixture data;
- real cooled repeats at approved cap/offset combinations;
- a second workload class before promoting general-purpose recommendations.

## 0.4 — public distribution

### Installer and first run

- Add `--dry-run`, dependency and exact-hardware preflight, manifest-based backups,
  verified installation, idempotent upgrade, uninstall, and rollback.
- Never overwrite an existing configuration or silently change service enablement.
- Install the root daemon and user application as separate explicit operations.
- Add `init-config` with ambiguity refusal and diagnostics-only mode for unsupported
  hardware.

### CI and release engineering

- Test supported Python versions using fake sysfs only.
- Compile Python entry points, check shell syntax/style, and smoke-test the staged
  install layout.
- Add semantic version output, changelog, signed/tagged release notes, checksums,
  compatibility matrix, and release checklist.
- Add `CONTRIBUTING.md`, `SECURITY.md`, `SUPPORT.md`, issue forms, and privacy/support
  bundle documentation.

### Publication

- Scan tracked files and history for tokens, personal paths, machine identifiers,
  email addresses, raw journals, and private benchmark metadata.
- Build a sanitized public history rather than pushing the internal development
  history.
- Preview the final tree, run CI locally, then push and protect the default branch.
- Publish no automatic telemetry or uploads. Users choose whether to attach a
  support bundle.

## 0.5 — polished daily use

- Single-instance native app; a second launch focuses the existing window.
- Local, configurable, rate-limited notifications for apply failure, watcher loss,
  recovery, and completed experiments.
- Keyboard navigation, visible focus, screen-reader status, reduced motion, high
  contrast, responsive tables, and accessible charts.
- Local audit history with annotations, comparisons, export, and explicit restore
  points.
- Capability-derived kernel/firmware warnings, including observed PCIe link
  downtraining; report and recommend, never force a live retrain.
- Optional Wayland-compatible tray integration only after dependency and lifecycle
  review.

## 0.6 — broader hardware and extensibility

- Diagnostics-only support for other R9700 subsystems before permitting writes.
- Explicit multi-GPU selection with per-device config, state, locking, and results.
- Versioned workload plug-in protocol with capability declarations and resource
  ownership.
- Packaging for additional systemd distributions after CI and real-hardware
  validation.
- Clock tuning only after voltage, cap, fan, recovery, and evidence workflows are
  mature; keep it experimental and disabled by default.

## How features are built

1. **Write the contract first.** State the user outcome, files in scope, invariants,
   non-goals, failure behavior, tests, and whether hardware/root/reboot actions are
   forbidden or owner-gated.
2. **Use an isolated worktree.** One task, one branch, one builder. Never let an
   agent edit the shared `main` checkout or merge/push itself.
3. **Implement pure logic first.** Put parsing, validation, state machines, and
   formatting behind dependency-injected or fake-filesystem boundaries.
4. **Prove negative behavior.** Tests must show that invalid input causes zero
   subprocesses/writes, suspended paths avoid unsafe reads, unrelated processes are
   never signalled, and cleanup is idempotent.
5. **Run the software gate.** Targeted pytest, complete pytest, and `py_compile` for
   every changed entry point. Record exact commands and counts.
6. **Review independently.** Audit identity, PM safety, authentication, atomicity,
   rollback, concurrency, privacy, and documentation claims against the diff.
7. **Integrate incrementally.** Merge/cherry-pick one reviewed feature onto a clean
   integration branch, rerunning the full gate after every feature.
8. **Perform owner-gated acceptance.** Install the exact integration commit; record
   installed/repo byte or version agreement, live observations, kernel gate, D3cold
   recovery, and rollback outcome.
9. **Update documentation after behavior settles.** Architecture describes current
   code, testing gives executable procedures, and acceptance records name the exact
   build.
10. **Release from the sanitized tree.** Version, changelog, CI, privacy scan,
    checksums, tag, publish, and only then announce support.

## Required test layers

| Layer | Runs where | Must prove |
|---|---|---|
| Pure unit | every branch | parsing, validation, state transitions, errors |
| Fake sysfs | every hardware-facing branch | identity, safe paths, reads/writes, rollback |
| API/lifecycle | dashboard/app branches | auth, bounds, concurrency, ownership, cleanup |
| Integration | clean integration branch | complete suite and entry-point compilation |
| Hardware | owner-gated supported machine | D3cold, wake restore, guard/fan fallback, kernel cleanliness |
| Release | sanitized public tree | install layout, docs, version, privacy, reproducible artifact |

A release advances only when every required layer has evidence. A skipped or
unavailable layer is recorded as pending, never silently treated as a pass.

# r9700-tunerd public-release feature program (draft 2026-09-04)

This roadmap combines the owner requests in `handoff/HANDOFF.md` and
`docs/ROADMAP.md` with the work needed for a trustworthy public GitHub release.
The product remains local-only, runtime-PM-aware, sysfs-only, and specifically
supports the ASUS Radeon AI PRO R9700 until broader hardware is independently
validated.

## Non-negotiable release invariants

1. Discover by vendor/device/subsystem identity; never by `cardN`, `renderDN`, or
   a fixed PCI address.
2. Never open `/dev/dri` from the tuner, UI server, native wrapper, or tools.
3. While suspended, read only the PM-safe files already measured on this machine.
4. Every hardware write is range-validated from live or explicitly cached evidence,
   read back, bounded, logged, and recoverable.
5. No benchmark, fan controller, chart, or UI poll may prevent D3cold.
6. No remote listener, telemetry, cloud account, or dependency is enabled by
   default. The authenticated dashboard stays on `127.0.0.1`.
7. Experimental results are not labelled stable or measured without complete
   evidence: zero errors, exact setting stability, clean kernel gate, and D3cold
   recovery.
8. Builders do not approve their own work. PM-critical changes receive adversarial
   independent review and live hardware validation.

## Release 0.1 — trustworthy foundation

### R0-01 Dashboard safety and correctness — Ray W1

- Authenticate every API route.
- Validate complete mutation requests before running commands.
- Serialize mutations; bound benchmark input.
- Refresh displayed config after Apply.
- Fail closed on incomplete benchmark evidence.
- Correct duplicate PCI and stale hwmon handling.

### R0-02 Native app lifecycle — Halo W1

- Authenticated backend identity check, not port-open trust.
- Recover from child death; Retry performs attach-or-spawn.
- Close PDEATHSIG race; idempotent owned-child cleanup.
- Escape error HTML and restrict external schemes.
- Test lifecycle without GTK or hardware.

### R0-03 Atomic daemon tuning and stable machine output — Ray W2

- Add `set-tuning --offset-mv N --cap-w W`.
- Validate both values against one range snapshot before any config mutation.
- Replace both config values atomically; one apply cycle; restore prior config on
  apply failure and report recovery outcome.
- Add versioned `status --json` without weakening asleep-state read restrictions.
- Make `reset` return nonzero if either reset operation fails.
- Make UI and matrix use the atomic command.

### R0-04 Public diagnostics — Halo W2

- Add `doctor`/`doctor --json`: platform, kernel, Python, systemd, amdgpu, exact PCI
  identity, runtime-PM configuration, sysfs capability inventory, service state,
  file permissions, and installed/repo version.
- Add a redacted support bundle containing config, cached ranges, recent relevant
  journal excerpts, app self-check, and benchmark metadata—never API tokens,
  unrelated logs, serials, or private paths.
- Stay PM-safe when the card is asleep.

## Release 0.2 — reproducible tuning and useful profiles

### R1-01 Experiment runner v2 — Halo

- Seeded randomized ordering, requested repeat count per combination, resumable run
  IDs, cool-down after observed D3cold, and provenance (commit, kernel, model,
  endpoint class, prompt-set version, timestamp).
- D3cold timeout or unavailable kernel log is a hard failure in rigorous mode.
- Capture the kernel cursor before Apply so apply-time faults are included.
- Restore the original safe point on success, failure, interruption, and timeout
  unless the operator explicitly opts to keep a tested point.
- Record failed and incomplete steps rather than hiding them.

### R1-02 Matrix results view — Ray backend, Halo frontend

- Authenticated normalized matrix API with legacy-CSV compatibility.
- Group by cap/offset; show repeat count, median, spread, throughput, efficiency,
  temperatures, stability, kernel result, and D3cold recovery.
- Mark best only among complete PASS rows; failed/incomplete rows remain visible.
- “Load into sliders” never applies hardware settings automatically.

### R1-03 Evidence-backed profile editor — Ray backend, Halo frontend

- Built-in profiles plus atomic versioned user storage in
  `~/.config/r9700-tuner/profiles.json`, mode `0600`.
- Create, rename, update from sliders, clone, delete custom profiles, and versioned
  import/export with range/compatibility validation.
- Built-ins are immutable. Corrupt storage is preserved for recovery and ignored.
- A matrix PASS row can seed a custom profile; measured badges require the full
  evidence gate, not one matching file.
- Applying a profile remains explicit and uses atomic `set-tuning`.

### R1-04 Benchmark ownership — Halo

- Launch workloads in an owned process group.
- Add cancellation, maximum runtime, server-shutdown cleanup, exit status, and clear
  final state.
- Refuse benchmark launch when sliders differ from the active configured target.
- Make endpoint/model/prompt set configurable locally with safe defaults and no
  arbitrary shell execution.

### R1-05 Complete the measured profile set — operator gate

- Randomized cooled repeats at 210 W for -25/-50 mV.
- 250 W and 300 W passes for -25/-50 mV; reconcile the stale 230 W reference first.
- Longer -50 mV / 210 W real-workload soak and at least one second workload class.
- Promote Balanced/Performance only after accepted evidence.

## Release 0.3 — PM-safe custom fan curve

The live card exposes `gpu_od/fan_ctrl/{fan_curve,fan_minimum_pwm,
fan_target_temperature,acoustic_*}`. On 2026-09-04 it reported five curve anchors,
25..100 C, 30..100% PWM; `fan_zero_rpm_enable` read empty. The kernel interface says
writing a curve anchor implicitly selects manual mode, `c` commits, and `r` resets.
No write-capable feature lands until reset/fallback behavior is measured and reviewed.

### R2-01 Capability/status probe — Ray

- Add PM-safe `fan-status --json`; read fan files only when already active.
- Report sleeping/unsupported without waking, and include exact live ranges and
  interface version.
- Do not write anything.

### R2-02 Pure curve engine and simulator — Ray

- Validate exactly supported anchor count, strictly increasing temperatures,
  nondecreasing PWM, measured minimum PWM, upper range, and critical-temperature
  margin.
- Provide interpolation/hysteresis playback from recorded temperature traces.
- Inject all failures in unit tests; keep `FAN_CONTROL=0` default.

### R2-03 Threat-model review — Halo

- Prove the controller cannot keep an idle GPU awake.
- Invalid fan config must return firmware control; keeping last-good manual mode is
  unsafe.
- Require firmware fallback on normal stop, exception, startup, rebind/device loss,
  service stop, malformed config, and forced process death.
- Cover SIGKILL with systemd-managed recovery such as `ExecStopPost`, plus startup
  recovery; process handlers alone are insufficient.

### R2-04 Reviewed backend and UI — Ray build, Halo review

- Idempotent `set-fan-curve`, `fan-auto`, and readback/status telemetry.
- Enter manual only under the reviewed sustained-active policy; immediately release
  to firmware auto on idle transition without reading sensors while asleep.
- UI curve editor uses daemon CLI only, displays manual 30% floor and unsupported
  zero-RPM behavior, supports preview, explicit Apply, and “Firmware auto.”
- Expose requested/effective curve, current mode, fallback reason, and controller
  health.

### R2-05 Hardware acceptance — operator gate

- SIGTERM, SIGKILL, injected crash, bad config, service stop, device rebind, and
  reboot all return firmware control.
- Dashboard hazard test remains PASS: D3cold within 30 seconds after workload
  release with SSE and fan feature enabled.
- No unreviewed fan writes and no reboot without explicit owner approval.

## Release 0.4 — GitHub-ready distribution and support

### R3-01 Safe installer and uninstaller

- `--dry-run`, preflight, explicit supported-hardware check, distro/systemd/amdgpu
  requirements, manifest-based backups, verified install, idempotent upgrade,
  `--uninstall`, and rollback instructions.
- Never alter service enablement implicitly. Never overwrite an existing config.
- Package native-app user install separately from root daemon install.

### R3-02 First-run setup

- Guided `init-config`/`doctor` flow that lists exact matching devices, refuses
  ambiguity, validates runtime PM and available ranges, and creates a config only
  after explicit confirmation.
- Support “diagnostics only” mode on unsupported or partially supported hardware.

### R3-03 Repository hygiene and automation

- GitHub Actions unit tests across supported Python versions, syntax compilation,
  shell checks, and an install-layout smoke test using fake sysfs only.
- Add `CONTRIBUTING.md`, `SECURITY.md`, `SUPPORT.md`, changelog, issue/bug templates,
  architecture-currentness checks, and a release checklist.
- Add semantic version output, tagged release notes, checksums, and a compatibility
  matrix for kernel/firmware/card revisions.
- Owner must choose the open-source license before public release; do not infer one.

### R3-04 Support bundle and privacy

- One-command redacted diagnostics attached easily to GitHub issues.
- Preview every included file/field before export.
- No telemetry or automatic uploads; users upload deliberately.

## Release 0.5 — polished daily use

- Single-instance native app: second launch focuses the existing window without a
  second WebView/server or extra launch wake.
- Wayland-compatible tray/status integration only after dependency availability is
  established; do not use deprecated `Gtk.StatusIcon`.
- Desktop notifications for apply failure, lost watcher, recovery, and completed
  experiments, locally configurable and rate-limited.
- Accessible labels, keyboard navigation, focus states, reduced-motion mode,
  high-contrast palette, responsive tables, and screen-reader status updates.
- Local session history, comparison/export, annotations, and configuration audit
  trail with restore points.
- Firmware/driver/kernel compatibility warnings derived from observed capabilities,
  never guessed from version alone.

## Later research tracks

- Clock tuning only after voltage/cap/fan safety is mature.
- Multiple supported GPUs with explicit stable identity and per-device isolated
  state/config; never silently select one of several matches.
- Workload plug-ins with a versioned local protocol and resource ownership.
- Portable packaging for additional systemd Linux distributions after CI and real
  hardware validation. Keep Arch/Omarchy as the only supported target until then.

## Documentation reconciliation required

- `docs/TESTING.md` wrongly says reboot acceptance was not performed.
- `docs/ARCHITECTURE.md` still describes a pre-UI baseline.
- `docs/UI.md` still describes signal handlers and two-second updates; HEAD uses
  default signal disposition/PDEATHSIG and 0.5-second busy ticks.
- `docs/ROADMAP.md` says -50 mV adoption is pending although it is now adopted.
- Handoff Git counts and some native-app WIP wording predate later commits.
- Decide whether the old `status --json`/Unix-socket prerequisite was superseded;
  prefer the smallest stable local interface rather than adding IPC by inertia.

## Public-release decision gates for the owner

1. Choose repository name and open-source license.
2. Decide whether v0.x officially supports only the exact ASUS subsystem or also an
   opt-in diagnostics-only mode for other R9700 boards.
3. Approve any live fan write, reboot acceptance, and experimental cap/offset run.
4. Decide whether optional AppIndicator packaging is worth an added dependency.

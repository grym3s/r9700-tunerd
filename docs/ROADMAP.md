# Roadmap

Phases are gated: nothing moves to the next phase until the previous one is
validated on the live card.

1. **Core hardening** (DONE 2026-09-04, accepted on hardware) – config validation, restart-storm safety,
   unit hardening, unit tests without hardware, `status`/`reset` polish,
   optional `--dry-run`.
2. **Reboot acceptance** (DONE 2026-09-04, docs/ACCEPTANCE-2026-09-04.md) – boot → idle → D3cold; workload → active → -25 mV
   restored, cap 210 W; stop → D3cold; no tuner DRM handles. Requires user approval
   to reboot.
3. **Real-workload validation** (first measurement done at -25 mV / 210 W; harness v3) – the user's llama-server / Qwen workload:
   stability, VO, power, hotspot, clocks, tokens/s, tokens/s/W, D3cold recovery.
4. **Undervolt characterisation** (first pass DONE 2026-09-04 at 210 W, docs/MATRIX-2026-09-04.md:
   -25/-50/-75/-100 mV all PASS, the gain is entirely at -50 mV (+4.8 %), flat beyond;
   safe point left at -25 mV, -50 mV recommended pending owner adoption; next: caps 230/250 W,
   randomised repeats with cool-down, longer soak) – automated benchmark/results harness;
   -50/-75/-100 mV candidates; objective is best sustained throughput per watt with
   zero instability, not the largest offset. Then EFFICIENCY / BALANCED /
   PERFORMANCE profiles from measured data.
5. **GPU tuning UI** (v1 DONE 2026-09-04: tools/r9700-ui.py + ui/index.html; PROFILES delivered pending hardware acceptance; next: tray/launcher integration, profile editing, matrix results view)
   – a thin front end over the daemon: voltage-offset slider bounded by the live
   `OD_RANGE`, power-cap slider bounded by `power1_cap_min/max`, profile buttons,
   Apply / Benchmark / Restore-defaults, live state (D3cold / active / tuned,
   fan RPM or firmware mode). Prerequisites, in order: (a) a machine-readable
   `status --json` and a small local IPC (unix socket) so the UI never touches
   sysfs itself and never holds the card awake; (b) profiles from Phase 4 data.
   Not before Phases 2–4 are accepted.
6. **Custom fan curve** (delivered pending hardware acceptance; owner request 2026-09-04; see handoff/HANDOFF.md §10 item 6;
   DONE 2026-09-04, daemon side: `r9700-tunerd` card R0.2)
   – active-only controller with firmware fallback on every exit path, curve
   editor in the app writing through the daemon CLI. Must pass the dashboard
   hazard test with the controller enabled. Daemon side (validation,
   interpolation, hysteresis, `set-fan-curve`, `status --json` fan block,
   firmware-fallback on every exit path, unit tests) is implemented; the
   app-side curve editor UI is still open (tracked separately).
7. **Later** – clock tuning, multi-GPU support beyond the R9700.

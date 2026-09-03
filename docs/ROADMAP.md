# Roadmap

Phases are gated: nothing moves to the next phase until the previous one is
validated on the live card.

1. **Core hardening** (in progress) – config validation, restart-storm safety,
   unit hardening, unit tests without hardware, `status`/`reset` polish,
   optional `--dry-run`.
2. **Reboot acceptance** – boot → idle → D3cold; workload → active → -25 mV
   restored, cap 210 W; stop → D3cold; no tuner DRM handles. Requires user approval
   to reboot.
3. **Real-workload validation** – the user's llama-server / Qwen workload:
   stability, VO, power, hotspot, clocks, tokens/s, tokens/s/W, D3cold recovery.
4. **Undervolt characterisation** – automated benchmark/results harness;
   -50/-75/-100 mV candidates; objective is best sustained throughput per watt with
   zero instability, not the largest offset. Then EFFICIENCY / BALANCED /
   PERFORMANCE profiles from measured data.
5. **GPU tuning UI** (owner's direction: this becomes a general GPU tuning tool)
   – a thin front end over the daemon: voltage-offset slider bounded by the live
   `OD_RANGE`, power-cap slider bounded by `power1_cap_min/max`, profile buttons,
   Apply / Benchmark / Restore-defaults, live state (D3cold / active / tuned,
   fan RPM or firmware mode). Prerequisites, in order: (a) a machine-readable
   `status --json` and a small local IPC (unix socket) so the UI never touches
   sysfs itself and never holds the card awake; (b) profiles from Phase 4 data.
   Not before Phases 2–4 are accepted.
6. **Later** – clock tuning, hybrid active-load fan controller (only if proven
   runtime-PM-safe), multi-GPU support beyond the R9700.

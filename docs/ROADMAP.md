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
5. **Later** – clock tuning, profiles command, hybrid active-load fan controller
   (only if proven runtime-PM-safe), simple UI/tray.

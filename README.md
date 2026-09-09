# r9700-tunerd

Runtime-PM-aware tuner for the ASUS Radeon AI PRO R9700 32GB on Omarchy.

Phase A/B/C: stable DRM names + power cap + VDDGFX offset restored after every D3cold wake by the runtime_status watcher. See docs/ARCHITECTURE.md.
No DRM/render-node handles. No LACT on the control path. Active-only custom
fan curve with firmware fallback on every exit path (R0.2, `set-fan-curve`).

## Paths

- `/usr/local/sbin/r9700-tunerd`
- `/etc/r9700-tunerd.conf`
- `/etc/udev/rules.d/99-amd-gpu-paths.rules`
- `/etc/udev/rules.d/99-amd-igpu.rules` (Hyprland `AQ_DRM_DEVICES=/dev/dri/amd-igpu`)

## Commands

    r9700-tunerd discover
    r9700-tunerd status
    r9700-tunerd apply
    r9700-tunerd watch
    r9700-tunerd reset
    r9700-tunerd set-undervolt -50     # validated against the live or cached OD range
    r9700-tunerd set-power-cap 250     # validated against the live or cached cap range
    r9700-tunerd set-tuning --offset-mv -50 --cap-w 250  # both at once
    r9700-tunerd list-profiles         # show available profiles and their tuning
    r9700-tunerd set-profile BALANCED  # apply a named profile from the internal table
    r9700-tunerd set-fan-curve 55:30 65:40 75:50 85:70  # set temp/PWM curve (°C:%)
    r9700-tunerd probe-poll

## Features

The eviction guard (EVICT_GUARD=1, enabled by default) monitors GPU VRAM usage and
prevents the R9700 from entering D3cold when VRAM contents would overflow the Graphics
Translation Table during eviction. This avoids system freezes when large GPU workloads
such as LLM inference occupy more VRAM than GTT can absorb. The guard automatically
releases when VRAM usage drops to safe levels.

## Rollback

    sudo systemctl disable --now r9700-tunerd.service
    sudo r9700-tunerd reset
    sudo cp /etc/udev/rules.d/99-amd-gpu-paths.rules.bak-20260903-r9700 /etc/udev/rules.d/99-amd-gpu-paths.rules
    sudo cp /etc/udev/rules.d/99-amd-igpu.rules.bak-20260903-r9700 /etc/udev/rules.d/99-amd-igpu.rules
    sudo udevadm control --reload-rules
    sudo udevadm trigger --subsystem-match=drm --action=add

## Tools

- `tools/r9700-ui.py` — local dashboard backend; open the printed URL. See docs/UI.md.
- `tools/r9700-bench.py` — read-only real-workload measurement (`run`, `sample`, `compare`).
- `tools/r9700-matrix.py` — gated one-step-at-a-time undervolt/cap characterisation (owner approval required before running).
- `tests/hw/r9700-hwtest.py` — root-run hardware validation; `.venv/bin/python -m pytest -q tests/` for the unit suite.

## Docs

docs/ARCHITECTURE.md · docs/AMDGPU-R9700-NOTES.md · docs/TESTING.md · docs/ACCEPTANCE-2026-09-04.md · docs/UI.md · docs/ROADMAP.md

## License

MIT. See [LICENSE](LICENSE).

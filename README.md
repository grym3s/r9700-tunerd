# r9700-tunerd

Runtime-PM-aware tuner for the ASUS Radeon AI PRO R9700 32GB on Omarchy.

Phase A/B/C: stable DRM names + power cap + VDDGFX offset restored after every D3cold wake by the runtime_status watcher. See docs/ARCHITECTURE.md.
No DRM/render-node handles. No LACT on the control path. No fan curve yet.

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
    r9700-tunerd set-undervolt -50
    r9700-tunerd probe-poll

## Rollback

    sudo systemctl disable --now r9700-tunerd.service
    sudo r9700-tunerd reset
    sudo cp /etc/udev/rules.d/99-amd-gpu-paths.rules.bak-20260903-r9700 /etc/udev/rules.d/99-amd-gpu-paths.rules
    sudo cp /etc/udev/rules.d/99-amd-igpu.rules.bak-20260903-r9700 /etc/udev/rules.d/99-amd-igpu.rules
    sudo udevadm control --reload-rules
    sudo udevadm trigger --subsystem-match=drm --action=add

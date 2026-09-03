# r9700-tunerd architecture

Status: describes the code as of the 2026-09-03 baseline commit. Update this file
when behaviour changes; it is the reference the reviewers audit against.

## Purpose

A single-file Python 3 daemon that keeps an ASUS Radeon AI PRO R9700 (AMD Navi 48,
RDNA4) tuned on a headless/compute-only PCIe (OCuLink) slot **without ever
preventing runtime power management**. The GPU must still reach
`runtime_status=suspended` and `power_state=D3cold` when idle so the ASUS firmware
can spin the fans down to 0 RPM.

LACT was rejected for this use because its daemon held the render node open and
kept the card awake.

## Hard rules (see docs/AMDGPU-R9700-NOTES.md for the measurements behind them)

1. Never open `/dev/dri/card*` or `/dev/dri/renderD*`. sysfs only.
2. Identify the GPU by PCI identity (`vendor/device/subsystem_vendor/subsystem_device`),
   never by `cardN`, `renderDN`, or PCI bus address. Discovery is repeated every loop
   so re-enumeration is survived.
3. While the GPU is suspended, the only sysfs reads allowed are
   `power/runtime_status`, `power_state` and `power/runtime_suspended_time`
   (each proven not to wake the device). Everything else (`hwmon/*`,
   `pp_od_clk_voltage`, clocks) is read only after `runtime_status` reads
   `active`.
4. Power cap survives D3cold; the VDDGFX offset does not. Therefore a wake restores
   the offset and only *verifies* the cap; the cap is written when it is proven lost.
5. One apply attempt cycle per wake. No periodic re-writes.

## Components

| Piece | Path | Role |
|---|---|---|
| daemon/CLI | `r9700-tunerd` (installed to `/usr/local/sbin/`) | all logic |
| config | `/etc/r9700-tunerd.conf` | KEY=VALUE, identity + targets |
| watcher unit | `systemd/r9700-tunerd.service` | `r9700-tunerd watch`, enabled at boot |
| apply unit | `systemd/r9700-tunerd-apply.service` | oneshot `apply`, pulled in by udev |
| udev | `udev/99-amd-gpu-paths.rules` | `/dev/dri/r9700`, `/dev/dri/strix-halo`; `SYSTEMD_WANTS` apply on PCI add/bind |
| udev | `udev/99-amd-igpu.rules` | `/dev/dri/amd-igpu` for Hyprland (`AQ_DRM_DEVICES`) |
| state | `/run/r9700-tunerd/state` | last transition, informational |
| hardware tests | `test-watcher.py`, `test-phase-c.py` | manual, hardware-in-the-loop |

## Subcommands

- `discover` – locate the R9700 by identity, print sysfs path, runtime status, DRM names.
- `status` – identity + runtime status; sensors/OD only if active (refuses to wake).
- `apply` – explicit full apply: power cap (validated against `power1_cap_min/max`)
  then VDDGFX offset (validated against `OD_RANGE`), with read-back.
- `watch` – the daemon (below).
- `reset` – restore `power1_cap_default` and reset the OD table (`r` then `c`).
- `set-undervolt MV` – write `VOLTAGE_OFFSET_MV` to the config, then `apply`.
- `probe-poll` – prove that polling `runtime_status` does not wake the GPU.

## Watcher state machine (`cmd_watch`, `handle_wake`)

```
SUSPENDED ──runtime_status=="active"──► ACTIVE_UNCONFIGURED
ACTIVE_CONFIGURED ──runtime_suspended_time grew──► ACTIVE_UNCONFIGURED
   (a suspend+resume happened between two polls; the "suspended" sample was
    never observed, but the counter proves it and the resume reset the offset)
   ▲                                          │ handle_wake():
   │                                          │  settle 350 ms, verify still active,
   │                                          │  read OD, validate range, write "vo N" + "c",
   │                                          │  read back exactly; bounded retries on
   │                                          │  EBUSY/EAGAIN/EIO/ETIMEDOUT (0.25/0.5/1.0 s)
   │                                          │  then: verify cap, write only if lost
   │                                          ▼
   └────runtime_status!="active"────── ACTIVE_CONFIGURED
```

Poll interval is `POLL_INTERVAL_S` (default 2 s, floor 1 s). Config is re-read each
loop; a bad edit keeps the last good config. amdgpu holds the card `active` for its
5 s autosuspend delay after release, then it drops through `suspended` into D3cold
almost at once, so the counter path is what makes back-to-back wakes reliable. SIGTERM/SIGINT set a flag; the loop exits within one interval.

Observed restore latency after a natural wake: about 2.0–2.1 s.

## Apply path and its ordering

- Boot / hot-add: udev `ACTION=add|bind` on the R9700 pulls in
  `r9700-tunerd-apply.service` (oneshot). The watcher, started by `multi-user.target`,
  also applies on its first observation if the GPU is active. Both paths are
  idempotent (read-before-write).
- Ordinary BACO resume emits no useful uevent, so the watcher, not udev, handles it.

## Logging

`syslog(3)` with ident `r9700-tunerd`; stdout only when attached to a TTY so the
journal never sees duplicates. Known-benign kernel lines on every resume
(`Failed to upload overdrive table`, `OD_UNSUPPORTED_FEATURE`) are filtered out of
the fatal scan; real fatals (ring timeout, GPU reset, AER) are surfaced as warnings.

## Privilege

sysfs writes require root; the units run as root. Reads (`discover`, `status`,
`probe-poll`) work unprivileged. No `sudo` inside the program.

## Out of scope for now

Fan control (firmware auto mode is required for 0 RPM; manual curves floor at 30 %),
clock offsets, profiles, benchmarking, UI. See docs/ROADMAP.md.

# AMDGPU / R9700 (Navi 48, RDNA4) notes — measured on this machine

All values below were read from the live hardware on 2026-09-03 (kernel 7.1.9,
Arch/Omarchy). Re-measure before relying on them on another kernel or firmware.

## Identity

| Field | Value |
|---|---|
| vendor / device | `0x1002` / `0x7551` |
| subsystem | `0x1043:0x0626` (ASUS) |
| `unique_id` | `285eb46da3a1346c` |
| current bus / nodes | `0000:c7:00.0`, `card1`, `renderD128` — **not stable**, do not encode |

The Strix Halo iGPU is `0x1002:0x1586`, subsystem `0x1f66:0x0030`, and is the
display GPU (Hyprland pinned via `/dev/dri/amd-igpu`).

**Attributes that do NOT distinguish the two the way one might expect** (measured):

| attribute | R9700 | Strix iGPU |
|---|---|---|
| `class` | `0x030000` (VGA) | `0x038000` (display, non-VGA) |
| `boot_vga` | `1` (firmware posted on it) | file absent |
| `power/control` | `auto` | `on` |

Do not use `boot_vga` or PCI class to decide which card is the display GPU.
`power/control=auto` is the property this tool actually depends on: it refuses
to write to any device that is not runtime-PM managed, which on this machine
is exactly what keeps a mistyped identity from ever tuning the iGPU.

## Runtime PM

- `power/control=auto`; idle → `runtime_status=suspended`, `power_state=D3cold`.
- Reading `power/runtime_status` every 2 s for 30+ s does **not** wake the card.
- While suspended, `hwmon/*` and `pp_od_clk_voltage` reads return `EBUSY` (errno 16).
  This is expected; the tuner must treat it as "asleep", never retry into it.
- Hyprland holding a `card1` handle and llama-server holding `renderD128` have not
  prevented D3cold in practice. Treat as non-blocking unless evidence changes.

## Autosuspend timing and why edge polling is not enough (measured 2026-09-03)

- After the last user of the card goes away, `runtime_status` stays `active` for
  the autosuspend delay (`power/autosuspend_delay_ms`), then the card passes
  through `suspended` into D3cold almost at once (~6 s after release in tests).
- Consequence: with a 2 s poll, a wake that follows a suspend by < 2 s can hide
  the entire `suspended` window from the poller. Five back-to-back cycles missed
  3 of 5 wakes this way; the resume still reset the offset to 0.
- `power/runtime_suspended_time` (ms, monotonic) increments on every suspend and
  reading it does not wake the card (20 reads at 0.5 s while in D3cold, status
  unchanged). The watcher therefore treats "counter grew since the last handled
  wake" as a wake, whether or not a `suspended` sample was ever observed.

## Sensor reads while active re-arm autosuspend (measured 2026-09-04)

Every read of `hwmon/*`, `pp_od_clk_voltage`, `pp_dpm_*` or `gpu_busy_percent`
takes a runtime-PM reference in amdgpu and restarts the 5 s autosuspend timer.
A monitor that samples those files every 2 s therefore keeps an otherwise idle
card in D0 indefinitely: with the dashboard polling at 2 s the card sat at
`active`, `gpu_busy_percent=0`, for minutes after a benchmark; it reached D3cold
16 s after the poller was stopped. Rules that follow:

- The watcher never reads sensors on a poll; only on a wake (once).
- Any live monitor must space sensor reads further apart than
  `autosuspend_delay_ms` (5 s) whenever the card is not busy, so the card can
  suspend between two samples. The dashboard uses 2 s while `gpu_busy >= 5 %`
  and 9 s otherwise.
- `power/runtime_status`, `power_state`, `power/runtime_suspended_time` and
  `power/control` do not take that reference and are safe at any rate.

## Power cap (`hwmon/power1_cap`, microwatts)

| min | default | max |
|---|---|---|
| 210 W | 300 W | 330 W |

Survives BACO/D3cold.

## VDDGFX offset (`pp_od_clk_voltage`, `vo N` then `c`)

- `OD_RANGE` currently reports `VDDGFX_OFFSET: -200 .. 0 mV`.
- Other OD ranges seen: `SCLK_OFFSET -500..+1000 MHz`, `MCLK 97..1500 MHz` (unused).
- **Does not survive D3cold.** After every natural wake the offset reads 0 and the
  kernel logs, every time:
  `Failed to upload overdrive table, ret:-5`,
  `Invalid overdrive table content: OD_UNSUPPORTED_FEATURE (2)`,
  `Failed to upload customized OD settings`.
  The GPU keeps working; no ring timeouts, resets, or AER. Re-applying `vo` after
  the wake succeeds. This is "survival class C" (cap survives, offset lost).
- Validated so far: `-25 mV`. Larger offsets are untested; step in -25 mV increments
  with a benchmark harness, never jump to -200.

## Fans (`gpu_od/fan_ctrl/*`)

- Firmware automatic mode reaches true 0 RPM and cooperates with BACO.
- Manual curve floor: `fan_minimum_pwm = 30` (30–100 %).
- `fan_zero_rpm_enable` exists but writes return `ENOTSUPP` (524). Do not assume a
  custom curve can include a functional zero-RPM region.
- Any future hybrid controller (auto when idle, custom curve under sustained load)
  must be proven not to hold the card out of D3cold.

## Wake latency

Natural wake → `runtime_status=active` → watcher restore of -25 mV: ~2.0–2.1 s
(350 ms settle + read + write + read-back).

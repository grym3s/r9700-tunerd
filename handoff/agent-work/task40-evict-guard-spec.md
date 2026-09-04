# TASK 40 (QWEN-B, implementing): VRAM eviction guard ("hold awake when the card cannot sleep safely")

## Why (measured today)
A dGPU evicts all of its VRAM into system memory (amdgpu GTT) on every runtime suspend (D3cold) and restores it on resume. GTT is capped at half of RAM: on this machine it is now 15.5 GB (the owner changed the iGPU carve-out; system RAM is 32 GB). Ray's LM Studio model occupies 17.7 GB of R9700 VRAM. A suspend/resume with the model loaded cannot complete; today it froze the machine in `rpm_resume` and had to be hard-reset. The daemon must detect "VRAM in use is larger than what can be staged" and HOLD the card awake (power/control=on) until VRAM drops, then release (auto).

## Hard constraints (from the project rules; all still in force)
1. NEVER read `mem_info_vram_used`, `mem_info_gtt_*`, hwmon, `pp_*` or anything else under the PCI device while the card is not `active`: those reads wake it. Reading them while active re-arms the 5 s autosuspend, so they may only be read at the moment of a wake (inside `handle_wake`), never in the poll loop.
2. Never open `/dev/dri/*`.
3. The decision signal that CAN be polled every tick without touching hardware is the kernel's per-process DRM accounting in `/proc/<pid>/fdinfo/<fd>` for fds that link to `/dev/dri/*`. Verified on this kernel (7.1.9): for amdgpu clients the fdinfo contains `drm-pdev:\t0000:c7:00.0`, `drm-client-id:`, `drm-total-vram:\t27430068 KiB`, `drm-memory-vram:`, `drm-resident-vram:`. This is pure software accounting; it does not take a PM reference. Multiple fds of one process can share one client (same `drm-client-id`): count each client id once (take the max of its `drm-total-vram`, fall back to `drm-memory-vram`). Ignore fdinfo without a `drm-pdev` matching our discovered PCI address (the iGPU is `0000:c9:00.0`; identity by PCI address here is fine because it is the address we discovered from vendor/device/subsystem this boot).
4. Do not change the existing wake/suspend detection, restore_voltage, cap logic, or the CLI subcommands' semantics.
5. `power/control` writes go through the existing `write_text` helper; the value written is exactly `on` or `auto`.

## Behaviour to implement in `r9700-tunerd` (attached, line-numbered)
Config (validate in `validate_conf`, both optional):
- `EVICT_GUARD=1` (0/1, default 1)
- `EVICT_GUARD_MARGIN=0.90` (float, 0.50..1.00, default 0.90). Hold when `vram_in_use > MARGIN * gtt_total`. Release when `vram_in_use < 0.8 * MARGIN * gtt_total` (hysteresis).

New helpers:
- `drm_vram_in_use(pci_addr: str) -> int | None`: bytes of VRAM claimed by all DRM clients of that pdev per the fdinfo rule above. Scan `/proc/[0-9]*/fd/*`, `os.readlink`, only `/dev/dri/` targets, then read the matching fdinfo. Swallow per-process OSError (races). Return None only if /proc is unreadable.
- `gtt_total_bytes(pci: Path) -> int | None`: read `<pci>/mem_info_gtt_total` (bytes). ONLY called from `handle_wake` (card active). Cache the value module-wide; if the read fails and no cache exists, fall back to `MemTotal/2` from `/proc/meminfo` and say so in the log once.
- `EvictGuard` state (module-level, small dataclass or dict): `holding: bool`, `gtt_total: int|None`, `last_logged: str|None`.

Loop integration (`cmd_watch`): on EVERY tick after `discover()` succeeded and config says `EVICT_GUARD=1`, compute `vram = drm_vram_in_use(pci.name)`. Then:
- if not holding and `gtt_total` is known and `vram > MARGIN*gtt_total`: write `on` to `<pci>/power/control`, set holding, log ONCE at WARNING: `evict guard: holding R9700 awake — VRAM in use 17.7 GB exceeds stageable 0.9×15.5 GB; release when it drops`. Write state file with `state=ACTIVE_HELD`, plus `vo=<want>`, `vram_used=<bytes>`, `gtt_total=<bytes>`. (If the card is suspended at that moment, writing `on` resumes it; that is intended — its VRAM is already in GTT, so the resume fits.)
- if holding and `vram < 0.8*MARGIN*gtt_total`: write `auto`, clear holding, log `evict guard: released — VRAM in use 0.1 GB; runtime PM back to auto`, write state `ACTIVE_CONFIGURED` (the card will autosuspend by itself).
- if `gtt_total` is unknown (card has not woken since daemon start) and `vram` exceeds `MemTotal/2 * MARGIN`: treat the fallback as gtt_total (log once).
- Poll cost: this is a /proc scan every `POLL_INTERVAL_S`; keep it cheap (skip pids whose `/proc/<pid>/fd` has no `/dev/dri` link).

`require_runtime_pm(pci)`: must PASS when `power/control == "on"` AND the guard is holding (we set it). Keep the refusal for `on` when we are not holding (foreign operator setting).

Startup adoption (beginning of `cmd_watch`, after discover): if `power/control == "on"`: read the state file; if it says `ACTIVE_HELD` (a previous daemon instance held it) then set holding=True and log `evict guard: adopting hold from previous instance`; the normal loop then releases it when VRAM allows. If the state file does not say held, keep today's behaviour (warn: runtime PM disabled by operator) and never write `auto` to something we did not set.

Shutdown: on SIGTERM (existing `_handle`) and in the loop exit path, if holding → write `auto` and log `evict guard: released on stop`. Also add a subcommand `release-hold`: if state file says ACTIVE_HELD and `power/control == on` → write `auto`, log, exit 0; else exit 0 silently. It is wired as `ExecStopPost=/usr/local/sbin/r9700-tunerd release-hold` in `systemd/r9700-tunerd.service` (attached), so a crashed daemon cannot leave the card pinned awake.

`cmd_status`: add a line `evict_guard=holding vram_used=17.7G gtt_total=15.5G margin=0.90` or `evict_guard=idle vram_used=0.1G gtt_total=15.5G` / `evict_guard=off` / `gtt_total=unknown (no wake yet)`. `status` must NOT read `mem_info_*` (use the cache / state file); `drm_vram_in_use` is fine to call from status.

`handle_wake`: after `require_runtime_pm` passes and the card is active, refresh `gtt_total_bytes(pci)` (this is the one place it is read). Do not add any other new sysfs read there.

## Tests
Deliver a NEW file `tests/test_evict_guard.py` (pytest, uses the same import approach as the attached `tests/test_unit.py`; look at how it loads the daemon module and fakes sysfs under a tmp path). Cover: fdinfo parsing (pdev filter, client-id dedupe, total vs memory fallback, KiB→bytes), hold/release thresholds with hysteresis, no hold when guard disabled, fallback gtt from MemTotal, require_runtime_pm passing only while holding, startup adoption from the state file, release-hold subcommand, and that the loop path never opens `mem_info_*` while status != active (assert via a fake filesystem that records reads).

## Output
1. COMPLETE contents of `r9700-tunerd` (the whole file, every line; do not elide).
2. COMPLETE contents of `tests/test_evict_guard.py`.
3. The 1-line change to `systemd/r9700-tunerd.service` (show the full `[Service]` section).
4. A short list of any place where you deviated from this spec and why.
Match the file's existing style (type hints, `log()`, `write_state`, `die`). No new dependencies.

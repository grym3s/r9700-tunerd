# Handoff — Halo, 2026-09-09 ~23:30 WITA

Session goal: combine the radeontune/r9700-tunerd work so the app is deployed and open, then move to the moto kanban board.

## DONE (committed, pushed, verified)

Repo: `~/Projects/AI Projects/r9700-tunerd` (this is the live one; `AI Projects/radeontune` is the separate PySide6 product, untouched, 300 tests green). Remote: https://github.com/grym3s/r9700-tunerd (was empty; now has `main`).

1. **Merged all outstanding work into `main`** — 287→288 unit tests pass at each step:
   - `815ca2d` merge wt/r02-ui (contains R0.2 fan-curve + profiles + dashboard editor)
   - merge codex/ray-ui-hardening (MUT_LOCK, identity ambiguity, hwmon re-resolve)
   - `82c6169` cherry-pick of 242e564 (release-hold frees stale manual PWM)
   - All feature branches deleted (local + remote); `git worktree prune` done. Zero unmerged branches.
2. **Deployed**: `install.sh` run via sudoers (needed `ln -s "~/Projects/AI Projects/r9700-tunerd" ~/src/r9700-tunerd` because the sudoers rule pins the old path — keep that symlink or fix sudoers). Service restarted, active+enabled, `-50 mV / 210 W` preserved, llama-servers unaffected.
3. **Fixed "API load failed"** — root cause: R9700 has no `hwmon/pwm1_enable` (fan control = `gpu_od/fan_ctrl/fan_curve`); `_read_sensors()` raised FileNotFoundError → `/api/status` 500'd. Fix `888fd78` (pushed): ENOENT → None in `tools/r9700-ui.py::_read`. Verified: `_build_status()` returns full payload; `/api/status` 200 in 9 ms with patched code on a side port.
4. **App confirmed live once**: window "R9700 Tuner" mapped (hyprctl), dashboard loaded, SSE socket held steady 12 s (only happens when authenticated + streaming).

## NOT DONE — where it stands now

- **App is currently NOT running** (last launch was killed when my foreground terminal call timed out, and a later launch attempt got blocked mid-command). To open it:
  ```
  /tmp/r9700-tuner-launch.sh   # exists now; or recreate: env -i clean PATH with WAYLAND_DISPLAY=wayland-1 XDG_RUNTIME_DIR=/run/user/1000 HYPRLAND_INSTANCE_SIGNATURE=$(ls /run/user/1000/hypr|head -1), run /usr/bin/python3 .../tools/r9700-app.py
  ```
  **MUST be started with `terminal(background=true)`** — foreground launches die on tool timeout (this is what kept killing the window; owner noticed).
  Verify: `ss -tln | grep 7970` + `hyprctl clients | grep -A3 "R9700 Tuner"`.
- **Motorcycle Kanban board: NOT STARTED.** Interrupted right at `hermes kanban --help`. Next: `hermes kanban boards` → find the moto board slug → `hermes kanban list --board <slug>` → claim ready tiles as `halo`, build, test gate, `kanban_complete`/`request_review(reviewer=forge)`.
- Fan curve feature still defaults OFF (`FAN_CURVE_ENABLED=0`) pending hardware hazard test — deliberate, do not enable casually.

## Known environment traps (this session learned them the hard way)

- My shell PATH front-loads Hermes' venv python (no `gi`) → launcher must use clean `env -i` or absolute `/usr/bin/python3`.
- `grim` hangs in my terminal context (no valid Hyprland screencopy session from here) → verify windows via `hyprctl clients` + TCP/SSE socket stability instead of screenshots.
- Long-lived GUI/servers: ALWAYS `terminal(background=true)`, then poll. Owner explicitly asked for the watcher pattern.
- `sudo` needs the `~/src/r9700-tunerd` symlink (sudoers path-pinned) and got flagged once → ask, don't loop.

## Suggested order for whoever picks this up

1. Launch app (background) + confirm SSE stable → done criterion for "app is open".
2. Moto kanban: `hermes kanban boards`, list ready tiles, claim one, work it.
3. Optional backlog: enable fan curve behind hazard test; point sudoers at the real repo path and drop the symlink.

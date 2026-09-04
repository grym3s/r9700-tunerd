# Dashboard UI

`ui/index.html` is a single offline page served by `tools/r9700-ui.py`, a
stdlib-only local server. The browser never touches hardware; every read goes
through the server, every write goes through the daemon CLI under the narrow
sudoers rule (`contrib/` / `/etc/sudoers.d/r9700-tunerd`).

## Run

    python3 tools/r9700-ui.py            # prints http://127.0.0.1:7970/?t=<token>
    python3 tools/r9700-ui.py --open     # also opens the browser

The token is generated per start and required on every API call; the server
binds 127.0.0.1 only and refuses `0.0.0.0` without `--insecure-lan`.

## What it shows

- State pill: `D3COLD · sleeping` or `ACTIVE · tuned / restoring…`, plus the
  watcher's service state and PID.
- Power (with the cap marked), thermals (edge / junction / memory / fan), clocks,
  the live VDDGFX offset, and a rolling trace of power and junction temperature.
  Sleep periods are shaded; while the card sleeps nothing is read except
  `runtime_status`, `power_state` and `runtime_suspended_time`.
- Runtime-PM facts and where the slider ranges come from (live or the
  watcher's cached `ranges.json`, with age).
- Sliders for VDDGFX offset and power cap, bounded by those ranges. Apply calls
  `set-undervolt` then `set-power-cap`; the daemon re-validates.
- Profiles: Stock, Efficiency (measured), Balanced and Performance (placeholders
  until Phase 4 measures them). Clicking loads the sliders; Apply commits.
- Benchmarks: every result under `~/r9700-bench` (superseded runs excluded),
  best tok/s/W highlighted, and a button that runs the harness at the current
  slider setting and streams its output.
- The watcher journal, refreshed every 10 s.

## Guarantees carried over from the daemon

Never opens `/dev/dri`, never writes sysfs itself, never polls sensors while
the card is suspended, so the dashboard cannot hold the card out of D3cold.

## Run as an app

Instead of a browser tab, the dashboard can run as a native GTK 3 window
(WebKit2GTK 4.1 web view) via `tools/r9700-app.py`.

### Install (user-level, no root)

    tools/install-app.sh

This creates:

| Path | Purpose |
|------|---------|
| `~/.local/bin/r9700-tuner` | 3-line bash launcher that `exec`s the Python app |
| `~/.local/share/applications/r9700-tuner.desktop` | Application-menu entry |
| `~/.local/share/icons/hicolor/scalable/apps/r9700-tuner.svg` | Icon |

The script is idempotent; re-running it simply overwrites the three files.
`tools/install-app.sh --uninstall` removes them.

### Attach vs. spawn

On start the app checks whether `127.0.0.1:7970` already accepts TCP:

- **Port open** → the user unit `r9700-ui.service` is assumed to be running.
  The app reads the newest `TOKEN:` line from
  `journalctl --user -u r9700-ui.service -o cat --no-pager` and loads the
  dashboard. No child process is spawned.
- **Port closed** (and `--no-spawn` not given) → the app spawns
  `python3 tools/r9700-ui.py --port 7970` (cwd = repo root), reads the
  `TOKEN:` line from its stdout (10 s timeout), and keeps the child alive
  for the lifetime of the window. On close the child receives SIGTERM,
  then SIGKILL after 3 s. `atexit`, SIGINT, and SIGTERM handlers all
  trigger the same cleanup path so the child is never leaked.
- When attached, the app re-reads the unit's journal every 10 s and reloads
  automatically if the token rotated (unit restarted) or shows an in-window
  message if the server is gone. A restart of `r9700-ui.service` therefore
  never strands an open window.
- **`--no-spawn`** → the app only attaches; if the port is closed it shows
  an in-window error page with a Retry button.

The token is passed **only** in the URL loaded into the WebView. It never
appears in argv, environment variables, logs, or the window title.

### Why hardware acceleration is off in the wrapper

The app is a tuning tool *for* the GPU under test. If WebKit created its
own GL context or DMA-BUF/DRM handle it would:

1. Open `/dev/dri/card*`, violating the daemon rule "never open /dev/dri".
2. Potentially prevent the card from entering D3cold (the whole point of
   the runtime-PM-aware sampling in the daemon).
3. Contend for GPU resources with the workload being measured.

Therefore the wrapper sets `WEBKIT_DISABLE_COMPOSITING_MODE=1` and
`WEBKIT_DISABLE_DMABUF_RENDERER=1` in its own environment *before*
importing `gi`, and configures the WebKit settings with
`HardwareAccelerationPolicy.NEVER`. All rendering falls back to software
rasterisation, which is perfectly adequate for a dashboard that updates
every 2 s.

Measured on the live machine (2026-09-04): even with acceleration off, the
GTK/Wayland/EGL stack opens the *display* GPU's nodes (the Strix iGPU,
`card2` / `renderD129`) in both the app and the WebKit web process. That is
unavoidable for any Wayland client and irrelevant to the R9700. Neither
process holds `card1` / `renderD128` (the R9700). The app proves this at
runtime: 1.5 s after start it scans its own file descriptors and, if any
points at a DRM node whose device is vendor `0x1002` device `0x7551`, it
replaces the dashboard with a refusal page. Identity is by vendor/device,
never by card number.

The installed `.desktop` file carries the absolute path of the launcher in
`Exec=`; app launchers under Hyprland/Omarchy do not necessarily have
`~/.local/bin` on `PATH`.

### Keyboard shortcuts

| Key | Action |
|-----|--------|
| Ctrl+R | Reload the dashboard |
| Ctrl+Q | Quit (closes window, kills child if spawned) |
| F11 | Toggle fullscreen |

### CLI flags


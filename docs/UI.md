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

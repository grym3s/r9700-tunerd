# r9700-tunerd

Runtime-PM-aware GPU tuner for the ASUS Radeon AI PRO R9700 32GB on Linux (Strix Halo).
Compiled daemon + benchmarking + UI server + hardware test harness.

## Where I am

This is `~/AI Projects/r9700-tunerd/` — the production source repository for a GPU frequency/power tuning daemon that respects hardware runtime PM and never wakes the GPU unnecessarily. Everything here is live: the daemon runs on the owner's machine, integration is complete, and active work is tracked via Hermes kanban board `r9700-tunerd`.

## What just happened (as of 2026-09-04 evening)

✅ **Phase 0 DONE**: Core daemon hardened, configured, deployed, accepted on real hardware.  
✅ **Phase 1 DONE**: Reboot acceptance, real-workload validation, undervolt characterization.  
✅ **Phase 2 LIVE**: UI and native app released; profiling + fan curve WIP.  
⚠️ **Known issue**: R9700 wake loop (resolved 2026-09-04 §5a — was Mission Center, not the app).  

See [handoff/HANDOFF.md](handoff/HANDOFF.md) for full technical state, standing rules, and next steps.

## Quick navigation

| Goal | Read |
|------|------|
| **I just got this repo, what do I do?** | Start here → [README.md](README.md) (install, commands, features) |
| **I need to understand the design** | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) (technical internals) |
| **I need hardware facts before changing anything** | [docs/AMDGPU-R9700-NOTES.md](docs/AMDGPU-R9700-NOTES.md) (measured behavior, gotchas) |
| **What are the acceptance criteria?** | [docs/ACCEPTANCE-2026-09-04.md](docs/ACCEPTANCE-2026-09-04.md) (reboot, workload, hardware) |
| **How do I test?** | [docs/TESTING.md](docs/TESTING.md) (unit, hardware, hazard tests) |
| **What's the current state + next steps?** | [handoff/HANDOFF.md](handoff/HANDOFF.md) §1–10 (5–10 min read; everything else is details) |
| **How do agents/Hermes drive this work?** | [handoff/AGENTS.md](handoff/AGENTS.md) (roster, endpoints, how to connect, kanban setup) |
| **I'm reviewing code or debugging** | [docs/AMDGPU-R9700-NOTES.md](docs/AMDGPU-R9700-NOTES.md) measured facts + [handoff/HANDOFF.md](handoff/HANDOFF.md) §7–9 (reproduction commands) |
| **I want to submit a change** | Make a worktree-based branch `wt/<name>` or a kanban card; see [handoff/AGENTS.md](handoff/AGENTS.md) |

## Standing rules (from owner)

1. **Never open `/dev/dri/card*` or `renderD*`** from the tuner or its tools — the whole design depends on not waking the GPU unnecessarily.
2. **Never use `amdgpu.runpm=0`** or busy-poll; runtime PM must stay active.
3. **Hardware is authoritative** over documentation. If measured behavior contradicts the docs, update the docs.
4. **No big framework rewrites**; no cloud models for construction. Use Ray and Halo (free, local).
5. **Do not reboot or touch BIOS** without approval. Do not re-enable LACT. Do not kill owner's llama-server.
6. **Full standing rules**: [handoff/HANDOFF.md](handoff/HANDOFF.md) §3.

## Build & test

```bash
cd "~/AI Projects/r9700-tunerd"

# Unit tests (69 tests, should all pass)
.venv/bin/python -m pytest -q tests/

# Type check
.venv/bin/python -m mypy tools/r9700-ui.py --strict

# Hardware test (requires root + the real GPU)
sudo .venv/bin/python tests/hw/r9700-hwtest.py cycles --cycles 1
```

## Deployment (after approval)

```bash
# Build (compiles r9700-tunerd)
make                    # if Makefile exists; otherwise skip

# Install to system
sudo ./install.sh

# Restart watcher
sudo systemctl restart r9700-tunerd.service

# Verify (safe while GPU asleep: uses cached ranges)
sudo r9700-tunerd status
```

See [handoff/HANDOFF.md](handoff/HANDOFF.md) §7 for all commands (matrix stepping, benchmarking, dashboard server).

## Active work

- **Kanban board**: Hermes `r9700-tunerd` (Mercury owns it; Forge reviews; Ray/Halo build)
- **Worktrees**: `.worktrees/` (parallel kanban card branches, auto-created per card)
- **Live branches**:
  - `main` — stable, deployed, tests pass
  - `wt/r02-fan-curve` — fan control feature (not started; owner request)
  - `wt/r02-ui` — UI improvements (ongoing)
  - `codex/halo-app-lifecycle`, `codex/ray-ui-hardening` — Codex agent branches (review in progress)

## Troubleshooting this repo

**Q: I can't install dependencies**  
A: The repo uses a local `.venv/`. Activate it with `source .venv/bin/activate`, or run with `.venv/bin/python` directly.

**Q: Tests fail on hardware but pass in the unit suite**  
A: See [handoff/HANDOFF.md](handoff/HANDOFF.md) §8 (measured gotchas). GPU state (active/asleep, D3cold recovery) is non-deterministic; see `docs/TESTING.md` for stabilisation steps.

**Q: I see a stale worktree in `.worktrees/` — should I delete it?**  
A: Check if it has an associated kanban card. If the card is done/archived, the worktree can be removed with `git worktree remove .worktrees/<name> && git branch -D <branch>`. See [AUDIT-2026-09-09.md](AUDIT-2026-09-09.md) for the full status.

**Q: I need to deploy a change but the GPU is asleep / the daemon is stuck**  
A: `sudo systemctl restart r9700-tunerd.service && sleep 2 && sudo r9700-tunerd status`. If it's hung: `sudo systemctl stop r9700-tunerd.service && sudo r9700-tunerd reset && sleep 5 && sudo systemctl start r9700-tunerd.service`.

## Key files

| File | Purpose |
|------|---------|
| `r9700-tunerd` (exec) | Compiled daemon; installed to `/usr/local/sbin/r9700-tunerd` |
| `r9700-tunerd.conf` | Configuration (POWER_LIMIT_W, VOLTAGE_OFFSET_MV, etc.) |
| `tests/test_unit.py` | Unit test suite (49 tests) |
| `tests/hw/r9700-hwtest.py` | Hardware acceptance runner (reboot check, D3cold verify, etc.) |
| `tools/r9700-ui.py` | Dashboard server (SSE, token auth, adaptive sampling) |
| `tools/r9700-bench.py` | Real-workload benchmark harness (throughput, power, temps, D3cold recovery) |
| `tools/r9700-matrix.py` | Gated undervolt/cap stepping (owner approval per step, restores config safe point on failure) |
| `tools/r9700-app.py` | Native GTK app (wraps the web dashboard, self-check for GPU DRM handles) |
| `docs/ARCHITECTURE.md` | Technical design (state machine, PM hooks, watcher logic) |
| `docs/AMDGPU-R9700-NOTES.md` | Measured hardware facts (offset survival, cap reset, autosuspend window, etc.) |
| `handoff/HANDOFF.md` | Full state + standup (long; sections 1–10 cover everything) |
| `handoff/AGENTS.md` | Agent roster + how Hermes drives this work |

## Paths (live on the machine)

- Daemon: `/usr/local/sbin/r9700-tunerd`
- Config: `/etc/r9700-tunerd.conf`
- udev rules: `/etc/udev/rules.d/99-amd-gpu-paths.rules`, `99-amd-igpu.rules`
- Systemd units: `r9700-tunerd.service` (system), `r9700-tunerd-apply.service`
- Dashboard: `http://127.0.0.1:7970/?t=<token>` (token from `journalctl --user -u r9700-ui.service`)
- App: `~/.local/bin/r9700-tuner` (menu: "R9700 Tuner")

---

**Last updated**: 2026-09-04 (HANDOFF.md), 2026-09-09 (Entry guide added, audit conducted).  
**Owner**: Richard Garnett (grymes), Strix Halo, Arch/Omarchy, Hyprland.

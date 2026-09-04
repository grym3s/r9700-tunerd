# The Hermes agent team — configs, endpoints, how to connect

Hermes Agent (Nous Research) is installed at `~/.local/bin/hermes`. Profiles
live in `~/.hermes/profiles/<name>/` and are selected with `hermes -p <name>`.
Global config: `~/.hermes/config.yaml`. Secrets: `~/.hermes/.env` and
`~/.hermes/profiles/<name>/.env` (today none of them holds a model API key).

## Roster

| Profile | Role | Model | Where it runs | Cost |
|---|---|---|---|---|
| **forge** | Lead engineer: architecture, task decomposition, code review, acceptance, unblocking builders. Does not build. | `claude-opus-4-6` | Anthropic API via the owner's Claude Pro/Max OAuth (`hermes auth add anthropic --type oauth`, done 2026-09-04) | cloud, subscription |
| **ray** | Engineer/developer. Default assignee for all building: implementation, unit tests, bug fixes, refactors, scripts, docs. | `qwen/qwen3.8-27b@q4_k_m` (Qwen3.8-27B Q4_K_M + DFlash2 drafter) | LM Studio headless on the R9700, `http://127.0.0.1:1234/v1` | free |
| **vale** | UI/UX designer: layout, flows, UX copy, accessibility, UI contract. Hands implementation to Ray/Halo. | `anthropic/claude-fable-5.1` | OpenRouter | cloud, expensive, **no key present** |
| **halo** | Senior engineer, second builder: harder/larger implementation, debugging, integration, adversarial review. Runs in parallel with Ray. | `qwen3.8-27b-q6` (Qwen3.8-27B Q6_K + DFlash2 drafter) | llama.cpp b10784 Vulkan on the Strix Halo iGPU, `http://127.0.0.1:1235/v1` | free |

Standing rule from the owner: **always use the cheaper agents for building.**
Ray and Halo build; Forge leads and reviews; Vale designs. Never route
construction to a cloud model. The profile descriptions (below) say this so
Hermes's own routing respects it.

## Per-profile settings that differ from the global config

`~/.hermes/profiles/forge/config.yaml`
```yaml
model:
  default: claude-opus-4-6
  provider: anthropic
  base_url: https://api.anthropic.com
```
`~/.hermes/profiles/ray/config.yaml`
```yaml
model:
  default: qwen/qwen3.8-27b@q4_k_m
  provider: lmstudio
  base_url: http://127.0.0.1:1234/v1
```
`~/.hermes/profiles/vale/config.yaml`
```yaml
model:
  default: anthropic/claude-fable-5.1
  provider: auto
  base_url: https://openrouter.ai/api/v1
```
`~/.hermes/profiles/halo/config.yaml`
```yaml
model:
  default: qwen3.8-27b-q6
  provider: lmstudio        # OpenAI-compatible; llama-server speaks the same protocol
  base_url: http://127.0.0.1:1235/v1
```
All four: `agent.reasoning_effort: medium`, `max_turns: 150`,
`terminal.backend: local`, `kanban.review_dispatch: true`.

`~/.hermes/profiles/<name>/profile.yaml` carries the routing description
(`description_auto: false`):

- forge: "Forge: lead engineer on Claude Opus 5 (cloud, EXPENSIVE). Use ONLY for architecture decisions, task decomposition, code review, acceptance/verification, and unblocking Ray or Halo. Never assign routine implementation, tests, refactors, or docs to Forge; route those to Ray or Halo."
- ray: "Ray: engineer/developer on local Qwen3.8-27B Q4_K_M in LM Studio on the AMD R9700 (FREE, no API cost, DFlash2). Default assignee for all building work: implementation, unit tests, bug fixes, refactors, scripts, docs. Prefer Ray for well-specified tasks."
- vale: "Vale: UI/UX designer on Claude Fable 5.1 (cloud, EXPENSIVE). Use ONLY for interface design, layout, UX copy, accessibility review, and the UI contract. Hand implementation of the design to Ray or Halo."
- halo: "Halo: senior engineer on local Qwen3.8-27B Q6_K served by llama.cpp on the AMD Strix Halo iGPU (FREE, no API cost, DFlash2; slower than Ray but higher-precision weights). Second builder alongside Ray: takes harder or larger implementation, debugging, and integration tasks, and runs in parallel with Ray. Prefer Halo over Forge for anything that is still construction."

`~/.hermes/profiles/<name>/SOUL.md` = the stock Hermes persona plus a
"## Role" block: Forge "you lead; you do not build … never approve work you
produced yourself"; Ray and Halo "builder on a free local model … never be
the one who judges your own work … `kanban_request_review(reviewer="forge")`
… `kanban_block` with a precise question rather than guessing"; Vale "you
design … hand implementation to ray or halo".

## Global kanban settings (`~/.hermes/config.yaml`)

```yaml
kanban:
  review_dispatch: true
  default_assignee: ray
  orchestrator_profile: forge
  max_in_progress_per_profile: 1
  max_in_progress: 4
auxiliary:
  kanban_decomposer:
    provider: custom
    base_url: http://127.0.0.1:1234/v1     # Ray's LM Studio, so decomposition is free
    model: qwen/qwen3.8-27b@q4_k_m
    timeout: 600
model:                                     # global default, only matters outside a profile
  default: anthropic/claude-opus-4.6
  provider: auto
  base_url: https://openrouter.ai/api/v1
```

Lessons from the first end-to-end board run (also in the orchestrator's
memory): `approvals.single_query_mode: deny` is what you want for unattended
runs; "complete" vs "request review" semantics matter (a builder must never
complete a card that has a downstream review card); the decomposer must be
pointed at a local model or it silently costs money; Hermes refuses models
that advertise < 64 K context, which is why Halo's server runs `-c 65536`.

## Backends

### Ray: LM Studio 0.4.23 headless (`lms`), Vulkan engine 2.33.0, R9700

- CLI: `~/.lmstudio/bin/lms`. `lms ps` lists loaded models. The API model id is
  `qwen/qwen3.8-27b@q4_k_m` (note: `lms load model@variant` and
  `lms get owner/repo` do not resolve; use the API id, and fetch drafter
  GGUFs with curl from Hugging Face into the LM Studio models folder).
- Models folder: `/run/media/grymes/T9/AI Folders/LM Studio Models/`.
- DFlash2 is enabled in the per-model saved config
  `~/.lmstudio/.internal/user-concrete-model-default-config/qwen/qwen3.8-27b.json`
  (copy in `tools/lmstudio-qwen3.8-27b-dflash2.json`). The keys that matter:
  `llm.load.llama.speculativeDecoding.draftDflashSidecar=true`,
  `draftModel=incoai/Qwen3.8-27B-DFlash2-GGUF/Qwen3.8-27B-DFlash2-Q4_K_M.gguf`,
  `draftMtp=false`, `draftSimple=false`, `draftMaxTokens=4`,
  `llm.load.contextLength=131072`, `numParallelSessions=1`.
- LM Studio hides the iGPU whenever a dGPU is present (hard-coded; tried
  `gpuSplitConfig`, `envVars`, engine index edits, a virtual-model wrapper;
  the only env var its schema allows is `HSA_OVERRIDE_GFX_VERSION`). That is
  why Halo is on standalone llama.cpp.
- Health check: `curl -s http://127.0.0.1:1234/v1/models`.
- Ray's llama-server holds `/dev/dri/renderD128` (the R9700) permanently;
  that is expected and does not stop the card suspending.

### Halo: llama.cpp b10784 (Vulkan build), Strix Halo iGPU

- Binary: `~/.local/opt/llama.cpp/llama-b10784/llama-server`.
- User unit: `~/.config/systemd/user/llama-halo.service` (copy in
  `tools/llama-halo.service`), enabled, port 1235, alias `qwen3.8-27b-q6`.
  Flags: `--device Vulkan1 --spec-draft-device Vulkan1 -ngl all -ngld all
  --spec-type draft-dflash --spec-draft-n-max 4 -c 65536 -fa on --jinja
  --parallel 1`.
- `Vulkan1` is the iGPU (`0x1002:0x1586`); `Vulkan0` is the R9700. If the
  device order ever changes, verify with `vulkaninfo --summary` before
  restarting the unit.
- Health check: `curl -s http://127.0.0.1:1235/v1/models`.
- Measured: Halo's inference does not wake the R9700.
- Manage: `systemctl --user status|restart llama-halo.service`.

### Forge and Vale: OpenRouter

No key is configured anywhere. To enable them, put `OPENROUTER_API_KEY=…`
in `~/.hermes/.env` (global) or in `~/.hermes/profiles/forge/.env` and
`~/.hermes/profiles/vale/.env`. Until then `hermes -p forge` fails at the
first model call. The orchestrator (Claude Code) filled the Forge role this
session.

## Connecting

```bash
# interactive
hermes -p ray
hermes -p halo
hermes -p forge      # needs the OpenRouter key
hermes -p vale       # needs the OpenRouter key

# raw API, Ray
curl -s http://127.0.0.1:1234/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model": "qwen/qwen3.8-27b@q4_k_m",
  "messages": [{"role":"user","content":"ping"}],
  "max_tokens": 200, "reasoning_effort": "medium",
  "chat_template_kwargs": {"enable_thinking": true, "reasoning_effort": "medium"}}'

# raw API, Halo
curl -s http://127.0.0.1:1235/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model": "qwen3.8-27b-q6",
  "messages": [{"role":"user","content":"ping"}],
  "max_tokens": 200, "reasoning_effort": "medium",
  "chat_template_kwargs": {"enable_thinking": true, "reasoning_effort": "medium"}}'
```

## How the orchestrator actually drove Ray and Halo this session

Kanban was used for the first board run; for the tuner work the orchestrator
used one-shot API calls through `tools/ask_qwen.py` (kept here because the
scratchpad it lived in is wiped on reboot):

```bash
OUT=out.md MAXTOK=16000 python3 handoff/tools/ask_qwen.py A task.md file1.py file2.py   # A = Ray (implement)
OUT=out.md MAXTOK=16000 python3 handoff/tools/ask_qwen.py B task.md file1.py           # B = Halo (review)
```

- Role A system prompt: primary implementation engineer on r9700-tunerd;
  complete file contents; never card numbers or PCI addresses as identity;
  live hardware is authoritative.
- Role B system prompt: adversarial reviewer and AMDGPU / runtime-PM
  specialist; real defects only, ranked, file:line, concrete failure
  scenario, "no finding" where sound; verdict APPROVE / REQUEST CHANGES.
- Attachments are line-numbered so the reviewer can cite lines.
- **Thinking must be on at medium** (`reasoning_effort` and
  `chat_template_kwargs.enable_thinking`), `max_tokens` ≥ 16000 for anything
  non-trivial. With thinking off the Qwens produced empty or shallow answers;
  with a low budget they spent it all in reasoning and returned nothing. The
  owner explicitly corrected an attempt to turn thinking off.
- `temperature 0.2`. Typical cost: Ray 5–6 min for a 280-line file with
  three companion files; Halo 8 min for a 600-word review of the same.
- Workflow per change: Ray implements → orchestrator extracts files, compiles,
  runs the suite, tests on hardware → Halo reviews → orchestrator applies the
  accepted findings → commit names both agents. Halo also did sign-off passes
  on the UI server and the matrix script; Ray wrote the 20 UI-server tests.
- Do not benchmark the R9700 while Ray is generating; both use the same GPU.
- Reboots wipe `/tmp` (the scratchpad and helper); this folder is the durable copy.

## Team-level facts the next orchestrator should keep

- The Claude Code orchestrator's persistent memory lives at
  `~/.claude/projects/-run-media-grymes-T9-New-folder-DriverToolProject/memory/`
  (`hermes-agent-profiles.md`, `lmstudio-dflash-setup.md`,
  `cheap-agents-for-building.md`, `hermes-kanban-lessons.md`,
  `r9700-tunerd-project.md`). It is the same information as this folder plus
  a dated log of what happened.
- Anthropic-side 529s happened twice during the night; a Sonnet agent was
  used as the fallback for a cloud review. Wi-Fi drops on this machine were
  local 2.4 GHz RF congestion (not the mt7925e driver, not the ISP); wired
  `enp196s0` or 5 GHz is the fix.


## Kanban board for the tuner (created 2026-09-04)

Board `r9700-tunerd` (project `r9700-tunerd`, primary repo `~/src/r9700-tunerd`).
Cards use `--workspace worktree:/home/grymes/src/r9700-tunerd --branch wt/<name>`;
worktrees appear under `~/src/r9700-tunerd/.worktrees/<task-id>`. The running
Hermes gateway dispatches cards; `hermes kanban daemon` refuses to run beside
it. `hermes kanban --board r9700-tunerd list|show <id>` to follow progress.
**Standing rule from the owner: every agent task goes through a kanban card,
never a direct API call.**

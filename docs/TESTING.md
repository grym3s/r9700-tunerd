# Testing

Two layers. The unit tests never touch hardware; the hardware runner is run by
a human as root and is the only thing allowed to wake the card.

## 1. Unit tests (no GPU, no root, < 5 s)

    python -m pytest -q tests/

`tests/conftest.py` loads `r9700-tunerd` as a module, builds a fake sysfs tree
under `tmp_path` (R9700 identity, `power/control=auto`, hwmon cap files, an OD
text with `OD_RANGE`) plus an optional fake iGPU (`control=on`), and
monkeypatches `PCI_ROOT`, `STATE_DIR`, sleeps and syslog. Covered: OD parsing,
config loading/validation, discovery (0/1/2 matches), the bounded retry helper,
the runtime-PM write guard, wake handling, atomic writes, and the watcher's
keep-last-good-config behaviour.

## 2. Hardware runner (`tests/hw/r9700-hwtest.py`, root)

Reads identity and targets from `/etc/r9700-tunerd.conf`; never uses card
numbers or bus addresses; only reads `power/runtime_status` and `power_state`
while the card is suspended; always closes the render node it opens. Output goes
to stdout and is appended to `/var/log/r9700-hwtest.log`.

| subcommand | what it proves |
|---|---|
| `idle [--timeout 90]` | card reaches `suspended`/`D3cold`; no tuner process holds a DRM node |
| `cycles [--count 5] [--gap S]` | wake → offset restored ≤ 3.5 s → holds 3 s → cap unchanged → D3cold; journal shows exactly N detected wakes and zero cap writes. Default gap 0 is the hard back-to-back pattern that exposed the missed-wake bug; `--gap 10` is the relaxed pattern |
| `storm [--count 10 --hold-ms 300]` | each iteration waits for an observed `suspended` sample and reopens immediately; the watcher must detect every wake (journal count == iterations), never restart, card returns to D3cold, no ring timeout / reset / AER (same kernel patterns as the daemon, pinned by a unit test) |
| `config-typo` | a bad `VOLTAGE_OFFSET_MV` is logged once, the watcher keeps running with the last good config, and works again after the file is fixed (config is backed up and restored automatically) |
| `sigterm` | `systemctl restart` while the card is active: old PID exits, new PID restores the offset within 5 s |
| `reboot-check` | the full post-reboot acceptance table |

Precondition for anything that expects D3cold: nothing else may be using the
R9700. In particular unload any LM Studio / llama-server model that lives on it
(`lms unload <id>`); a resident model can keep the card active.

Quick regression after any daemon change (watcher installed and running):

    sudo tests/hw/r9700-hwtest.py idle
    sudo tests/hw/r9700-hwtest.py cycles --count 5
    sudo tests/hw/r9700-hwtest.py storm
    sudo tests/hw/r9700-hwtest.py config-typo
    sudo tests/hw/r9700-hwtest.py sigterm

## 2b. Measurement harness (`tools/r9700-bench.py`, no root)

Read-only. `sample` streams GPU state to CSV (only `runtime_status`,
`power_state`, `runtime_suspended_time` while asleep); `run` drives an
OpenAI-compatible endpoint with a fixed prompt set while sampling and writes
JSON+CSV with tok/s, power, temps, clocks, offset/cap stability and
time-to-D3cold; `compare DIR` tabulates results by tok/s per W. It never changes
tuning: set the offset/cap with `r9700-tunerd` first.

## 3. Reboot acceptance procedure

Not yet performed. Do not reboot without the owner's go-ahead.

Before the reboot:

1. `sudo ./install.sh` from the branch to be accepted (installs daemon, units,
   udev rules; keeps the existing config). Then `sudo systemctl daemon-reload`
   and `sudo systemctl restart r9700-tunerd.service`.
2. `systemctl is-enabled r9700-tunerd.service` must print `enabled`;
   `systemctl is-active lactd` must print `inactive`.
3. `sudo tests/hw/r9700-hwtest.py cycles --count 2` must PASS on the installed
   build (proves the build before trusting a boot with it).

After the reboot, with no AI workload started:

    sudo tests/hw/r9700-hwtest.py reboot-check

Acceptance is the table it prints: service enabled+active, lactd inactive, no
tuner DRM handles, boot→D3cold, wake→offset restored→cap 210 W→D3cold, and no
amdgpu failures beyond the known OD re-upload warning. Keep the log file with
the branch commit id in `docs/` history (paste the table into the PR/commit).

## 4. Rollback

    sudo systemctl disable --now r9700-tunerd.service
    sudo r9700-tunerd reset            # cap back to hardware default, OD table reset
    git checkout main && sudo ./install.sh   # previous known-good build

udev rule backups from the first install are kept beside the rules with a
`.bak-<date>` suffix (see README).

## 5. Dashboard hazard test (must pass after any change to tools/r9700-ui.py)

The dashboard's live sampling must never hold the card awake. With the server
running and a client connected:

    TOK=$(journalctl --user -u r9700-ui.service --no-pager | grep -o 'TOKEN: [0-9a-f]*' | tail -1 | cut -d' ' -f2)
    (timeout 75 curl -s -N "http://127.0.0.1:7970/api/events?t=$TOK" > /tmp/sse.log &)
    python3 -c "import os,time; fd=os.open('/dev/dri/renderD128', os.O_RDWR|os.O_CLOEXEC); time.sleep(10); os.close(fd)"
    # then watch /sys/bus/pci/devices/<pci>/power/runtime_status

PASS: `runtime_status` returns to `suspended` (and `power_state` to `D3cold`)
within 30 s of the release while the SSE client stays connected, and the SSE
stream shows the sampling mode go `busy` → `idle-backoff` → `asleep`.
Measured 2026-09-04: 20 s. A 2 s fixed sensor poll fails this test (card stays
`active` at 0 % busy indefinitely).

Start the server as a transient user unit so it survives the shell:

    systemd-run --user --unit r9700-ui --working-directory=$HOME/src/r9700-tunerd --collect python3 tools/r9700-ui.py
    journalctl --user -u r9700-ui.service | grep TOKEN

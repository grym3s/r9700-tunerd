#!/bin/bash
# Install Phase A udev names + Phase B tunerd. Does NOT enable the watcher.
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
STAMP=20260903-r9700

if [[ ${EUID} -ne 0 ]]; then
  echo "need root: sudo $0" >&2
  exit 1
fi

backup() {
  local f="$1"
  if [[ -f "$f" && ! -f "${f}.bak-${STAMP}" ]]; then
    cp -a "$f" "${f}.bak-${STAMP}"
    echo "backed up $f -> ${f}.bak-${STAMP}"
  fi
}

backup /etc/udev/rules.d/99-amd-gpu-paths.rules
backup /etc/udev/rules.d/99-amd-igpu.rules

install -d /usr/local/sbin /usr/local/share/doc/r9700-tunerd /etc/systemd/system
install -m 0755 "$SRC/r9700-tunerd" /usr/local/sbin/r9700-tunerd
install -m 0644 "$SRC/README.md" /usr/local/share/doc/r9700-tunerd/README.md
install -m 0644 "$SRC/systemd/r9700-tunerd.service" /etc/systemd/system/r9700-tunerd.service
install -m 0644 "$SRC/systemd/r9700-tunerd-apply.service" /etc/systemd/system/r9700-tunerd-apply.service
install -m 0644 "$SRC/udev/99-amd-gpu-paths.rules" /etc/udev/rules.d/99-amd-gpu-paths.rules
install -m 0644 "$SRC/udev/99-amd-igpu.rules" /etc/udev/rules.d/99-amd-igpu.rules

if [[ ! -f /etc/r9700-tunerd.conf ]]; then
  install -m 0644 "$SRC/r9700-tunerd.conf" /etc/r9700-tunerd.conf
else
  install -m 0644 "$SRC/r9700-tunerd.conf" /etc/r9700-tunerd.conf.new
  echo "kept existing /etc/r9700-tunerd.conf (new copy at .new)"
fi

udevadm control --reload-rules
udevadm trigger --subsystem-match=drm --action=add
systemctl daemon-reload
# Explicitly do not enable watcher until Phase A/B tests pass.
systemctl disable r9700-tunerd.service >/dev/null 2>&1 || true

sleep 0.5
echo "=== /dev/dri after udev ==="
ls -l /dev/dri/r9700 /dev/dri/amd-igpu /dev/dri/strix-halo /dev/dri/card* 2>/dev/null || ls -l /dev/dri
echo "install complete; watcher NOT enabled"

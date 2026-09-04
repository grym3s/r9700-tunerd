#!/usr/bin/env bash
# install-app.sh — user-level install of the R9700 Tuner app (no root).
# Idempotent.  --uninstall reverses it.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_PY="${REPO_ROOT}/tools/r9700-app.py"
DESKTOP_SRC="${REPO_ROOT}/contrib/r9700-tuner.desktop"
ICON_SRC="${REPO_ROOT}/contrib/r9700-tuner.svg"

BIN_DIR="${HOME}/.local/bin"
DESKTOP_DIR="${HOME}/.local/share/applications"
ICON_DIR="${HOME}/.local/share/icons/hicolor/scalable/apps"

BIN_TARGET="${BIN_DIR}/r9700-tuner"
DESKTOP_TARGET="${DESKTOP_DIR}/r9700-tuner.desktop"
ICON_TARGET="${ICON_DIR}/r9700-tuner.svg"

uninstall() {
    rm -f "${BIN_TARGET}" "${DESKTOP_TARGET}" "${ICON_TARGET}"
    if command -v update-desktop-database &>/dev/null; then
        update-desktop-database "${DESKTOP_DIR}" 2>/dev/null || true
    fi
    echo "Uninstalled r9700-tuner."
}

if [[ "${1:-}" == "--uninstall" ]]; then
    uninstall
    exit 0
fi

# ── install ──────────────────────────────────────────────────────────────────

mkdir -p "${BIN_DIR}" "${DESKTOP_DIR}" "${ICON_DIR}"

# 3-line bash launcher with the absolute repo path baked in
cat > "${BIN_TARGET}" <<LAUNCHER
#!/bin/bash
# r9700-tuner launcher (installed by install-app.sh)
exec python3 "${APP_PY}" "\$@"
LAUNCHER
chmod +x "${BIN_TARGET}"

# Desktop entry — bake the absolute launcher path in: app launchers under
# Hyprland/Omarchy do not necessarily have ~/.local/bin on PATH.
sed "s|^Exec=.*|Exec=${BIN_TARGET}|" "${DESKTOP_SRC}" > "${DESKTOP_TARGET}"

# Icon
cp -f "${ICON_SRC}" "${ICON_TARGET}"

# Refresh the desktop database (best-effort)
if command -v update-desktop-database &>/dev/null; then
    update-desktop-database "${DESKTOP_DIR}" 2>/dev/null || true
fi

echo "Installed r9700-tuner."
echo "  launcher : ${BIN_TARGET}"
echo "  desktop  : ${DESKTOP_TARGET}"
echo "  icon     : ${ICON_TARGET}"
echo ""
echo "Run: r9700-tuner   (or pick it from your application menu)"

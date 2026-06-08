#!/usr/bin/env bash
# update.sh — Bubble update script
# Usage: ./update.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Colours
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'
info() { echo -e "${GREEN}▶${NC} $*"; }
warn() { echo -e "${YELLOW}⚠${NC} $*"; }
die() {
  echo -e "${RED}✗${NC} $*" >&2
  exit 1
}

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
DAEMON_LIB="$HOME/.local/lib/bubble"
DAEMON_SRC="$SCRIPT_DIR/daemon/bubble-daemon.py"
SERVICE_SRC="$SCRIPT_DIR/daemon/bubble.service"
SERVICE_DST="$HOME/.config/systemd/user/bubble.service"
EXT_SRC="$SCRIPT_DIR/gnome-extension/bubble@bubble"
EXT_DST="$HOME/.local/share/gnome-shell/extensions/bubble@bubble"

# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------
info "Installing daemon to $DAEMON_LIB…"
mkdir -p "$DAEMON_LIB"
cp "$DAEMON_SRC" "$DAEMON_LIB/bubble-daemon.py"
chmod +x "$DAEMON_LIB/bubble-daemon.py"

# ---------------------------------------------------------------------------
# GNOME extension
# ---------------------------------------------------------------------------
info "Installing GNOME extension…"
mkdir -p "$EXT_DST"
cp -r "$EXT_SRC/." "$EXT_DST/"

# Compile GSettings schema
info "Compiling GSettings schemas…"
glib-compile-schemas "$EXT_DST/schemas/"

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo -e "${GREEN}✔ Installation complete!${NC}"
echo ""
echo "  Next steps:"
echo "    1. Restart GNOME Shell (X11: Alt+F2 → 'r'  |  Wayland: log out & back in)"
echo "    2. Enable the extension:"
echo "       gnome-extensions enable bubble@bubble"
echo ""
echo "  Keyboard shortcut: Super+B  (change via gnome-extensions prefs or gsettings)"
echo ""
echo "  Service status:    systemctl --user status bubble"
echo "  Service logs:      journalctl --user -u bubble -f"
echo "  Optional embed:    systemctl --user edit bubble  (set BUBBLE_EMBEDDINGS_ENABLED/BUBBLE_EMBED_MODEL)"
echo ""

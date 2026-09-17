#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$SCRIPT_DIR/.venv"

echo "🔪📁 SyncoPath Installer"
echo "========================"
echo ""

# 1. Create virtual environment
if [ ! -d "$VENV" ]; then
    echo "→ Creating virtual environment..."
    python3 -m venv "$VENV"
fi

# 2. Install dependencies
echo "→ Installing dependencies..."
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -e "$SCRIPT_DIR"

# 3. Initialize config
echo "→ Initializing config..."
"$VENV/bin/python" -m syncopath --init 2>/dev/null || true

# 4. Install systemd service
echo "→ Installing systemd service..."
mkdir -p ~/.config/systemd/user
cp "$SCRIPT_DIR/systemd/syncopath.service" ~/.config/systemd/user/
systemctl --user daemon-reload

# 5. Create log directory (for manual runs — service uses journald)
echo "→ Creating log directory /var/log/syncopath ..."
sudo install -d -m 755 -o "$(id -un)" /var/log/syncopath

# 6. Install icon (skip silently if sized PNGs not yet generated)
echo "→ Installing tray icon..."
_icons_installed=0
for size in 16 24 32 48 64 128 256; do
    src="$SCRIPT_DIR/assets/syncopath-${size}.png"
    if [ -f "$src" ]; then
        dir="$HOME/.local/share/icons/hicolor/${size}x${size}/apps"
        mkdir -p "$dir"
        cp "$src" "$dir/syncopath.png"
        _icons_installed=$((_icons_installed + 1))
    fi
done
if [ -f "$SCRIPT_DIR/assets/syncopath-icon.svg" ]; then
    mkdir -p "$HOME/.local/share/icons/hicolor/scalable/apps"
    cp "$SCRIPT_DIR/assets/syncopath-icon.svg" "$HOME/.local/share/icons/hicolor/scalable/apps/syncopath.svg"
    _icons_installed=$((_icons_installed + 1))
fi
if [ "$_icons_installed" -eq 0 ]; then
    echo "  (no icon assets found — skipping icon install)"
else
    gtk-update-icon-cache -f "$HOME/.local/share/icons/hicolor/" 2>/dev/null || true
fi

echo ""
echo "✅ Installation complete!"
echo ""
echo "Next steps:"
echo "  1. Edit config:    nano ~/.config/syncopath/config.yaml"
echo "  2. Authenticate:   $VENV/bin/python -m syncopath --auth gdrive-st"
echo "                     $VENV/bin/python -m syncopath --auth gdrive-boris"
echo "  3. Start service:  systemctl --user enable --now syncopath"
echo ""
echo "Commands:"
echo "  Status:   systemctl --user status syncopath"
echo "  Logs:     journalctl --user -u syncopath -f"
echo "  Log file: tail -f /var/log/syncopath/syncopath.log  (manual runs only)"
echo "  Stop:     systemctl --user stop syncopath"
echo "  Restart:  systemctl --user restart syncopath"

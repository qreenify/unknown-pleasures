#!/usr/bin/env bash
# Installs unknown-pleasures: a voice-reactive OSD companion for voxtype.
# Copies the script to ~/.local/bin, generates the systemd user unit, and
# enables it. Run from the repo root. Re-run any time to update.
set -euo pipefail
cd "$(dirname "$0")"

missing=()
python -c 'import gi' 2>/dev/null || missing+=("python-gobject")
python -c 'import numpy' 2>/dev/null || missing+=("python-numpy")
command -v voxtype >/dev/null || missing+=("voxtype (AUR: voxtype-bin)")
command -v pw-record >/dev/null || missing+=("pipewire (pw-record)")
layer_shell=$(ls /usr/lib*/libgtk4-layer-shell.so 2>/dev/null | head -1 || true)
[ -n "$layer_shell" ] || missing+=("gtk4-layer-shell")
if [ ${#missing[@]} -gt 0 ]; then
    echo "Missing dependencies: ${missing[*]}" >&2
    exit 1
fi

install -Dm755 unknown_pleasures.py "$HOME/.local/bin/unknown-pleasures"

install -Dm644 /dev/stdin "$HOME/.config/systemd/user/unknown-pleasures.service" <<EOF
[Unit]
Description=Unknown Pleasures — voice-reactive OSD for voxtype
PartOf=graphical-session.target
After=graphical-session.target voxtype.service

[Service]
Type=simple
ExecStart=/usr/bin/python $HOME/.local/bin/unknown-pleasures
Restart=on-failure
RestartSec=5
Environment=XDG_RUNTIME_DIR=%t
# Python bindings require preloading so layer-shell hooks GTK before the
# first Wayland surface exists (gtk4-layer-shell linking.md)
Environment=LD_PRELOAD=$layer_shell

[Install]
WantedBy=graphical-session.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now unknown-pleasures.service
systemctl --user restart unknown-pleasures.service

echo "Installed and running."
echo "Remaining manual steps (once) — see README:"
echo "  1. Disable voxtype's built-in OSD:  [osd] enabled = false  in ~/.config/voxtype/config.toml"
echo "  2. Add push-to-talk keybinds to your compositor config."

#!/usr/bin/env bash
# Zorin OS (17/18, Core/Pro/Lite) and other Ubuntu-based systems:
# installs the system packages castsync needs, then runs the normal installer.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"

if [ -r /etc/os-release ]; then
  . /etc/os-release
  echo "Detected: ${PRETTY_NAME:-unknown} (based on Ubuntu ${UBUNTU_CODENAME:-?})"
fi

echo "Installing system packages (you'll be asked for your password)..."
sudo apt update
sudo apt install -y python3 python3-venv python3-pip ffmpeg ca-certificates

python3 - <<'EOF'
import sys
if sys.version_info < (3, 9):
    sys.exit(f"Python {sys.version.split()[0]} is too old; castsync needs 3.9+.")
print(f"Python {sys.version.split()[0]} OK")
EOF
ffmpeg -hide_banner -version | head -1

"$HERE/install.sh"

# Let the hourly timer run even when nobody is logged in to the desktop.
sudo loginctl enable-linger "$USER"
echo "Linger enabled for $USER: $(loginctl show-user "$USER" -p Linger --value)"

# ~/.local/bin is only added to PATH at login if it existed then.
if ! command -v castsync >/dev/null 2>&1; then
  echo
  echo "NOTE: open a new terminal window (or run: source ~/.bashrc) so the 'castsync' command is found."
fi

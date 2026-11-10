#!/usr/bin/env bash
# Installs castsync for the current user and enables the hourly systemd timer.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
APP="$HOME/.local/share/castsync"
CONF="$HOME/.config/castsync"
UNITS="$HOME/.config/systemd/user"

for bin in python3 ffmpeg ffprobe; do
  command -v "$bin" >/dev/null || { echo "Missing $bin. Install it first, e.g.: sudo apt install python3-venv ffmpeg"; exit 1; }
done

python3 -c "import ensurepip" 2>/dev/null || {
  echo "Python's venv support is missing. Install it first: sudo apt install python3-venv"; exit 1; }

mkdir -p "$APP" "$CONF" "$UNITS"
python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --upgrade pip
"$APP/venv/bin/pip" install -q "$HERE"

if [ ! -f "$CONF/config.yaml" ]; then
  cp "$HERE/config.example.yaml" "$CONF/config.yaml"
  echo "Created $CONF/config.yaml  <- edit this"
fi
if [ ! -f "$CONF/castsync.env" ]; then
  cp "$HERE/castsync.env.example" "$CONF/castsync.env"
  chmod 600 "$CONF/castsync.env"
  echo "Created $CONF/castsync.env <- put your secrets here"
fi

cp "$HERE"/systemd/castsync*.service "$HERE"/systemd/castsync*.timer "$UNITS/"
mkdir -p "$HOME/.local/bin"
ln -sf "$APP/venv/bin/castsync" "$HOME/.local/bin/castsync"
install -m 755 "$HERE/uninstall.sh" "$APP/uninstall.sh"
ln -sf "$APP/uninstall.sh" "$HOME/.local/bin/castsync-uninstall"

# Make sure ~/.local/bin is on PATH in every new terminal. Ubuntu/Zorin only add it
# at login, and only if the folder already existed then.
MARK="# added by castsync installer"
if ! grep -qF "$MARK" "$HOME/.bashrc" 2>/dev/null; then
  printf '\n%s\ncase ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;; esac\n' \
    "$MARK" >> "$HOME/.bashrc"
fi

systemctl --user daemon-reload
systemctl --user enable --now castsync-update.timer
# Keep user timers running when you're not logged in
loginctl enable-linger "$USER" 2>/dev/null || echo "Note: run 'sudo loginctl enable-linger $USER' so the timer runs while logged out."

cat <<EOF

Installed. Open a NEW terminal window so the castsync command is found
(or run:  source ~/.bashrc). Next steps:
  1. Edit $CONF/config.yaml and $CONF/castsync.env
  2. castsync auth restream          # one-time browser sign-in
  3. castsync test-email
  4. castsync run --dry-run          # see what it would do
  5. systemctl --user enable --now castsync.timer
     (logs: journalctl --user -u castsync -f   or   ~/.local/share/castsync/castsync.log)

To remove castsync later:  castsync-uninstall   (or ./uninstall.sh from this folder)
EOF

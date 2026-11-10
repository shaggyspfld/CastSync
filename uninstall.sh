#!/usr/bin/env bash
# Uninstall castsync - this version and any earlier one.
#
#   ./uninstall.sh            remove the program; ask about settings, history and kept audio
#   ./uninstall.sh --purge    remove everything, including settings, history and kept audio
#   ./uninstall.sh --keep-data  remove the program only; keep settings/history/audio (no questions)
#   add --yes to answer "yes" to every question (for scripts)
#
# Also installed as:  castsync-uninstall   (and `castsync uninstall`)
set -uo pipefail
ME="$(id -un)"

PURGE=0; KEEP=0; YES=0
for a in "$@"; do
  case "$a" in
    --purge) PURGE=1 ;;
    --keep-data|--keep-config) KEEP=1 ;;
    -y|--yes) YES=1 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "Unknown option: $a (try --help)"; exit 1 ;;
  esac
done

APP="$HOME/.local/share/castsync"
CONF="$HOME/.config/castsync"
BIN="$HOME/.local/bin"
removed=()

ask() {  # ask "question" -> 0 for yes
  [ "$YES" = 1 ] && return 0
  [ -t 0 ] || return 1          # not interactive: answer no
  read -r -p "$1 [y/N] " ans
  [[ "$ans" =~ ^[Yy] ]]
}

# Read a (non-commented) "key: value" from config.yaml, expanding ~
conf_value() {
  [ -f "$CONF/config.yaml" ] || return 0
  grep -E "^[[:space:]]*$1:[[:space:]]*[^#[:space:]]" "$CONF/config.yaml" | head -1 \
    | sed -E "s/^[[:space:]]*$1:[[:space:]]*//; s/[[:space:]]+#.*$//; s/^[\"']//; s/[\"']$//; s|^~|$HOME|"
}
STATE_DIR="$(conf_value state_dir)"; STATE_DIR="${STATE_DIR:-$APP}"
WORK_DIR="$(conf_value work_dir)";   WORK_DIR="${WORK_DIR:-$STATE_DIR/work}"
KEEP_DIR="$(conf_value keep_dir)"

echo "Uninstalling castsync..."

# 1. Stop and remove systemd timers/services (user level, any version / name)
if command -v systemctl >/dev/null 2>&1; then
  for dir in "$HOME/.config/systemd/user" "$HOME/.local/share/systemd/user"; do
    for unit in "$dir"/castsync*.timer "$dir"/castsync*.service; do
      [ -e "$unit" ] || continue
      name="$(basename "$unit")"
      systemctl --user disable --now "$name" >/dev/null 2>&1
      rm -f "$unit"; removed+=("systemd unit $name")
    done
  done
  systemctl --user daemon-reload >/dev/null 2>&1
  systemctl --user reset-failed 'castsync*' >/dev/null 2>&1
  # system-wide units (only if someone installed it that way by hand)
  sys_units=$(ls /etc/systemd/system/castsync*.service /etc/systemd/system/castsync*.timer 2>/dev/null)
  if [ -n "$sys_units" ]; then
    echo "Found system-wide castsync units:"; echo "$sys_units"
    if ask "Remove them (needs sudo)?"; then
      for u in $sys_units; do sudo systemctl disable --now "$(basename "$u")" >/dev/null 2>&1; sudo rm -f "$u"; removed+=("system unit $(basename "$u")"); done
      sudo systemctl daemon-reload
    fi
  fi
fi

# 2. Cron entries (README offers cron as an alternative to the timer)
if command -v crontab >/dev/null 2>&1 && crontab -l 2>/dev/null | grep -q castsync; then
  crontab -l 2>/dev/null | grep -v castsync | crontab -
  removed+=("cron entries mentioning castsync")
fi

# 3. A run started by hand may still be going (the timer's run was stopped above).
#    Detected via castsync's own lock file - nothing is killed.
LOCK="$STATE_DIR/castsync.lock"
if [ -f "$LOCK" ] && command -v flock >/dev/null 2>&1 && ! flock -n "$LOCK" true 2>/dev/null; then
  echo "Note: a castsync run is still in progress (started by hand); it stops on its own when done."
fi

# 4. Program files (this and earlier installers)
for f in "$BIN/castsync" "$BIN/castsync-uninstall"; do
  if [ -e "$f" ] || [ -L "$f" ]; then rm -f "$f"; removed+=("$f"); fi
done
[ -d "$APP/venv" ] && rm -rf "$APP/venv" && removed+=("$APP/venv (program + yt-dlp)")
[ -f "$APP/uninstall.sh" ] && rm -f "$APP/uninstall.sh"
for d in "$WORK_DIR" "$APP/work"; do
  [ -d "$d" ] && rm -rf "$d" && removed+=("$d (temporary/working audio)")
done

# Earlier manual installs: pip --user, pipx, /usr/local/bin
if command -v pipx >/dev/null 2>&1 && \
   { [ -d "$HOME/.local/share/pipx/venvs/castsync" ] || [ -d "$HOME/.local/pipx/venvs/castsync" ]; }; then
  pipx uninstall castsync >/dev/null 2>&1 && removed+=("pipx package castsync")
fi
if python3 -m pip show castsync >/dev/null 2>&1; then
  loc="$(python3 -m pip show castsync 2>/dev/null | sed -n 's/^Location: //p')"
  if [[ "$loc" == "$HOME"* ]]; then
    python3 -m pip uninstall -y castsync >/dev/null 2>&1 \
      || python3 -m pip uninstall -y --break-system-packages castsync >/dev/null 2>&1
    removed+=("pip --user package castsync")
  else
    echo "castsync is also installed system-wide in $loc."
    if ask "Remove it (needs sudo)?"; then
      sudo python3 -m pip uninstall -y castsync >/dev/null 2>&1 \
        || sudo python3 -m pip uninstall -y --break-system-packages castsync >/dev/null 2>&1
      removed+=("system pip package castsync")
    fi
  fi
fi
if [ -e /usr/local/bin/castsync ]; then
  if ask "Remove /usr/local/bin/castsync (needs sudo)?"; then
    sudo rm -f /usr/local/bin/castsync && removed+=("/usr/local/bin/castsync")
  fi
fi

# PATH line the installer added to ~/.bashrc
if [ -f "$HOME/.bashrc" ] && grep -qF "# added by castsync installer" "$HOME/.bashrc"; then
  sed -i '/# added by castsync installer/,+1d' "$HOME/.bashrc"
  removed+=("PATH line in ~/.bashrc")
fi

# 5. Your data: settings, history/tokens/logs/reports, kept audio
remove_data() {
  [ -d "$CONF" ] && rm -rf "$CONF" && removed+=("$CONF (settings and secrets)")
  for d in "$STATE_DIR" "$APP"; do
    [ -d "$d" ] && rm -rf "$d" && removed+=("$d (history, sign-in tokens, logs, reports)")
  done
}
kept_msgs=()
if [ "$PURGE" = 1 ]; then
  remove_data
  if [ -n "$KEEP_DIR" ] && [ -d "$KEEP_DIR" ]; then rm -rf "$KEEP_DIR"; removed+=("$KEEP_DIR (kept audio)"); fi
elif [ "$KEEP" = 1 ]; then
  kept_msgs+=("Settings: $CONF" "History/logs/reports: $STATE_DIR")
  [ -n "$KEEP_DIR" ] && kept_msgs+=("Kept audio: $KEEP_DIR")
else
  if [ -d "$CONF" ] || [ -d "$STATE_DIR" ]; then
    if ask "Also delete your settings, history, sign-in tokens, logs and reports?"; then
      remove_data
    else
      kept_msgs+=("Settings: $CONF" "History/logs/reports: $STATE_DIR")
    fi
  fi
  if [ -n "$KEEP_DIR" ] && [ -d "$KEEP_DIR" ]; then
    if ask "Delete the audio castsync kept in $KEEP_DIR?"; then
      rm -rf "$KEEP_DIR"; removed+=("$KEEP_DIR (kept audio)")
    else
      kept_msgs+=("Kept audio: $KEEP_DIR")
    fi
  fi
fi
# tidy now-empty parent folder
[ -d "$APP" ] && [ -z "$(ls -A "$APP")" ] && rmdir "$APP"

echo
if [ ${#removed[@]} -eq 0 ]; then
  echo "Nothing to remove - castsync doesn't appear to be installed."
else
  echo "Removed:"; printf '  - %s\n' "${removed[@]}"
fi
if [ ${#kept_msgs[@]} -gt 0 ]; then
  echo; echo "Kept (delete by hand or run with --purge):"; printf '  - %s\n' "${kept_msgs[@]}"
fi
cat <<'EOF'

Not touched (other programs may need them):
  - system packages ffmpeg / python3-venv  (remove with: sudo apt remove ffmpeg)
  - "linger" for your user                 (turn off with: sudo loginctl disable-linger $(id -un))
  - your Restream app / Transistor API key / email app password (revoke them on those sites
    if you won't use castsync again)
EOF

#!/usr/bin/env bash
# Install a user desktop entry for the unpacked Linux build.
# Run from the release folder (next to "HLS Downloader") or from the repo root.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

find_app_dir() {
  local candidate
  for candidate in \
    "$SCRIPT_DIR" \
    "$SCRIPT_DIR/.." \
    "$SCRIPT_DIR/../.." \
    "$PWD" \
    "$PWD/dist/HLS Downloader"
  do
    if [[ -x "$candidate/HLS Downloader" ]]; then
      cd "$candidate" && pwd
      return 0
    fi
  done
  return 1
}

APP_DIR="$(find_app_dir)" || {
  echo "Executable \"HLS Downloader\" not found." >&2
  echo "Unpack the Linux release zip, cd into it, then run: ./install-desktop.sh" >&2
  exit 1
}

BIN="$APP_DIR/HLS Downloader"
ICON_SRC=""
for candidate in "$APP_DIR/app.png" "$APP_DIR/_internal/app.png"; do
  if [[ -f "$candidate" ]]; then
    ICON_SRC="$candidate"
    break
  fi
done

ICON_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/256x256/apps"
APP_MENU="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$ICON_DIR" "$APP_MENU"

if [[ -n "$ICON_SRC" ]]; then
  cp -f "$ICON_SRC" "$ICON_DIR/hls-downloader.png"
  ICON_NAME=hls-downloader
else
  ICON_NAME=application-x-executable
fi

# Desktop Entry Spec: quote paths that contain spaces.
desktop_quote() {
  printf '"%s"' "${1//\"/\\\"}"
}

DESKTOP="$APP_MENU/hls-downloader.desktop"
cat >"$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=HLS Downloader
Name[ru]=HLS Загрузчик
Comment=Download HLS / page video to MP4
Comment[ru]=Скачивание HLS и видео со страниц в MP4
Exec=$(desktop_quote "$BIN")
Path=$(desktop_quote "$APP_DIR")
Icon=$ICON_NAME
Terminal=false
Categories=AudioVideo;Network;Video;
StartupNotify=true
StartupWMClass=Hls-Downloader
MimeType=application/vnd.apple.mpegurl;application/x-mpegURL;audio/mpegurl;audio/x-mpegurl;
Keywords=HLS;m3u8;download;video;youtube;
EOF
chmod +x "$DESKTOP"

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$APP_MENU" >/dev/null 2>&1 || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -f -t "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor" >/dev/null 2>&1 || true
fi

echo "Installed desktop entry: $DESKTOP"
echo "Launch from the app menu or: $BIN"

#!/usr/bin/env bash
# Build an x86_64 AppImage from the PyInstaller onedir output.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SRC="${1:-$ROOT/dist/HLS Downloader}"
OUT="${2:-$ROOT/HLS-Downloader-x86_64.AppImage}"
APPDIR="${ROOT}/build/AppDir"

if [[ ! -x "$SRC/HLS Downloader" ]]; then
  echo "PyInstaller output not found: $SRC/HLS Downloader" >&2
  exit 1
fi

rm -rf "$APPDIR"
mkdir -p \
  "$APPDIR/usr/bin" \
  "$APPDIR/usr/lib/hls-downloader" \
  "$APPDIR/usr/share/applications" \
  "$APPDIR/usr/share/icons/hicolor/256x256/apps"

# Keep the whole onedir tree (ffmpeg, yt-dlp, _internal, …).
cp -a "$SRC"/. "$APPDIR/usr/lib/hls-downloader/"
chmod +x "$APPDIR/usr/lib/hls-downloader/HLS Downloader"

ICON_SRC="$ROOT/app.png"
if [[ -f "$APPDIR/usr/lib/hls-downloader/app.png" ]]; then
  ICON_SRC="$APPDIR/usr/lib/hls-downloader/app.png"
elif [[ -f "$APPDIR/usr/lib/hls-downloader/_internal/app.png" ]]; then
  ICON_SRC="$APPDIR/usr/lib/hls-downloader/_internal/app.png"
fi
cp -f "$ICON_SRC" "$APPDIR/usr/share/icons/hicolor/256x256/apps/hls-downloader.png"
cp -f "$ICON_SRC" "$APPDIR/hls-downloader.png"

cat >"$APPDIR/AppRun" <<'EOF'
#!/bin/sh
set -e
HERE="$(dirname "$(readlink -f "$0")")"
APP="$HERE/usr/lib/hls-downloader"
cd "$APP"
exec "$APP/HLS Downloader" "$@"
EOF
chmod +x "$APPDIR/AppRun"

# Wrapper name without spaces for desktop Exec=.
cat >"$APPDIR/usr/bin/hls-downloader" <<'EOF'
#!/bin/sh
set -e
HERE="$(dirname "$(readlink -f "$0")")/../lib/hls-downloader"
cd "$HERE"
exec "$HERE/HLS Downloader" "$@"
EOF
chmod +x "$APPDIR/usr/bin/hls-downloader"

cat >"$APPDIR/hls-downloader.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=HLS Downloader
Name[ru]=HLS Загрузчик
Comment=Download HLS / page video to MP4
Comment[ru]=Скачивание HLS и видео со страниц в MP4
Exec=hls-downloader
Icon=hls-downloader
Terminal=false
Categories=AudioVideo;Network;Video;
StartupNotify=true
StartupWMClass=Hls-Downloader
MimeType=application/vnd.apple.mpegurl;application/x-mpegURL;audio/mpegurl;audio/x-mpegurl;
Keywords=HLS;m3u8;download;video;youtube;
EOF
cp -f "$APPDIR/hls-downloader.desktop" "$APPDIR/usr/share/applications/hls-downloader.desktop"

TOOL_DIR="${ROOT}/build/appimagetool"
mkdir -p "$TOOL_DIR"
TOOL="$TOOL_DIR/appimagetool-x86_64.AppImage"
if [[ ! -x "$TOOL" ]]; then
  curl -fsSL \
    "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage" \
    -o "$TOOL"
  chmod +x "$TOOL"
fi

# CI runners usually lack FUSE — extract-and-run the tool itself.
export APPIMAGE_EXTRACT_AND_RUN=1
export ARCH=x86_64
rm -f "$OUT"
"$TOOL" "$APPDIR" "$OUT"
chmod +x "$OUT"
echo "Built $OUT"

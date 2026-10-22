# HLS Downloader

Desktop app for Windows and Linux: paste a page URL or `.m3u8` playlist and download video to MP4.

Supports YouTube, VK, Rutube, Chaturbate, Pornhub, Eporner, Mixdrop/mxcontent progressive MP4, and generic HLS.

## Features

- Queue with per-item quality / audio selection
- Batch URL paste, duplicate skip, pause/resume queue, scheduled resume (`HH:MM`)
- Container (MP4/MKV/WebM), audio-only, subtitles, metadata/thumbnail embed
- Trim range, rate limit, SponsorBlock (YouTube), cookies from browser
- Site presets (YouTube / Pornhub / live / audio)
- Post-actions: open file/folder, system notification; optional minimize-to-tray
- Export/import settings and queue; update yt-dlp from Settings
- Separate thread settings for HLS and progressive MP4
- Proxy (HTTP/HTTPS/SOCKS5), cookies.txt, UA presets
- Auto-fill URL from clipboard, toast + sound on finish
- Hotkeys: Ctrl+Enter (download all), Ctrl+Shift+V (batch), Ctrl+P (pause), Ctrl+, (settings)
- Retry failed items and re-download from history
- Themes: dark / light

## Run from source

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux:
source .venv/bin/activate

pip install -r requirements.txt
# Put ffmpeg (+ yt-dlp optional) into ./bin
python hls_downloader.py
# or: ./hls-downloader.sh
```

On first use the app can download `yt-dlp` into `bin/` automatically. `ffmpeg` must be present in `bin/` (or on `PATH`).

### Arch Linux / CachyOS

Recommended packages:

```bash
sudo pacman -S --needed ffmpeg yt-dlp xdg-utils zenity
# optional:
sudo pacman -S --needed aria2 libcanberra sound-theme-freedesktop
# AppImage only:
sudo pacman -S --needed fuse2
```

On Linux the app prefers **system** `/usr/bin/ffmpeg` and `/usr/bin/yt-dlp` over bundled copies (avoids nested PyInstaller/Cryptodome extract failures).

Folder/file pickers use the system dialog when `zenity` or `kdialog` is installed.

**Wayland:** the UI runs via XWayland (Tk). That is normal; install `zenity` for native file/folder dialogs.

Settings for AppImage / read-only installs live in `~/.config/hls-downloader/`.

#### Install options (best → portable)

1. **makepkg / AUR-style package** (uses system `ffmpeg` + `yt-dlp`):

```bash
cd packaging/arch
makepkg -si
hls-downloader
```

2. **Arch AppImage** (`HLS-Downloader-arch-x86_64.AppImage` from [Releases](https://github.com/MaksBaks92/hls-downloader/releases)) — built in an Arch container for CachyOS/Arch:

```bash
sudo pacman -S --needed fuse2   # once
chmod +x HLS-Downloader-arch-x86_64.AppImage
./HLS-Downloader-arch-x86_64.AppImage
```

3. **Arch zip** (`HLS-Downloader-archlinux.zip`):

```bash
chmod +x "HLS Downloader" install-desktop.sh
./install-desktop.sh   # optional app menu entry
./"HLS Downloader"
```

4. Generic Ubuntu AppImage (`HLS-Downloader-x86_64.AppImage`) also runs on Arch if `fuse2` is installed.

From source:

```bash
sudo pacman -S --needed python python-pip tk ffmpeg yt-dlp
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
./hls-downloader.sh
```

## Build

Builds run on GitHub Actions (Windows + Linux + Arch). Locally:

```bash
# Windows (bin/ffmpeg.exe + bin/yt-dlp.exe required)
pyinstaller --noconfirm hls_downloader.spec

# Linux (bin/ffmpeg + yt-dlp required)
pyinstaller --noconfirm hls_downloader.spec
```

Output: `dist/HLS Downloader/`

GitHub Actions builds Windows and Linux artifacts on every push to `main` and on tags.

### Linux release zip / AppImage

| Artifact | Built on | Best for |
|---|---|---|
| `HLS-Downloader-arch-x86_64.AppImage` | Arch container | Arch / CachyOS |
| `HLS-Downloader-archlinux.zip` | Arch container | Arch / CachyOS |
| `HLS-Downloader-x86_64.AppImage` | Ubuntu | Most distros |
| `HLS-Downloader-linux.zip` | Ubuntu | Generic Linux |

```bash
chmod +x HLS-Downloader-arch-x86_64.AppImage   # or the Ubuntu AppImage
./HLS-Downloader-arch-x86_64.AppImage
```

After unpacking a zip:

```bash
chmod +x "HLS Downloader" install-desktop.sh
./install-desktop.sh   # optional: add app menu entry (XDG)
./"HLS Downloader"
```

## Settings

Stored next to the executable when writable; otherwise under `~/.config/hls-downloader/` (AppImage / system package).

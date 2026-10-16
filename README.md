# HLS Downloader

Desktop app for Windows and Linux: paste a page URL or `.m3u8` playlist and download video to MP4.

Supports YouTube, VK, Rutube, Chaturbate, Pornhub, Eporner, Mixdrop/mxcontent progressive MP4, and generic HLS.

## Features

- Queue with per-item quality / audio selection
- Separate thread settings for HLS and progressive MP4
- Proxy (HTTP/HTTPS/SOCKS5), cookies.txt, UA presets
- Auto-fill URL from clipboard, toast + sound on finish
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

Folder/file pickers use the system dialog when `zenity` or `kdialog` is installed
(`sudo pacman -S --needed zenity`).

On Arch/CachyOS, installing system `yt-dlp` is recommended — the AppImage will prefer it
over the bundled binary (`sudo pacman -S --needed yt-dlp`).

Prefer the **Arch** CI artifact (built in `archlinux` container, works on CachyOS):

1. Download `HLS-Downloader-archlinux.zip` from [Actions](https://github.com/MaksBaks92/hls-downloader/actions) or Releases
2. Unpack and run:

```bash
chmod +x "HLS Downloader" install-desktop.sh
./install-desktop.sh   # optional app menu entry
./"HLS Downloader"
```

Or build a package locally with `makepkg`:

```bash
cd packaging/arch
makepkg -si
hls-downloader
```

From source:

```bash
sudo pacman -S --needed python python-pip tk ffmpeg
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
./hls-downloader.sh
```

## Build

Builds run on GitHub Actions (Windows + Linux). Locally:

```bash
# Windows (bin/ffmpeg.exe + bin/yt-dlp.exe required)
pyinstaller --noconfirm hls_downloader.spec

# Linux (bin/ffmpeg + bin/yt-dlp required)
pyinstaller --noconfirm hls_downloader.spec
```

Output: `dist/HLS Downloader/`

GitHub Actions builds Windows and Linux artifacts on every push to `main` and on tags.

### Linux release zip / AppImage

**AppImage** (recommended on CachyOS / Arch / most distros):

```bash
chmod +x HLS-Downloader-x86_64.AppImage
./HLS-Downloader-x86_64.AppImage
```

After unpacking `HLS-Downloader-linux.zip`:

```bash
chmod +x "HLS Downloader" install-desktop.sh
./install-desktop.sh   # optional: add app menu entry (XDG)
./"HLS Downloader"
```

The generic `HLS-Downloader-linux.zip` and AppImage are built on Ubuntu. For a native Arch tree use `HLS-Downloader-archlinux.zip`.

## Settings

Stored next to the executable / script as `settings.json`.

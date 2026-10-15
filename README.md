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

### Arch Linux (from source)

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

### Linux release zip

After unpacking `HLS-Downloader-linux.zip`:

```bash
chmod +x "HLS Downloader" install-desktop.sh
./install-desktop.sh   # optional: add app menu entry (XDG)
./"HLS Downloader"
```

Works on Arch and other glibc x86_64 distros (artifact is built on Ubuntu).

## Settings

Stored next to the executable / script as `settings.json`.

"""Desktop app: paste a page URL or HLS playlist and download the video to MP4."""

from __future__ import annotations

import base64
import hashlib
import html
import http.cookiejar
import json
import os
import queue
import re
import shutil
import ssl
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
YTDLP_BIN = "yt-dlp.exe" if IS_WINDOWS else "yt-dlp"
FFMPEG_BIN = "ffmpeg.exe" if IS_WINDOWS else "ffmpeg"
ARIA2_BIN = "aria2c.exe" if IS_WINDOWS else "aria2c"


def runtime_dir() -> Path:
    """Writable folder next to the .exe (or the script)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_dir() -> Path:
    """Bundled resources: ffmpeg, yt-dlp, icon."""
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = runtime_dir()
RESOURCE_DIR = resource_dir()
SETTINGS_PATH = APP_DIR / "settings.json"
HISTORY_PATH = APP_DIR / "history.json"
ARIA2_PATH = APP_DIR / "bin" / ARIA2_BIN
FFMPEG_PATH = RESOURCE_DIR / "bin" / FFMPEG_BIN
YTDLP_PATH = RESOURCE_DIR / "bin" / YTDLP_BIN
ICON_PATH = RESOURCE_DIR / "app.ico"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
USER_AGENT_PRESETS: dict[str, str | None] = {
    "Chrome (Windows)": DEFAULT_UA,
    "Chrome (macOS)": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    ),
    "Firefox (Windows)": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0"
    ),
    "Edge (Windows)": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0"
    ),
    "Safari (macOS)": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/18.0 Safari/605.1.15"
    ),
    "Chrome (Android)": (
        "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.7339.155 Mobile Safari/537.36"
    ),
    "Safari (iPhone)": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
    ),
    "Пустой (empty)": "",
    "Свой (custom)": None,
}
UA_PRESET_CUSTOM = "Свой (custom)"
DEFAULT_THREADS = 24
DEFAULT_DIRECT_THREADS = 12
MAX_THREADS = 64
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
PROXY_SCHEMES = ("http", "https", "socks5", "socks5h")
_DOWNLOAD_CTX = threading.local()
ATTR_RE = re.compile(r'([A-Z0-9-]+)=("(?:\\.|[^"])*"|[^,]*)')
M3U8_URL_RE = re.compile(
    r"""(?P<url>https?://[^\s"'<>\\]+?\.m3u8(?:\?[^\s"'<>\\]*)?)""",
    re.IGNORECASE,
)
ESCAPED_M3U8_RE = re.compile(
    r"""https?:\\?/\\?/[^\s"'<>\\]+?\.m3u8(?:\\?/[^\s"'<>\\]*)?(?:\?[^\s"'<>\\]*)?""",
    re.IGNORECASE,
)
GENERIC_NAMES = {
    "index.m3u8",
    "playlist.m3u8",
    "master.m3u8",
    "chunklist.m3u8",
    "prog_index.m3u8",
    "stream.m3u8",
}
YOUTUBE_HOSTS = (
    "youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
    "googlevideo.com",
    "youtubekids.com",
)
# Sites where browser HLS is incomplete/fragile — download via yt-dlp instead.
YTDLP_NATIVE_HOSTS = YOUTUBE_HOSTS + (
    "rutube.ru",
    "vk.com",
    "vk.ru",
    "vkvideo.ru",
    "m.vk.com",
    "chaturbate.com",
    # Signed phncdn HLS often 410s after probe; yt-dlp refreshes formats cleanly.
    "pornhub.com",
    "pornhub.org",
    "pornhub.net",
    "pornhubpremium.com",
    # Page HTML exposes gvideo.eporner.com stubs that 403; real CDN needs yt-dlp cookies.
    "eporner.com",
)
# Fast clients omit multi-audio tracks; use only for single default audio downloads.
YTDLP_YT_FAST_CLIENT = "youtube:player_client=android,tv,mweb"
# Prefer AAC/m4a — opus/webm DASH often 403s when ffmpeg pulls --download-sections.
YTDLP_YT_FORMAT_BEST = "bv*+ba[ext=m4a]/bv*+ba/b"
YTDLP_YT_FORMAT_HEIGHT = "bv*[height<={height}]+ba[ext=m4a]/bv*[height<={height}]+ba/b"


class PlaylistError(Exception):
    pass


@dataclass
class Variant:
    url: str
    bandwidth: int = 0
    resolution: str = ""
    codecs: str = ""
    audio_group: str = ""

    def label(self) -> str:
        parts = []
        if self.resolution:
            parts.append(self.resolution)
        if self.bandwidth > 0:
            parts.append(human_bitrate(self.bandwidth))
        if self.codecs:
            parts.append(self.codecs)
        return " · ".join(parts) if parts else self.url


@dataclass
class AudioTrack:
    url: str
    name: str = ""
    group_id: str = ""
    language: str = ""
    default: bool = False

    def label(self) -> str:
        title = self.name or self.language or "Аудио"
        if self.language and self.language.lower() not in title.lower():
            title = f"{title} ({self.language})"
        if self.default:
            title += " · по умолчанию"
        return title


@dataclass
class Segment:
    url: str
    duration: float


@dataclass
class Playlist:
    source_url: str
    is_master: bool
    is_vod: bool
    duration: float | None
    variants: list[Variant] = field(default_factory=list)
    audios: list[AudioTrack] = field(default_factory=list)
    segments: list[Segment] = field(default_factory=list)
    segment_count: int = 0


@dataclass
class Resolved:
    video_url: str
    audio_url: str | None
    is_vod: bool
    duration: float | None
    label: str
    headers: dict[str, str] = field(default_factory=dict)
    temp_files: list[Path] = field(default_factory=list)
    segments: list[Segment] = field(default_factory=list)
    browser: bool = False
    page_referer: str = ""


@dataclass
class PageResolve:
    playlist_url: str
    title: str | None = None
    referer: str = ""
    browser: bool = False
    thumbnail: str | None = None


def clamp_threads(value: int | str | None) -> int:
    try:
        if value is None or value == "":
            threads = DEFAULT_THREADS
        else:
            threads = int(value)
    except (TypeError, ValueError):
        threads = DEFAULT_THREADS
    return max(1, min(MAX_THREADS, threads))


def human_bitrate(bps: int) -> str:
    if bps >= 1_000_000:
        return f"{bps / 1_000_000:.1f} Мбит/с"
    if bps >= 1_000:
        return f"{bps / 1_000:.0f} кбит/с"
    return f"{bps} бит/с"


def fmt_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return ""
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def fmt_size(num: float | None) -> str:
    if num is None or num < 0:
        return ""
    value = float(num)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if value < 1024 or unit == "ГБ":
            if unit == "Б":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return ""


def sanitize_filename(name: str) -> str:
    cleaned = "".join(ch for ch in name if ch not in '<>:"/\\|?*' and ord(ch) >= 32)
    cleaned = cleaned.strip().rstrip(". ")
    return cleaned or "video"


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 1000):
        candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise PlaylistError("Слишком много файлов с таким именем")


def normalize_proxy_url(raw: str, default_scheme: str = "http") -> str:
    """Accept host:port or full URL; return canonical http/https/socks5 URL."""
    text = (raw or "").strip().strip('"').strip("'")
    if not text:
        return ""
    scheme_hint = (default_scheme or "http").lower().strip()
    if scheme_hint == "socks":
        scheme_hint = "socks5"
    if scheme_hint not in PROXY_SCHEMES:
        scheme_hint = "http"
    if "://" not in text:
        text = f"{scheme_hint}://{text}"
    parts = urllib.parse.urlsplit(text)
    scheme = (parts.scheme or scheme_hint).lower()
    if scheme == "socks":
        scheme = "socks5"
    if scheme not in PROXY_SCHEMES:
        raise PlaylistError(
            f"Неподдерживаемый тип прокси «{scheme}». Доступны: http, https, socks5."
        )
    host = parts.hostname
    if not host:
        raise PlaylistError("Укажите хост прокси, например 127.0.0.1:1080")
    auth = ""
    if parts.username is not None:
        user = urllib.parse.quote(parts.username, safe="")
        password = urllib.parse.quote(parts.password or "", safe="")
        auth = f"{user}:{password}@" if parts.password is not None else f"{user}@"
    port = f":{parts.port}" if parts.port else ""
    return f"{scheme}://{auth}{host}{port}"


def proxy_display(proxy: str) -> str:
    if not proxy:
        return ""
    parts = urllib.parse.urlsplit(proxy)
    host = parts.hostname or "?"
    port = f":{parts.port}" if parts.port else ""
    auth = " · login" if parts.username else ""
    return f"{parts.scheme}://{host}{port}{auth}"


def requests_proxies(proxy: str) -> dict[str, str] | None:
    if not proxy:
        return None
    return {"http": proxy, "https": proxy, "all": proxy}


def proxy_env(proxy: str) -> dict[str, str]:
    env = os.environ.copy()
    if not proxy:
        return env
    for key in (
        "ALL_PROXY",
        "all_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "http_proxy",
        "https_proxy",
        "SOCKS_PROXY",
        "socks_proxy",
    ):
        env[key] = proxy
    return env


def current_proxy() -> str:
    return str(getattr(_DOWNLOAD_CTX, "proxy", "") or "")


class use_download_proxy:
    """Set proxy/cookies for the current worker thread (BrowserClient / ffmpeg / yt-dlp)."""

    def __init__(self, proxy: str, cookies: str = "") -> None:
        self.proxy = proxy
        self.cookies = cookies
        self._prev_proxy = ""
        self._prev_cookies = ""

    def __enter__(self) -> str:
        self._prev_proxy = current_proxy()
        self._prev_cookies = str(getattr(_DOWNLOAD_CTX, "cookies", "") or "")
        _DOWNLOAD_CTX.proxy = self.proxy
        _DOWNLOAD_CTX.cookies = self.cookies
        return self.proxy

    def __exit__(self, exc_type, exc, tb) -> None:
        _DOWNLOAD_CTX.proxy = self._prev_proxy
        _DOWNLOAD_CTX.cookies = self._prev_cookies


def detect_proxy_scheme(raw: str) -> str:
    text = (raw or "").strip()
    if "://" in text:
        scheme = urllib.parse.urlsplit(text).scheme.lower()
        if scheme == "socks":
            return "socks5"
        if scheme in PROXY_SCHEMES:
            return scheme
    return "http"


def resolve_user_agent(preset: str, custom: str, spoof: bool = False) -> str:
    """Pick UA from preset / custom / random spoof pool (OVD-style)."""
    import random

    if spoof:
        pool = [value for key, value in USER_AGENT_PRESETS.items() if isinstance(value, str) and value]
        return random.choice(pool) if pool else DEFAULT_UA
    if preset == UA_PRESET_CUSTOM or preset not in USER_AGENT_PRESETS:
        text = (custom or "").strip()
        return text
    value = USER_AGENT_PRESETS.get(preset)
    if value is None:
        return (custom or "").strip()
    return value


def detect_ua_preset(user_agent: str) -> str:
    text = (user_agent or "").strip()
    for name, value in USER_AGENT_PRESETS.items():
        if isinstance(value, str) and value == text:
            return name
    if not text:
        return "Пустой (empty)"
    return UA_PRESET_CUSTOM


def load_netscape_cookie_header(path: Path) -> str:
    """Parse cookies.txt (Netscape) into a Cookie request header."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise PlaylistError(f"Не удалось прочитать cookies: {exc}") from exc
    pairs: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_") :]
        elif line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        name, value = parts[5], parts[6]
        if name:
            pairs.append(f"{name}={value}")
    return "; ".join(pairs)


def apply_cookies_file(headers: dict[str, str], cookies_file: str) -> dict[str, str]:
    path_text = (cookies_file or "").strip()
    if not path_text:
        return headers
    path = Path(path_text)
    if not path.is_file():
        raise PlaylistError(f"Файл cookies не найден: {path}")
    cookie_header = load_netscape_cookie_header(path)
    if cookie_header:
        headers = dict(headers)
        headers["Cookie"] = cookie_header
    return headers


def ytdlp_cookies_args(cookies_file: str) -> list[str]:
    path_text = (cookies_file or "").strip()
    if not path_text:
        return []
    path = Path(path_text)
    if not path.is_file():
        raise PlaylistError(f"Файл cookies не найден: {path}")
    return ["--cookies", str(path)]


def append_ytdlp_cookies(command: list[str], cookies_file: str = "") -> list[str]:
    """Insert --cookies before the URL (last argument)."""
    path = cookies_file or str(getattr(_DOWNLOAD_CTX, "cookies", "") or "")
    args = ytdlp_cookies_args(path)
    if not args or not command:
        return command
    return command[:-1] + args + [command[-1]]


def default_output_dir() -> Path:
    for candidate in (
        Path(r"D:\Видео"),
        Path.home() / "Videos",
        Path.home() / "Downloads",
        Path.home(),
    ):
        if candidate.exists():
            return candidate
    return Path.home()


def name_from_url(url: str) -> str:
    path = urllib.parse.unquote(urllib.parse.urlparse(url).path)
    parts = [part for part in path.split("/") if part]
    if not parts:
        return "video.mp4"
    last = parts[-1]
    if last.lower() in GENERIC_NAMES or last.lower().endswith(".m3u8"):
        parent = parts[-2] if len(parts) >= 2 else "video"
        if parent.lower() in {"hls", "live", "stream", "playlist"} and len(parts) >= 3:
            parent = parts[-3]
        last = parent
    stem = last.rsplit(".", 1)[0]
    return sanitize_filename(stem) + ".mp4"


def resolve_url(base: str, uri: str) -> str:
    """Join a playlist URI and keep the parent's query on relative links.

    Signed IPTV playlists often put the token only on the master URL.
    """
    uri = uri.strip()
    joined = urllib.parse.urljoin(base, uri)
    if uri.lower().startswith(("http://", "https://", "file:")):
        return joined
    base_query = urllib.parse.urlsplit(base).query
    parts = urllib.parse.urlsplit(joined)
    if base_query and not parts.query:
        return urllib.parse.urlunsplit(parts._replace(query=base_query))
    return joined


def parse_attrs(raw: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for match in ATTR_RE.finditer(raw):
        value = match.group(2)
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        attrs[match.group(1)] = value
    return attrs


def parse_playlist(text: str, base_url: str) -> Playlist:
    sample = text.lstrip("\ufeff")[:800].lower()
    if "<html" in sample or "<!doctype html" in sample:
        raise PlaylistError(
            "Сервер вернул HTML-страницу, а не плейлист. "
            "Проверьте ссылку или укажите Referer в «Дополнительно»."
        )
    if "#EXTM3U" not in text:
        raise PlaylistError("Это не HLS-плейлист: в тексте нет #EXTM3U.")

    variants: list[Variant] = []
    audios: list[AudioTrack] = []
    segments: list[Segment] = []
    duration = 0.0
    segment_count = 0
    pending_inf = 0.0
    expect_segment = False
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if line.startswith("#EXT-X-STREAM-INF:"):
            attrs = parse_attrs(line.split(":", 1)[1])
            cursor = index + 1
            uri = ""
            while cursor < len(lines):
                nxt = lines[cursor].strip()
                if not nxt:
                    cursor += 1
                    continue
                if nxt.startswith("#"):
                    if nxt.startswith("#EXT-X-STREAM-INF:") or nxt.startswith("#EXT-X-MEDIA:"):
                        break
                    cursor += 1
                    continue
                uri = nxt
                cursor += 1
                break
            if uri:
                try:
                    bandwidth = int(attrs.get("BANDWIDTH", "0"))
                except ValueError:
                    bandwidth = 0
                variants.append(
                    Variant(
                        url=resolve_url(base_url, uri),
                        bandwidth=bandwidth,
                        resolution=attrs.get("RESOLUTION", ""),
                        codecs=attrs.get("CODECS", ""),
                        audio_group=attrs.get("AUDIO", ""),
                    )
                )
            index = cursor
            continue
        if line.startswith("#EXT-X-MEDIA:"):
            attrs = parse_attrs(line.split(":", 1)[1])
            if attrs.get("TYPE", "").upper() == "AUDIO" and attrs.get("URI"):
                audios.append(
                    AudioTrack(
                        url=resolve_url(base_url, attrs["URI"]),
                        name=attrs.get("NAME", ""),
                        group_id=attrs.get("GROUP-ID", ""),
                        language=attrs.get("LANGUAGE", ""),
                        default=attrs.get("DEFAULT", "").upper() == "YES",
                    )
                )
        elif line.startswith("#EXTINF:"):
            raw = line.split(":", 1)[1].split(",", 1)[0].strip()
            try:
                pending_inf = float(raw)
            except ValueError:
                pending_inf = 0.0
            expect_segment = True
        elif expect_segment and line and not line.startswith("#"):
            duration += pending_inf
            segment_count += 1
            segments.append(Segment(resolve_url(base_url, line), pending_inf))
            expect_segment = False
            pending_inf = 0.0
        index += 1

    is_vod = "#EXT-X-ENDLIST" in text
    variants.sort(key=lambda item: item.bandwidth, reverse=True)
    return Playlist(
        source_url=base_url,
        is_master=bool(variants),
        is_vod=is_vod,
        duration=duration if is_vod and not variants else None,
        variants=variants,
        audios=audios,
        segments=[] if variants else segments,
        segment_count=0 if variants else segment_count,
    )


def build_headers(user_agent: str, referer: str, extra: str, cookies_file: str = "") -> dict[str, str]:
    headers = {
        "User-Agent": (user_agent or DEFAULT_UA).strip() or DEFAULT_UA,
        "Accept": "*/*",
    }
    if referer.strip():
        headers["Referer"] = referer.strip()
    for line in extra.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if key and "\n" not in value and "\r" not in value:
            headers[key] = value
    if cookies_file.strip():
        headers = apply_cookies_file(headers, cookies_file)
    return headers


URI_ATTR_RE = re.compile(r'URI="([^"]*)"')


def absolutize_playlist(text: str, base_url: str) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            lines.append("")
            continue
        if stripped.startswith("#"):
            lines.append(URI_ATTR_RE.sub(lambda match: f'URI="{resolve_url(base_url, match.group(1))}"', line.strip()))
        else:
            lines.append(resolve_url(base_url, stripped))
    return "\n".join(lines) + "\n"


def cleanup_stale_temp() -> None:
    folder = APP_DIR / "temp"
    if not folder.exists():
        return
    cutoff = time.time() - 3600
    for path in folder.glob("*.m3u8"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


class HttpClient:
    def __init__(self, headers: dict[str, str], insecure: bool) -> None:
        self.headers = dict(headers)
        self.jar = http.cookiejar.CookieJar()
        context = ssl._create_unverified_context() if insecure else ssl.create_default_context()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            urllib.request.HTTPSHandler(context=context),
        )

    def fetch_text(self, url: str) -> tuple[str, str]:
        if re.match(r"^[a-zA-Z]:[\\/]", url) or url.startswith("\\\\"):
            path = Path(url)
            return path.read_text(encoding="utf-8", errors="replace"), path.resolve().as_uri()
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme == "file":
            path = Path(urllib.request.url2pathname(parsed.path))
            return path.read_text(encoding="utf-8", errors="replace"), path.resolve().as_uri()

        request = urllib.request.Request(url, headers=self.headers)
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                with self.opener.open(request, timeout=30) as response:
                    final_url = response.geturl()
                    data = response.read(50 * 1024 * 1024)
                    charset = response.headers.get_content_charset() or "utf-8"
                return data.decode(charset, errors="replace"), final_url
            except urllib.error.HTTPError as exc:
                detail = f"Сервер ответил {exc.code}."
                if exc.code in {401, 403}:
                    detail += " Укажите Referer, User-Agent или Cookie в «Дополнительно»."
                raise PlaylistError(detail) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                if attempt == 2:
                    break
        raise PlaylistError(f"Не удалось открыть плейлист: {last_error}")

    def ffmpeg_headers(self) -> dict[str, str]:
        headers = dict(self.headers)
        if any(key.lower() == "cookie" for key in headers):
            return headers
        cookies = "; ".join(f"{cookie.name}={cookie.value}" for cookie in self.jar)
        if cookies:
            headers["Cookie"] = cookies
        return headers


def load_playlist(url: str, client: HttpClient) -> tuple[str, str, Playlist]:
    text, final_url = client.fetch_text(url)
    return text, final_url, parse_playlist(text, final_url)


def separate_audio(variant: Variant, audios: list[AudioTrack], preferred_url: str | None) -> str | None:
    """Use an external audio playlist when the variant points at one.

    CODECS may list an audio codec even when the video playlist itself has no
    audio: that codec describes the linked AUDIO group.
    """
    if not variant.audio_group:
        return None
    tracks = [track for track in audios if track.group_id == variant.audio_group and track.url]
    if not tracks:
        return None
    if preferred_url:
        for track in tracks:
            if track.url == preferred_url:
                return track.url
    defaults = [track for track in tracks if track.default]
    return (defaults or tracks)[0].url


def variant_rank(variant: Variant) -> tuple[int, int, int]:
    pixels = 0
    if "x" in variant.resolution.lower():
        width, _, height = variant.resolution.lower().partition("x")
        if width.isdigit() and height.isdigit():
            pixels = int(width) * int(height)
    codecs = variant.codecs.lower()
    aac = 1 if "mp4a" in codecs or codecs.endswith("aac") or ",aac" in codecs else 0
    return (pixels, aac, variant.bandwidth)


def variant_key(variant: Variant) -> str:
    return f"{variant.url}|{variant.audio_group}|{variant.codecs}"


def select_variant(playlist: Playlist, choice_url: str | None) -> Variant | None:
    if not playlist.variants:
        return None
    if choice_url:
        for variant in playlist.variants:
            if choice_url in {variant_key(variant), variant.url}:
                return variant
    return max(playlist.variants, key=variant_rank)


def is_image_url(url: str) -> bool:
    path = urllib.parse.unquote(urllib.parse.urlparse(url).path).lower()
    return path.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".image"))


def image_segments(playlist: Playlist) -> list[Segment]:
    if playlist.is_master or not playlist.segments:
        return []
    if is_image_url(playlist.segments[0].url):
        return playlist.segments
    return []


def playback_url(url: str) -> str:
    if url.startswith("file:"):
        return urllib.request.url2pathname(urllib.parse.urlparse(url).path)
    return url


def resolve_source(
    source_url: str,
    headers: dict[str, str],
    insecure: bool,
    choice_url: str | None,
    audio_choice: str | None,
) -> Resolved:
    client = HttpClient(headers, insecure)
    temp_files: list[Path] = []
    try:
        return _resolve_source(client, source_url, choice_url, audio_choice, temp_files)
    except Exception:
        for path in temp_files:
            path.unlink(missing_ok=True)
        raise


def _resolve_source(
    client: HttpClient,
    source_url: str,
    choice_url: str | None,
    audio_choice: str | None,
    temp_files: list[Path],
) -> Resolved:
    _text, final_url, playlist = load_playlist(source_url, client)
    if not playlist.is_master:
        frames = image_segments(playlist)
        label = "поток"
        return Resolved(
            video_url=playback_url(final_url),
            audio_url=None,
            is_vod=playlist.is_vod,
            duration=playlist.duration,
            label=label,
            headers=client.ffmpeg_headers(),
            temp_files=temp_files,
            segments=frames,
        )

    variant = select_variant(playlist, choice_url)
    assert variant is not None
    _vtext, vfinal, nested = load_playlist(variant.url, client)
    chosen = variant
    if nested.is_master:
        inner = select_variant(nested, None)
        if inner is not None:
            chosen = inner
            _vtext, vfinal, nested = load_playlist(inner.url, client)
    frames = image_segments(nested)
    audio_http = None if frames else separate_audio(chosen if chosen.audio_group else variant, playlist.audios, audio_choice)
    audio_input = None
    if audio_http:
        _atext, afinal, _audio_playlist = load_playlist(audio_http, client)
        audio_input = playback_url(afinal)
    label = variant.label()
    if frames and variant.resolution:
        label = variant.resolution
    return Resolved(
        video_url=playback_url(vfinal),
        audio_url=audio_input,
        is_vod=nested.is_vod,
        duration=nested.duration,
        label=label,
        headers=client.ffmpeg_headers(),
        temp_files=temp_files,
        segments=frames,
    )


def find_ffmpeg() -> Path | None:
    if FFMPEG_PATH.exists():
        return FFMPEG_PATH
    found = shutil.which("ffmpeg")
    if found:
        return Path(found)
    return None


def input_options(headers: dict[str, str], proxy: str, insecure: bool, live: bool) -> list[str]:
    options = [
        "-user_agent",
        headers.get("User-Agent", DEFAULT_UA),
        "-reconnect",
        "1",
        "-reconnect_streamed",
        "1",
        "-reconnect_on_network_error",
        "1",
        "-reconnect_delay_max",
        "10",
        "-rw_timeout",
        "20000000",
        "-http_persistent",
        "1",
        "-probesize",
        "20M",
        "-analyzeduration",
        "20M",
    ]
    if live:
        options += ["-seekable", "0"]
    if insecure:
        options += ["-tls_verify", "0"]
    extra = []
    for key, value in headers.items():
        if key.lower() == "user-agent":
            continue
        extra.append(f"{key}: {value}")
    if extra:
        options += ["-headers", "\r\n".join(extra) + "\r\n"]
    if proxy.strip():
        try:
            normalized = normalize_proxy_url(proxy)
        except PlaylistError:
            normalized = proxy.strip()
        scheme = urllib.parse.urlsplit(normalized).scheme.lower()
        # ffmpeg -http_proxy works for HTTP(S) proxies; SOCKS goes via process env.
        if scheme in {"http", "https"}:
            options += ["-http_proxy", normalized]
    return options


def build_ffmpeg_command(
    ffmpeg: Path,
    resolved: Resolved,
    output: Path,
    headers: dict[str, str],
    proxy: str,
    insecure: bool,
    limit_seconds: int | None,
    use_aac_bsf: bool,
) -> list[str]:
    command = [
        str(ffmpeg),
        "-hide_banner",
        "-y",
        "-loglevel",
        "warning",
        "-nostats",
        "-progress",
        "pipe:1",
        "-protocol_whitelist",
        "file,http,https,tcp,tls,crypto,data,httpproxy,subfile",
        "-allowed_extensions",
        "ALL",
        "-extension_picky",
        "0",
        "-fflags",
        "+genpts+discardcorrupt",
    ]
    command += input_options(headers, proxy, insecure, live=not resolved.is_vod)
    command += ["-i", resolved.video_url]
    if resolved.audio_url:
        command += input_options(headers, proxy, insecure, live=not resolved.is_vod)
        command += ["-i", resolved.audio_url, "-map", "0:v:0?", "-map", "1:a:0?"]
    else:
        command += ["-map", "0:v:0?", "-map", "0:a:0?"]
    if limit_seconds:
        command += ["-t", str(limit_seconds)]
    command += ["-c", "copy", "-dn"]
    if use_aac_bsf:
        command += ["-bsf:a", "aac_adtstoasc"]
    codecs = resolved.label.lower()
    if "ac-3" in codecs or "ec-3" in codecs:
        command += ["-movflags", "+delay_moov+faststart"]
    elif resolved.is_vod and not limit_seconds:
        command += ["-movflags", "+faststart"]
    else:
        command += ["-movflags", "+frag_keyframe+empty_moov+default_base_moof"]
    command.append(str(output))
    return command


def parse_progress_time(line: str) -> float | None:
    if line.startswith("out_time_us="):
        raw = line.split("=", 1)[1].strip()
        if raw.isdigit():
            return int(raw) / 1_000_000
        return None
    if line.startswith("out_time=") and not line.startswith("out_time_"):
        raw = line.split("=", 1)[1].strip()
        try:
            hours, minutes, seconds = raw.split(":")
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        except ValueError:
            return None
    return None


def run_ffmpeg(
    command: list[str],
    duration: float | None,
    cancel: threading.Event,
    events: queue.Queue,
) -> tuple[int, str, float]:
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=CREATE_NO_WINDOW,
        env=proxy_env(current_proxy()),
    )
    events.put(("proc", process))
    stderr_lines: list[str] = []
    seen_time = 0.0

    def stop_process() -> None:
        if process.poll() is not None:
            return
        try:
            if process.stdin is not None:
                process.stdin.write("q\n")
                process.stdin.flush()
        except Exception:
            pass
        try:
            process.wait(timeout=8)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            process.terminate()
            process.wait(timeout=4)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    def watch_cancel() -> None:
        while process.poll() is None:
            if cancel.is_set():
                stop_process()
                return
            time.sleep(0.2)

    threading.Thread(target=watch_cancel, daemon=True).start()

    def read_stderr() -> None:
        assert process.stderr is not None
        for line in process.stderr:
            text = line.strip()
            if text:
                stderr_lines.append(text)
                events.put(("log", text))

    thread = threading.Thread(target=read_stderr, daemon=True)
    thread.start()
    assert process.stdout is not None
    try:
        for line in process.stdout:
            if cancel.is_set():
                stop_process()
                break
            line = line.strip()
            moment = parse_progress_time(line)
            if moment is not None:
                seen_time = moment
                percent = None
                if duration and duration > 0:
                    percent = max(0.0, min(99.0, moment / duration * 100))
                events.put(("progress", {"time": moment, "percent": percent}))
            elif line.startswith("total_size="):
                raw = line.split("=", 1)[1].strip()
                if raw.isdigit():
                    events.put(("size", int(raw)))
            elif line.startswith("speed="):
                events.put(("speed", line.split("=", 1)[1].strip()))
            elif line == "progress=end" and duration:
                events.put(("progress", {"time": duration, "percent": 100.0}))
    finally:
        if cancel.is_set() and process.poll() is None:
            stop_process()
        try:
            code = process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            code = process.wait(timeout=5)
        try:
            if process.stdin is not None:
                process.stdin.close()
        except Exception:
            pass
        thread.join(timeout=2)
    if cancel.is_set():
        code = -2
    return code, "\n".join(stderr_lines[-30:]), seen_time


def remux_recording_to_mp4(src: Path, dst: Path, events: queue.Queue | None = None) -> Path:
    """Rewrite fragmented live MP4 into a normal seekable MP4."""
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        if src.resolve() != dst.resolve():
            if dst.exists():
                dst.unlink(missing_ok=True)
            src.replace(dst)
        return dst
    if events is not None:
        events.put(("status", "Собираю запись…"))
        events.put(("log", "Финализирую MP4 после остановки"))
    temp = dst.with_suffix(dst.suffix + ".final.mp4")
    command = [
        str(ffmpeg),
        "-hide_banner",
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(src),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(temp),
    ]
    process = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=CREATE_NO_WINDOW,
    )
    if process.returncode == 0 and temp.exists() and temp.stat().st_size > 1024:
        if dst.exists():
            dst.unlink(missing_ok=True)
        temp.replace(dst)
        if src.resolve() != dst.resolve():
            src.unlink(missing_ok=True)
        return dst
    temp.unlink(missing_ok=True)
    if src.resolve() != dst.resolve():
        if dst.exists():
            dst.unlink(missing_ok=True)
        src.replace(dst)
    return dst


def ytdlp_get_stream_urls(
    page_url: str,
    format_spec: str,
    headers: dict[str, str],
    proxy: str,
    events: queue.Queue | None = None,
    cookies_file: str = "",
) -> list[str]:
    """Resolve direct media URLs (video[, audio]) via yt-dlp -g."""
    ytdlp = ensure_yt_dlp(events)
    command = [
        str(ytdlp),
        "--no-playlist",
        "--no-warnings",
        "-f",
        format_spec or "bv*+ba/b",
        "-g",
    ]
    if headers.get("User-Agent"):
        command += ["--user-agent", headers["User-Agent"]]
    if headers.get("Referer"):
        command += ["--referer", headers["Referer"]]
    if proxy.strip():
        command += ["--proxy", proxy.strip()]
    command += ytdlp_cookies_args(cookies_file)
    command.append(page_url)
    process = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=CREATE_NO_WINDOW,
        timeout=90,
    )
    if process.returncode != 0:
        detail = (process.stderr or process.stdout or "").strip()
        raise PlaylistError(detail[-400:] or "Не удалось получить поток эфира")
    urls = [line.strip() for line in (process.stdout or "").splitlines() if line.strip().startswith("http")]
    if not urls:
        raise PlaylistError("Эфир недоступен или модель офлайн")
    return urls


def download_live_stream(
    page_url: str,
    output: Path,
    headers: dict[str, str],
    proxy: str,
    insecure: bool,
    events: queue.Queue,
    cancel: threading.Event,
    format_spec: str = "bv*+ba/b",
    limit_seconds: int | None = None,
) -> None:
    """Record a live HLS/DASH stream until Stop, then finalize a playable MP4."""
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        raise PlaylistError("Не найден ffmpeg.exe. Положите его в папку bin рядом с программой.")
    events.put(("status", "Получаю ссылку на эфир…"))
    urls = ytdlp_get_stream_urls(
        page_url,
        format_spec,
        headers,
        proxy,
        events,
        cookies_file=str(getattr(_DOWNLOAD_CTX, "cookies", "") or ""),
    )
    video_url = urls[0]
    audio_url = urls[1] if len(urls) > 1 else None
    events.put(("log", f"Режим: запись эфира · стоп = сохранить файл"))
    if audio_url:
        events.put(("log", "Видео + аудио потоки"))
    part = output.with_suffix(output.suffix + ".part.mp4")
    part.unlink(missing_ok=True)
    resolved = Resolved(
        video_url=video_url,
        audio_url=audio_url,
        label="live",
        is_vod=False,
        duration=None,
        headers=headers,
        temp_files=[],
        segments=[],
    )
    command = build_ffmpeg_command(
        ffmpeg,
        resolved,
        part,
        headers,
        proxy,
        insecure,
        limit_seconds,
        use_aac_bsf=True,
    )
    events.put(("status", "Запись эфира… нажмите «Стоп» чтобы сохранить"))
    events.put(("log", f"Пишу во временный файл: {part.name}"))
    code, err, seen = run_ffmpeg(command, float(limit_seconds) if limit_seconds else None, cancel, events)
    stopped = cancel.is_set() or code == -2
    has_data = part.exists() and part.stat().st_size > 8 * 1024
    if not has_data and output.exists() and output.stat().st_size > 8 * 1024:
        has_data = True
        part = output
    if stopped or code == 0:
        if not has_data:
            part.unlink(missing_ok=True)
            events.put(("done", {"cancelled": True, "output": str(output), "live": True}))
            return
        final = remux_recording_to_mp4(part, output, events)
        warn = verify_mp4_file(final)
        if warn:
            events.put(("log", f"Проверка файла: {warn}"))
        events.put(("progress", {"time": seen, "percent": 100.0}))
        events.put(
            (
                "done",
                {
                    "ok": True,
                    "output": str(final),
                    "live": True,
                    "stopped": stopped,
                    "mode": "live",
                },
            )
        )
        return
    message = err.strip() or f"ffmpeg завершился с кодом {code}"
    if has_data:
        final = remux_recording_to_mp4(part, output, events)
        message += f"\nЧастичный файл: {final}"
        raise PlaylistError(message)
    part.unlink(missing_ok=True)
    raise PlaylistError(message)


def sniff_image_ext(data: bytes, content_type: str) -> str:
    if data.startswith(b"\x89PNG"):
        return ".png"
    if data.startswith(b"\xff\xd8"):
        return ".jpg"
    if data.startswith(b"GIF8"):
        return ".gif"
    if data.startswith(b"RIFF") and b"WEBP" in data[:16]:
        return ".webp"
    kind = content_type.split(";", 1)[0].strip().lower()
    return {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }.get(kind, ".img")


def limit_segments(segments: list[Segment], limit_seconds: int | None) -> list[Segment]:
    if not limit_seconds:
        return segments
    chosen: list[Segment] = []
    elapsed = 0.0
    for segment in segments:
        if elapsed >= limit_seconds:
            break
        duration = segment.duration if segment.duration > 0 else 1.0
        if elapsed + duration > limit_seconds:
            duration = limit_seconds - elapsed
        chosen.append(Segment(segment.url, duration))
        elapsed += duration
    return chosen


def download_frame(index: int, segment: Segment, folder: Path, headers: dict[str, str], insecure: bool) -> Path:
    request = urllib.request.Request(segment.url, headers=headers)
    context = ssl._create_unverified_context() if insecure else ssl.create_default_context()
    last_error: Exception | None = None
    for attempt in range(6):
        try:
            with urllib.request.urlopen(request, timeout=60, context=context) as response:
                data = response.read()
                ext = sniff_image_ext(data[:16], response.headers.get("Content-Type") or "")
            path = folder / f"{index:05d}{ext}"
            path.write_bytes(data)
            if path.stat().st_size == 0:
                raise PlaylistError(f"Пустой кадр {index + 1}")
            return path
        except urllib.error.HTTPError as exc:
            if exc.code in {429, 500, 502, 503, 504} and attempt < 5:
                last_error = exc
                time.sleep(min(8.0, 0.7 * (2**attempt)))
                continue
            raise PlaylistError(f"Кадр {index + 1}: сервер ответил {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt == 5:
                break
            time.sleep(min(8.0, 0.7 * (2**attempt)))
    raise PlaylistError(f"Не удалось скачать кадр {index + 1}: {last_error}")


def extract_mpegts(data: bytes) -> bytes | None:
    """TikTok-style segments are a 1x1 PNG with an MPEG-TS stream after IEND."""
    payload = data
    if data.startswith(b"\x89PNG"):
        marker = data.find(b"IEND")
        if marker < 0:
            return None
        payload = data[marker + 8 :]
    if len(payload) >= 188 and payload[0] == 0x47 and len(payload) % 188 == 0:
        return payload
    return None


def slideshow_interval(segments: list[Segment]) -> float:
    durations = sorted(segment.duration for segment in segments if segment.duration >= 0.2)
    if not durations:
        durations = [segment.duration for segment in segments if segment.duration > 0] or [4.0]
    return max(durations[len(durations) // 2], 0.04)


def remux_ts_to_mp4(
    ffmpeg: Path,
    ts_path: Path,
    output: Path,
    duration: float | None,
    events: queue.Queue,
    cancel: threading.Event,
) -> None:
    """Mux concatenated MPEG-TS into MP4 with fallbacks for broken AAC tracks."""
    events.put(("status", "Собираю MP4…"))
    attempts: list[tuple[str, list[str]]] = [
        (
            "copy+aac",
            [
                "-probesize",
                "50M",
                "-analyzeduration",
                "50M",
                "-fflags",
                "+genpts+igndts+discardcorrupt",
                "-i",
                str(ts_path),
                "-map",
                "0:v:0?",
                "-map",
                "0:a:0?",
                "-c",
                "copy",
                "-bsf:a",
                "aac_adtstoasc",
            ],
        ),
        (
            "copy",
            [
                "-probesize",
                "50M",
                "-analyzeduration",
                "50M",
                "-fflags",
                "+genpts+igndts+discardcorrupt",
                "-i",
                str(ts_path),
                "-map",
                "0:v:0?",
                "-map",
                "0:a:0?",
                "-c",
                "copy",
            ],
        ),
        (
            "reencode-audio",
            [
                "-probesize",
                "50M",
                "-analyzeduration",
                "50M",
                "-fflags",
                "+genpts+igndts+discardcorrupt",
                "-i",
                str(ts_path),
                "-map",
                "0:v:0?",
                "-map",
                "0:a:0?",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
            ],
        ),
        (
            "video-only",
            [
                "-probesize",
                "50M",
                "-analyzeduration",
                "50M",
                "-fflags",
                "+genpts+igndts+discardcorrupt",
                "-i",
                str(ts_path),
                "-map",
                "0:v:0",
                "-an",
                "-c",
                "copy",
            ],
        ),
    ]
    last_err = ""
    for label, middle in attempts:
        if cancel.is_set():
            events.put(("done", {"cancelled": True, "output": str(output), "vod": True}))
            return
        if output.exists():
            output.unlink(missing_ok=True)
        command = [
            str(ffmpeg),
            "-hide_banner",
            "-y",
            "-loglevel",
            "warning",
            "-nostats",
            "-progress",
            "pipe:1",
            *middle,
            "-movflags",
            "+faststart",
            str(output),
        ]
        events.put(("log", f"Сборка MP4 ({label})…"))
        code, err, seen = run_ffmpeg(command, duration, cancel, events)
        if code == -2:
            events.put(("done", {"cancelled": True, "output": str(output), "vod": True}))
            return
        if code == 0 and output.exists() and output.stat().st_size > 1024:
            if label == "video-only":
                events.put(("log", "Аудиодорожка битая — сохранил только видео"))
            events.put(("progress", {"time": duration or seen, "percent": 100.0}))
            events.put(("done", {"ok": True, "output": str(output)}))
            return
        last_err = err.strip() or f"ffmpeg код {code}"
        events.put(("log", f"Сборка ({label}) не вышла: {last_err.splitlines()[-1][:160]}"))
    raise PlaylistError(last_err or "Не удалось собрать MP4")


def encode_slideshow(
    ffmpeg: Path,
    resolved: Resolved,
    output: Path,
    headers: dict[str, str],
    insecure: bool,
    limit_seconds: int | None,
    events: queue.Queue,
    cancel: threading.Event,
    threads: int = DEFAULT_THREADS,
) -> None:
    segments = [segment for segment in limit_segments(resolved.segments, limit_seconds) if segment.duration >= 0.2]
    if not segments:
        segments = limit_segments(resolved.segments, limit_seconds)
    if not segments:
        raise PlaylistError("В плейлисте нет кадров")
    folder = APP_DIR / "temp" / f"frames-{uuid_hex()}"
    folder.mkdir(parents=True, exist_ok=True)
    try:
        events.put(("log", f"Сегменты выглядят как картинки: {len(segments)}. Проверяю, что внутри."))
        events.put(("status", f"Скачиваю кадры 0 из {len(segments)}"))
        saved: list[Path | None] = [None] * len(segments)
        workers = min(clamp_threads(threads), len(segments))
        events.put(("log", f"Потоков для кадров: {workers}"))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(download_frame, index, segment, folder, headers, insecure): index
                for index, segment in enumerate(segments)
            }
            finished = 0
            failed: list[int] = []
            for future in as_completed(futures):
                if cancel.is_set():
                    pool.shutdown(wait=False, cancel_futures=True)
                    events.put(("done", {"cancelled": True, "output": str(output), "vod": True}))
                    return
                index = futures[future]
                try:
                    saved[index] = future.result()
                except PlaylistError as exc:
                    failed.append(index)
                    events.put(("log", str(exc)))
                finished += 1
                events.put(("progress", {"percent": finished / len(segments) * 85}))
                events.put(("status", f"Скачиваю кадры {finished} из {len(segments)}"))
        for index in failed:
            if cancel.is_set():
                events.put(("done", {"cancelled": True, "output": str(output), "vod": True}))
                return
            time.sleep(1.0)
            saved[index] = download_frame(index, segments[index], folder, headers, insecure)
        frames = [path for path in saved if path is not None]
        hidden = extract_mpegts(frames[0].read_bytes()) if frames else None
        if hidden is not None:
            ts_path = folder / "video.ts"
            with ts_path.open("wb") as stream:
                stream.write(hidden)
                for frame in frames[1:]:
                    payload = extract_mpegts(frame.read_bytes())
                    if payload is None:
                        raise PlaylistError("Часть сегментов не похожа на видео. Скачивание остановлено.")
                    stream.write(payload)
            duration = sum(segment.duration for segment in segments)
            events.put(("log", "Внутри картинок обычное видео. Собираю файл."))
            remux_ts_to_mp4(ffmpeg, ts_path, output, duration, events, cancel)
            return
        else:
            extensions = {path.suffix.lower() for path in frames}
            if len(extensions) != 1:
                raise PlaylistError("Кадры разного формата, такой плейлист собрать не удалось")
            interval = slideshow_interval(segments)
            duration = interval * len(frames)
            pattern = (folder / f"%05d{frames[0].suffix.lower()}").as_posix()
            events.put(("log", "Это слайдшоу из картинок. Собираю видео."))
            events.put(("status", "Собираю видео…"))
            command = [
                str(ffmpeg),
                "-hide_banner",
                "-y",
                "-loglevel",
                "warning",
                "-nostats",
                "-progress",
                "pipe:1",
                "-framerate",
                f"{1 / interval:.6f}",
                "-start_number",
                "0",
                "-i",
                pattern,
                "-vf",
                "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(output),
            ]
        code, err, _seen = run_ffmpeg(command, duration, cancel, events)
        if code == -2:
            events.put(("done", {"cancelled": True, "output": str(output), "vod": True}))
            return
        if code != 0:
            message = err.strip() or f"ffmpeg завершился с кодом {code}"
            raise PlaylistError(message)
        events.put(("progress", {"time": duration, "percent": 100.0}))
        events.put(("done", {"ok": True, "output": str(output)}))
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def uuid_hex() -> str:
    import uuid

    return uuid.uuid4().hex


def looks_like_playlist_url(url: str) -> bool:
    path = urllib.parse.unquote(urllib.parse.urlparse(url).path).lower()
    return path.endswith(".m3u8") or ".m3u8?" in url.lower() or "/m3u8" in path


def unescape_candidate(raw: str) -> str:
    text = html.unescape(raw)
    text = text.replace("\\/", "/").replace("\\u0026", "&").replace("\\u003d", "=")
    text = text.replace("\\u002F", "/").replace("\\/", "/")
    text = urllib.parse.unquote(text)
    text = text.rstrip("\\").rstrip(".,);]}\"'")
    return text


def unique_urls(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        result.append(url)
    return result


PACKER_RE = re.compile(
    r"eval\(function\(p,a,c,k,e,[dr]\)\{.*?return p\}"
    r"\('(?P<p>(?:\\'|[^'])*)',(?P<a>\d+),(?P<c>\d+),'(?P<k>(?:\\'|[^'])*)'\.split\('\|'\)",
    re.DOTALL,
)


def _to_string_base(num: int, base: int) -> str:
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    if num == 0:
        return alphabet[0]
    out = ""
    n = num
    while n > 0:
        out = alphabet[n % base] + out
        n //= base
    return out


def _unpack_packer(p: str, a: int, c: int, k: list[str]) -> str:
    """Match the common packer loop: replace \\b + c.toString(a) + \\b with k[c]."""
    idx = c
    while idx > 0:
        idx -= 1
        if idx < len(k) and k[idx]:
            token = _to_string_base(idx, a)
            p = re.sub(rf"\b{re.escape(token)}\b", k[idx], p)
    return p


def unpack_packed_scripts(text: str) -> str:
    chunks = [text]
    for match in PACKER_RE.finditer(text):
        payload = match.group("p").replace("\\'", "'").replace("\\n", "\n")
        a = int(match.group("a"))
        c = int(match.group("c"))
        keys = match.group("k").split("|")
        try:
            chunks.append(_unpack_packer(payload, a, c, keys))
        except Exception:
            continue
    return "\n".join(chunks)


def extract_encoded_player_urls(text: str) -> list[str]:
    """Decode base64 player configs (e.g. streamcash window.__PCr)."""
    found: list[str] = []
    patterns = [
        r"""window\.__PCr\s*=\s*['"]([A-Za-z0-9+/=]+)['"]""",
        r"""(?:window\.)?(?:__PC|playerConfig|videoConfig)\s*=\s*['"]([A-Za-z0-9+/=]+)['"]""",
    ]
    blobs: list[str] = []
    for pattern in patterns:
        blobs.extend(match.group(1) for match in re.finditer(pattern, text))
    # Also catch long base64 assignments that look like JSON configs.
    for match in re.finditer(
        r"""(?:window\.)?[A-Za-z_$][\w$]*\s*=\s*['"]([A-Za-z0-9+/]{80,}={0,2})['"]""",
        text,
    ):
        blobs.append(match.group(1))

    for blob in blobs:
        try:
            raw = base64.b64decode(blob).decode("utf-8", errors="ignore")
        except Exception:
            continue
        if not raw.startswith("{"):
            continue
        try:
            data = json.loads(raw)
        except Exception:
            # Sometimes m3u8 is plain text inside decoded blob
            for match in M3U8_URL_RE.finditer(raw):
                found.append(unescape_candidate(match.group("url")))
            continue
        if not isinstance(data, dict):
            continue
        for key in ("src", "file", "url", "source", "hls", "playlist", "streaming_url"):
            value = data.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://")) and looks_like_media_url(value):
                found.append(value)
        for match in M3U8_URL_RE.finditer(raw):
            found.append(unescape_candidate(match.group("url")))
    return unique_urls(found)


def extract_m3u8_candidates(text: str, base_url: str = "") -> list[str]:
    search_text = unpack_packed_scripts(text)
    found: list[str] = []
    for match in M3U8_URL_RE.finditer(search_text):
        found.append(unescape_candidate(match.group("url")))
    for match in ESCAPED_M3U8_RE.finditer(search_text):
        found.append(unescape_candidate(match.group(0)))
    for match in re.finditer(r"""["']([^"']+\.m3u8(?:\?[^"']*)?)["']""", search_text, flags=re.IGNORECASE):
        candidate = unescape_candidate(match.group(1))
        if candidate.startswith("//"):
            candidate = "https:" + candidate
        elif base_url and not candidate.startswith(("http://", "https://")):
            candidate = urllib.parse.urljoin(base_url, candidate)
        if candidate.startswith(("http://", "https://")):
            found.append(candidate)
    # JWPlayer / similar: sources:[{file:"https://...m3u8"}]
    for match in re.finditer(
        r"""(?:file|src|source)\s*[:=]\s*["'](https?://[^"']+\.m3u8[^"']*)["']""",
        search_text,
        flags=re.IGNORECASE,
    ):
        found.append(unescape_candidate(match.group(1)))
    found.extend(extract_encoded_player_urls(text))
    return unique_urls(found)


def extract_direct_media_candidates(text: str, base_url: str = "") -> list[str]:
    """Find progressive MP4/WebM URLs in page/embed HTML (incl. Dean Edwards packer / MDCore)."""
    search_text = unpack_packed_scripts(text)
    found: list[str] = []
    patterns = (
        r"""MDCore\.wurl\s*=\s*["']([^"']+)["']""",
        r"""(?:file|src|source|url|wurl)\s*[:=]\s*["']((?:https?:)?//[^"']+\.(?:mp4|webm|mkv|m4v|mov)[^"']*)["']""",
        r"""["']((?:https?:)?//[^"']+\.(?:mp4|webm|mkv|m4v|mov)(?:\?[^"']*)?)["']""",
        r"""["'](https?://[^"']+\.(?:mp4|webm|mkv|m4v|mov)(?:\?[^"']*)?)["']""",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, search_text, flags=re.IGNORECASE):
            absolute = absolute_media_url(match.group(1), base_url)
            if absolute and looks_like_direct_media_url(absolute):
                lower = absolute.lower()
                if any(skip in lower for skip in ("thumb", "preview", "sprite", "poster", "/ads/", "blank.mp4")):
                    continue
                # eporner page lists gvideo.*.mp4 stubs that always 403 without signed CDN.
                if "gvideo.eporner.com" in lower:
                    continue
                found.append(absolute)
    return unique_urls(found)


def extract_player_poster(text: str, base_url: str = "") -> str | None:
    """Poster/thumbnail from packed player configs (e.g. MDCore.poster)."""
    search_text = unpack_packed_scripts(text)
    patterns = (
        r"""MDCore\.poster\s*=\s*["']([^"']+)["']""",
        r"""(?:poster|image|preview)\s*[:=]\s*["']((?:https?:)?//[^"']+\.(?:jpg|jpeg|png|webp)[^"']*)["']""",
    )
    for pattern in patterns:
        match = re.search(pattern, search_text, flags=re.IGNORECASE)
        if not match:
            continue
        absolute = absolute_media_url(match.group(1), base_url)
        if absolute:
            return absolute
    return None


def score_playlist_url(url: str) -> tuple[int, int, str]:
    lower = url.lower()
    score = 0
    if "master" in lower:
        score += 50
    if "index" in lower or "playlist" in lower:
        score += 20
    if "chunklist" in lower or "media" in lower:
        score -= 10
    if "iframe" in lower:
        score -= 40
    if "audio" in lower and "video" not in lower:
        score -= 30
    return (score, len(url), url)


def pick_best_playlist(urls: list[str]) -> str:
    return max(urls, key=score_playlist_url)


def score_direct_url(url: str) -> tuple[int, int, str]:
    lower = url.lower()
    score = 0
    if "/v2/" in lower or "mxcontent" in lower:
        score += 40
    if ".mp4" in lower:
        score += 20
    if urllib.parse.urlsplit(url).query:
        score += 15
    if any(token in lower for token in ("thumb", "preview", "sprite", "poster")):
        score -= 80
    return (score, len(url), url)


def pick_best_direct(urls: list[str]) -> str:
    return max(urls, key=score_direct_url)


def find_yt_dlp() -> Path | None:
    if YTDLP_PATH.exists():
        return YTDLP_PATH
    found = shutil.which("yt-dlp")
    return Path(found) if found else None


def ensure_yt_dlp(events: queue.Queue | None = None) -> Path:
    existing = find_yt_dlp()
    if existing is not None:
        return existing
    YTDLP_PATH.parent.mkdir(parents=True, exist_ok=True)
    if events is not None:
        events.put(("status", "Скачиваю yt-dlp…"))
        events.put(("log", "Нужен yt-dlp, чтобы разбирать страницы сайтов"))
    asset = "yt-dlp.exe" if IS_WINDOWS else "yt-dlp"
    url = f"https://github.com/yt-dlp/yt-dlp/releases/latest/download/{asset}"
    request = urllib.request.Request(url, headers={"User-Agent": DEFAULT_UA})
    with urllib.request.urlopen(request, timeout=180) as response:
        data = response.read()
    YTDLP_PATH.write_bytes(data)
    if not IS_WINDOWS:
        YTDLP_PATH.chmod(YTDLP_PATH.stat().st_mode | 0o111)
    return YTDLP_PATH


def open_path(target: str | Path) -> None:
    """Open a file/folder/URL with the OS default handler."""
    path = str(target)
    if not path:
        return
    try:
        if IS_WINDOWS:
            os.startfile(path)  # type: ignore[attr-defined]
        elif IS_MAC:
            subprocess.Popen(["open", path], creationflags=CREATE_NO_WINDOW)
        else:
            subprocess.Popen(["xdg-open", path], creationflags=CREATE_NO_WINDOW)
    except OSError as exc:
        raise PlaylistError(f"Не удалось открыть: {path}\n{exc}") from exc


def looks_like_video_url(text: str) -> bool:
    value = (text or "").strip()
    if not value:
        return False
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if len(lines) > 1:
        return all(looks_like_video_url(line) for line in lines)
    if not value.startswith(("http://", "https://")):
        return False
    if any(ch.isspace() for ch in value):
        return False
    lower = value.lower()
    if any(
        token in lower
        for token in (
            ".m3u8",
            "youtube.",
            "youtu.be",
            "vk.com",
            "vk.ru",
            "vkvideo.",
            "rutube.",
            "pornhub.",
            "eporner.",
            "chaturbate.",
            "mixdrop.",
            "mxdrop.",
        )
    ):
        return True
    return bool(re.match(r"https?://[^/\s]+/.+", value))


def friendly_download_error(message: str) -> str:
    text = (message or "").strip()
    lower = text.lower()
    if "video has been deleted" in lower or "удалено с eporner" in lower:
        return text.splitlines()[0][:200]
    if "unable to extract hash" in lower:
        return "Не удалось разобрать страницу (видео удалено или недоступно)."
    if "http error 403" in lower or "forbidden" in lower:
        return "Доступ запрещён (403). Нужны cookies / другой UA / прокси."
    if "http error 404" in lower or "not found" in lower:
        return "Ссылка не найдена (404)."
    if "timed out" in lower or "timeout" in lower:
        return "Таймаут сети. Проверьте интернет или прокси."
    if "ffmpeg" in lower and "не найден" in lower:
        return text.splitlines()[0][:200]
    first = text.splitlines()[0].strip() if text else "Ошибка скачивания"
    return first[:200]


def play_alert_sound(ok: bool = True) -> None:
    try:
        if IS_WINDOWS:
            import winsound

            winsound.MessageBeep(winsound.MB_OK if ok else winsound.MB_ICONHAND)
        else:
            # Terminal bell; harmless when launched as GUI.
            sys.stdout.write("\a")
            sys.stdout.flush()
    except Exception:
        pass


def ensure_curl_cffi(events: queue.Queue | None = None) -> None:
    try:
        import curl_cffi  # noqa: F401

        return
    except ImportError:
        pass
    if events is not None:
        events.put(("status", "Ставлю curl_cffi…"))
        events.put(("log", "Нужен curl_cffi, чтобы обходить защиту сайтов"))
    command = [sys.executable, "-m", "pip", "install", "--user", "curl_cffi"]
    process = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=CREATE_NO_WINDOW,
    )
    if process.returncode != 0:
        raise PlaylistError("Не удалось установить curl_cffi. Выполните: pip install curl_cffi")
    try:
        import curl_cffi  # noqa: F401
    except ImportError as exc:
        raise PlaylistError("curl_cffi установлен, но Python его не видит. Перезапустите программу.") from exc


def page_origin(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}/"


def host_matches(url: str, hosts: tuple[str, ...]) -> bool:
    host = urllib.parse.urlsplit(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return any(host == item or host.endswith("." + item) for item in hosts)


def prefers_ytdlp_download(url: str) -> bool:
    """Sites where raw HLS often is audio-only or incomplete — use yt-dlp merge instead."""
    return host_matches(url, YTDLP_NATIVE_HOSTS)


def is_youtube_url(url: str) -> bool:
    return host_matches(url, YOUTUBE_HOSTS)


def is_vk_url(url: str) -> bool:
    return host_matches(url, ("vk.com", "vk.ru", "vkvideo.ru", "m.vk.com"))


def is_eporner_url(url: str) -> bool:
    return host_matches(url, ("eporner.com",))


def eporner_unavailable_reason(body: str) -> str | None:
    """Detect deleted/missing eporner pages (yt-dlp then fails with 'Unable to extract hash')."""
    lower = (body or "").lower()
    if 'id="deletedfile"' in lower or "video has been deleted" in lower:
        if "copyright" in lower:
            return "Видео удалено с eporner (запрос правообладателя)."
        return "Видео удалено с eporner."
    if "file has been removed" in lower:
        return "Видео удалено с eporner."
    return None


def is_youtube_playlist_url(url: str) -> bool:
    if not is_youtube_url(url):
        return False
    lower = url.lower()
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qs(parts.query)
    if "list" in query and query["list"] and not query["list"][0].startswith("UL"):
        # Presence of list= usually means playlist context; still allow single-video probe first.
        if "/playlist" in lower or "list=" in lower:
            if "/watch" in lower and query.get("v"):
                return "list" in query  # watch+list → treat as playlist picker opportunity
            return True
    path = parts.path.lower()
    return any(
        token in path
        for token in (
            "/playlist",
            "/videos",
            "/streams",
            "/shorts",
            "/channel/",
            "/c/",
            "/user/",
            "/@",
        )
    )


def youtube_watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def find_aria2c() -> Path | None:
    if ARIA2_PATH.exists():
        return ARIA2_PATH
    found = shutil.which("aria2c")
    return Path(found) if found else None


class SpeedMeter:
    """Track download speed and ETA from cumulative byte progress."""

    def __init__(self) -> None:
        now = time.time()
        self.started = now
        self.last_t = now
        self.last_bytes = 0
        self.ema_bps = 0.0

    def update(self, done_bytes: int, total_bytes: int | None = None) -> tuple[str, str]:
        now = time.time()
        dt = max(0.001, now - self.last_t)
        delta = max(0, done_bytes - self.last_bytes)
        inst = delta / dt
        self.ema_bps = inst if self.ema_bps <= 0 else (self.ema_bps * 0.7 + inst * 0.3)
        self.last_t = now
        self.last_bytes = done_bytes
        speed = fmt_size(self.ema_bps) + "/с" if self.ema_bps > 0 else ""
        eta = ""
        if total_bytes and total_bytes > done_bytes and self.ema_bps > 500:
            remain = (total_bytes - done_bytes) / self.ema_bps
            eta = f"~{fmt_duration(remain)}"
        return speed, eta


def append_download_history(entry: dict) -> None:
    items: list[dict] = []
    try:
        raw = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            items = raw
    except (OSError, json.JSONDecodeError):
        items = []
    items.insert(0, entry)
    items = items[:200]
    try:
        HISTORY_PATH.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def load_download_history() -> list[dict]:
    try:
        raw = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        return raw if isinstance(raw, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def verify_mp4_file(path: Path, expected_size: int | None = None) -> str | None:
    """Return error message if file looks broken, else None."""
    if not path.exists():
        return "Файл не создан"
    size = path.stat().st_size
    if size < 1024:
        return "Файл слишком маленький"
    if expected_size and abs(size - expected_size) > max(64, expected_size // 1000):
        return f"Размер не совпал: {fmt_size(size)} вместо {fmt_size(expected_size)}"
    try:
        head = path.read_bytes()[:64]
    except OSError as exc:
        return str(exc)
    if b"ftyp" not in head[4:12] and not head.startswith(b"\x00\x00\x00"):
        # Many MP4s start with size+ftyp; accept also ISO BMFF variants.
        if b"ftyp" not in head:
            return "Нет сигнатуры MP4 (ftyp)"
    return None


def ytdlp_entry_url(entry: dict) -> str | None:
    if not isinstance(entry, dict):
        return None
    for key in ("url", "webpage_url", "original_url"):
        value = entry.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value
    video_id = entry.get("id")
    ie = str(entry.get("ie_key") or entry.get("extractor_key") or "").lower()
    if isinstance(video_id, str) and video_id and ("youtube" in ie or len(video_id) == 11):
        return youtube_watch_url(video_id)
    return None


def flatten_ytdlp_entries(info: dict) -> list[dict]:
    """Normalize yt-dlp playlist/channel JSON into selectable video rows."""
    entries = info.get("entries")
    if not isinstance(entries, list):
        return []
    rows: list[dict] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        # Nested playlists / unavailable
        if item.get("entries") and not item.get("id"):
            rows.extend(flatten_ytdlp_entries(item))
            continue
        if item.get("_type") == "playlist":
            rows.extend(flatten_ytdlp_entries(item))
            continue
        url = ytdlp_entry_url(item)
        if not url:
            continue
        title = item.get("title") or item.get("id") or url
        duration = item.get("duration")
        thumb = pick_ytdlp_thumbnail(item) or item.get("thumbnail")
        rows.append(
            {
                "id": str(item.get("id") or ""),
                "title": str(title),
                "url": url,
                "duration": duration if isinstance(duration, (int, float)) else None,
                "thumbnail": thumb if isinstance(thumb, str) else None,
            }
        )
    # de-dupe by url
    seen: set[str] = set()
    unique: list[dict] = []
    for row in rows:
        if row["url"] in seen:
            continue
        seen.add(row["url"])
        unique.append(row)
    return unique


def probe_ytdlp_playlist(url: str, events: queue.Queue | None = None) -> dict | None:
    """Return playlist probe dict or None if this is a single video."""
    ytdlp = ensure_yt_dlp(events)
    command = [
        str(ytdlp),
        "--flat-playlist",
        "--no-warnings",
        "-J",
        url,
    ]
    command = append_ytdlp_cookies(command)
    try:
        process = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        return None
    if process.returncode != 0 or not process.stdout.strip():
        return None
    try:
        info = json.loads(process.stdout)
    except json.JSONDecodeError:
        return None
    entries = flatten_ytdlp_entries(info)
    # Single video flat JSON still has no entries / one entry matching itself
    if len(entries) <= 1 and not (info.get("_type") == "playlist" and len(entries) >= 1):
        if info.get("_type") != "playlist" and not is_youtube_playlist_url(url):
            return None
        if len(entries) <= 1 and info.get("_type") != "playlist":
            return None
    if not entries:
        return None
    return {
        "url": url,
        "title": info.get("title") or "Плейлист YouTube",
        "thumbnail": pick_ytdlp_thumbnail(info),
        "referer": url,
        "playlist_url": f"ytdlp:{url}",
        "browser": False,
        "mode": "ytdlp_playlist",
        "entries": entries,
        "qualities": [{"label": "Максимальное (лучшее)", "ytdlp_format": "bv*+ba/b", "choice_url": None}],
    }


def format_has_video(item: dict) -> bool:
    vcodec = str(item.get("vcodec") or "").lower()
    if vcodec in ("", "none", "null"):
        return False
    note = str(item.get("format_note") or "").lower()
    if "audio only" in note:
        return False
    if item.get("height") in (0, None) and item.get("width") in (0, None) and not item.get("resolution"):
        # Some entries omit size but still have vcodec; trust vcodec.
        pass
    return True


def browser_headers(url: str, referer: str = "", extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
        "Upgrade-Insecure-Requests": "1",
    }
    headers["Referer"] = referer.strip() or page_origin(url)
    if extra:
        for key, value in extra.items():
            if value:
                headers[key] = value
    return headers


IFRAME_SRC_RE = re.compile(
    r"""<(?:iframe|embed)[^>]+(?:src|data-src)\s*=\s*["']([^"']+)["']""",
    re.IGNORECASE,
)
MIRROR_URL_RE = re.compile(
    r"""(?:var|let|const)\s+(?:MIRROR|MIRROR_URL|EMBED_URL|PLAYER_URL)\s*=\s*["'](https?://[^"']+)["']""",
    re.IGNORECASE,
)


def extract_iframe_urls(text: str, base_url: str) -> list[str]:
    found: list[str] = []
    for match in IFRAME_SRC_RE.finditer(text):
        candidate = unescape_candidate(match.group(1))
        if candidate.startswith("//"):
            candidate = "https:" + candidate
        elif not candidate.startswith(("http://", "https://")):
            candidate = urllib.parse.urljoin(base_url, candidate)
        if candidate.startswith(("http://", "https://")):
            lower = candidate.lower()
            if any(skip in lower for skip in ("googletagmanager", "facebook.com", "twitter.com", "ads", "doubleclick")):
                continue
            found.append(candidate)
    return unique_urls(found)


def extract_mirror_urls(text: str) -> list[str]:
    return unique_urls(match.group(1) for match in MIRROR_URL_RE.finditer(text))


def extract_follow_urls(text: str, base_url: str) -> list[str]:
    return unique_urls(extract_iframe_urls(text, base_url) + extract_mirror_urls(text))


def filecode_from_url(url: str) -> str | None:
    path = urllib.parse.urlsplit(url).path.strip("/")
    parts = [part for part in path.split("/") if part]
    if not parts:
        return None
    code = parts[-1]
    if re.fullmatch(r"[A-Za-z0-9_-]{6,64}", code):
        return code
    return None


def looks_like_media_url(url: str) -> bool:
    lower = url.lower()
    return ".m3u8" in lower or ".mp4" in lower or "/hls/" in lower


def looks_like_direct_media_url(url: str) -> bool:
    """True for progressive MP4/WebM (not HLS playlists)."""
    if not url or url.startswith("ytdlp:"):
        return False
    if looks_like_playlist_url(url):
        return False
    path = urllib.parse.unquote(urllib.parse.urlsplit(url).path).lower()
    return any(path.endswith(ext) for ext in (".mp4", ".webm", ".mkv", ".m4v", ".mov"))


def absolute_media_url(candidate: str, base_url: str = "") -> str | None:
    text = unescape_candidate(candidate).strip()
    if not text or text in {" ", "null", "undefined"}:
        return None
    if text.startswith("//"):
        text = "https:" + text
    elif not text.startswith(("http://", "https://")):
        # Bare filenames like "hash.mp4" are not useful without a CDN host.
        if "/" not in text:
            return None
        if not base_url:
            return None
        text = urllib.parse.urljoin(base_url, text)
    if not text.startswith(("http://", "https://")):
        return None
    return text


def extract_title(text: str) -> str | None:
    match = re.search(r"<title[^>]*>(.*?)</title>", text, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", match.group(1))).strip()
    return title or None


class BrowserClient:
    """HTTP client that mimics Chrome and bypasses many bot checks."""

    def __init__(self, headers: dict[str, str] | None = None, proxy: str = "") -> None:
        ensure_curl_cffi()
        from curl_cffi import requests as crequests

        self._requests = crequests
        self.session = crequests.Session(impersonate="chrome131")
        self.headers = dict(headers or {})
        try:
            self.proxy = normalize_proxy_url(proxy or current_proxy())
        except PlaylistError:
            self.proxy = (proxy or current_proxy() or "").strip()
        self._proxies = requests_proxies(self.proxy)

    def get_text(self, url: str, referer: str = "", timeout: int = 60) -> tuple[str, str, dict[str, str]]:
        headers = browser_headers(url, referer, self.headers)
        response = self.session.get(
            url,
            headers=headers,
            timeout=timeout,
            allow_redirects=True,
            proxies=self._proxies,
        )
        if response.status_code >= 400:
            raise PlaylistError(f"Сервер ответил {response.status_code} для {url}")
        final = str(response.url)
        used = {
            "User-Agent": self.headers.get("User-Agent") or DEFAULT_UA,
            "Referer": headers.get("Referer", page_origin(final)),
            "Origin": page_origin(headers.get("Referer", final)).rstrip("/"),
            "Accept": "*/*",
        }
        return response.text, final, used

    def get_bytes(self, url: str, referer: str = "", timeout: int = 60) -> bytes:
        headers = browser_headers(url, referer, self.headers)
        headers["Accept"] = "*/*"
        response = self.session.get(
            url,
            headers=headers,
            timeout=timeout,
            allow_redirects=True,
            proxies=self._proxies,
        )
        if response.status_code >= 400:
            raise PlaylistError(f"Сервер ответил {response.status_code} при скачивании сегмента")
        return response.content

    def post_json(self, url: str, payload: dict, referer: str = "", timeout: int = 60) -> dict:
        headers = browser_headers(url, referer, self.headers)
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "application/json, */*"
        response = self.session.post(
            url,
            headers=headers,
            data=json.dumps(payload),
            timeout=timeout,
            allow_redirects=True,
            proxies=self._proxies,
        )
        if response.status_code >= 400:
            raise PlaylistError(f"Сервер ответил {response.status_code} для {url}")
        try:
            data = response.json()
        except Exception as exc:
            raise PlaylistError(f"Неверный JSON от {url}") from exc
        if not isinstance(data, dict):
            raise PlaylistError(f"Неожиданный ответ от {url}")
        return data


STREAM_API_PATH_RE = re.compile(
    r"""['"](/api/(?:stream|video|play|source|player|hls|media)[^'"]*)['"]""",
    re.IGNORECASE,
)
STREAM_API_FETCH_RE = re.compile(
    r"""fetch\(\s*(?:apiURL|['"]([^'"]*api[^'"]*)['"])""",
    re.IGNORECASE,
)


def discover_stream_api_urls(page_url: str, body: str) -> list[str]:
    """Find JSON stream endpoints used by embed players (any host)."""
    parts = urllib.parse.urlsplit(page_url)
    origin = f"{parts.scheme}://{parts.netloc}"
    found: list[str] = []

    if "/api/stream" in body or "streaming_url" in body:
        found.append(f"{origin}/api/stream")

    for match in STREAM_API_PATH_RE.finditer(body):
        path = match.group(1)
        found.append(urllib.parse.urljoin(origin + "/", path.lstrip("/")))

    for match in STREAM_API_FETCH_RE.finditer(body):
        raw = match.group(1)
        if not raw:
            continue
        if raw.startswith(("http://", "https://")):
            found.append(raw)
        elif raw.startswith("/"):
            found.append(urllib.parse.urljoin(origin + "/", raw.lstrip("/")))

    # Keep query from page except filecode (mirrors sometimes need it).
    query = urllib.parse.urlencode(
        [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True) if k != "filecode"]
    )
    with_query: list[str] = []
    for url in unique_urls(found):
        if query and "?" not in url:
            with_query.append(f"{url}?{query}")
        else:
            with_query.append(url)
    return unique_urls(with_query)


def extract_stream_url_from_json(data: dict) -> str | None:
    for key in (
        "streaming_url",
        "stream_url",
        "hls_url",
        "playlist",
        "playlist_url",
        "src",
        "file",
        "url",
        "source",
        "hls",
        "video",
        "link",
    ):
        value = data.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")) and looks_like_media_url(value):
            return value
        if isinstance(value, dict):
            nested = extract_stream_url_from_json(value)
            if nested:
                return nested
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    nested = extract_stream_url_from_json(item)
                    if nested:
                        return nested
                elif isinstance(item, str) and item.startswith(("http://", "https://")) and looks_like_media_url(item):
                    return item
    return None


def try_stream_api(
    client: BrowserClient,
    page_url: str,
    body: str,
    events: queue.Queue | None = None,
) -> tuple[str, str | None] | None:
    """Generic embed API: POST filecode/id -> m3u8 URL (+ optional thumbnail)."""
    api_urls = discover_stream_api_urls(page_url, body)
    if not api_urls:
        return None
    filecode = filecode_from_url(page_url)
    if not filecode:
        return None

    payloads = [
        {"filecode": filecode, "device": "web"},
        {"filecode": filecode, "device": "desktop"},
        {"id": filecode, "device": "web"},
        {"video_id": filecode},
        {"vid": filecode},
    ]

    for api_url in api_urls:
        if events is not None:
            events.put(("log", f"Запрашиваю поток: {api_url}"))
        for payload in payloads:
            try:
                data = client.post_json(api_url, payload, referer=page_url)
            except PlaylistError:
                continue
            stream = extract_stream_url_from_json(data)
            if stream:
                return stream, extract_json_thumbnail(data)
    return None


def probe_browser_url(client: BrowserClient, url: str, referer: str = "") -> tuple[str, str, str, dict[str, str]]:
    text, final_url, used = client.get_text(url, referer=referer)
    sample = text.lstrip("\ufeff")[:800].lower()
    if "#extm3u" in sample:
        return "playlist", final_url, text, used
    if not text.strip():
        return "empty", final_url, text, used
    return "page", final_url, text, used


# videosh.upns.live / similar SPA players encrypt /api/v1/video with AES-CBC.
# Key/IV match player-version 16.x (derived from protocol + hash + table "3579").
UPNS_AES_KEY = b"kiemtienmua911ca"
UPNS_AES_IV = b"1234567890oiuytr"
UPNS_HOST_MARKERS = ("upns.", "upnshare.", "videosh.")


def looks_like_upns_player(url: str, body: str = "") -> bool:
    host = urllib.parse.urlsplit(url).netloc.lower()
    if any(marker in host for marker in UPNS_HOST_MARKERS):
        return True
    sample = (body or "")[:2000].lower()
    return 'player-version="' in sample and "/assets/index-" in sample


def upns_video_id_from_url(url: str) -> str | None:
    fragment = html.unescape(urllib.parse.urlsplit(url).fragment or "")
    if not fragment:
        return None
    video_id = fragment.split("&", 1)[0].strip()
    if len(video_id) > 1 and re.fullmatch(r"[A-Za-z0-9_-]+", video_id):
        return video_id
    return None


def referrer_hostname(url: str) -> str:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def decrypt_upns_payload(hex_text: str) -> dict:
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as exc:
        raise PlaylistError("Нужен пакет cryptography для плеера upns") from exc
    raw = hex_text.strip()
    if not re.fullmatch(r"[0-9a-fA-F]+", raw) or len(raw) % 32 != 0:
        raise PlaylistError("Неверный ответ API upns")
    data = bytes.fromhex(raw)
    decryptor = Cipher(algorithms.AES(UPNS_AES_KEY), modes.CBC(UPNS_AES_IV)).decryptor()
    plain = decryptor.update(data) + decryptor.finalize()
    pad = plain[-1] if plain else 0
    if 1 <= pad <= 16 and plain.endswith(bytes([pad]) * pad):
        plain = plain[:-pad]
    try:
        parsed = json.loads(plain.decode("utf-8"))
    except Exception as exc:
        raise PlaylistError("Не удалось расшифровать ответ upns") from exc
    if not isinstance(parsed, dict):
        raise PlaylistError("Некорректный JSON от upns")
    return parsed


def upns_abs_url(value: str, origin: str) -> str | None:
    text = unescape_candidate(value).strip()
    if not text:
        return None
    if text.startswith("//"):
        text = "https:" + text
    elif not text.startswith(("http://", "https://")):
        text = urllib.parse.urljoin(origin.rstrip("/") + "/", text.lstrip("/"))
    if not text.startswith(("http://", "https://")):
        return None
    return text


def upns_attach_play_token(stream_url: str, pk: dict | None) -> str:
    if not pk or not isinstance(pk, dict):
        return stream_url
    key = pk.get("k")
    kx = pk.get("kx")
    if not key or "/v4/" not in stream_url:
        return stream_url
    if f"k={key}" in stream_url:
        return stream_url
    sep = "&" if "?" in stream_url else "?"
    token = f"k={key}"
    if kx:
        token += f"&kx={kx}"
    return stream_url + sep + token


def extract_upns_stream_urls(data: dict, origin: str) -> list[str]:
    """Prefer sources in the same order as the official player."""
    order = ["Tiktok", "Google", "Cloudflare", "In-House"]
    field_map = {
        "Tiktok": "hlsVideoTiktok",
        "Google": "hlsVideoGoogle",
        "Cloudflare": "cf",
        "In-House": "source",
    }
    cfg = data.get("streamingConfig")
    if isinstance(cfg, str):
        try:
            cfg = json.loads(cfg)
        except Exception:
            cfg = None
    if isinstance(cfg, dict) and isinstance(cfg.get("order"), list) and cfg["order"]:
        order = [str(item) for item in cfg["order"]]

    adjust = {}
    if isinstance(cfg, dict) and isinstance(cfg.get("adjust"), dict):
        adjust = cfg["adjust"]

    pk = data.get("pk") if isinstance(data.get("pk"), dict) else None
    found: list[str] = []
    for name in order:
        raw = data.get(field_map.get(name, ""))
        if name == "Cloudflare" and not raw:
            raw = data.get("cfNative")
        if not isinstance(raw, str) or not raw.strip():
            continue
        meta = adjust.get(name) if isinstance(adjust.get(name), dict) else {}
        if meta.get("disabled"):
            continue
        absolute = upns_abs_url(raw, origin)
        if not absolute:
            continue
        if isinstance(meta.get("domain"), str) and "/hls/" in absolute:
            absolute = absolute.replace("/hls/", f"/hlsmod/{meta['domain']}/", 1)
        if isinstance(meta.get("params"), dict):
            parts = urllib.parse.urlsplit(absolute)
            query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
            query.update({str(k): str(v) for k, v in meta["params"].items()})
            absolute = urllib.parse.urlunsplit(
                (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(query), parts.fragment)
            )
        found.append(upns_attach_play_token(absolute, pk))
    # Also collect any leftover media-looking fields.
    for key in ("cf", "cfNative", "source", "hlsVideoTiktok", "hlsVideoGoogle"):
        raw = data.get(key)
        if isinstance(raw, str):
            absolute = upns_abs_url(raw, origin)
            if absolute:
                found.append(upns_attach_play_token(absolute, pk))
    return unique_urls(found)


def resolve_upns_embed(
    client: BrowserClient,
    embed_url: str,
    page_referer: str = "",
    events: queue.Queue | None = None,
) -> tuple[str, str | None, str | None]:
    """Return (stream_url, title, thumbnail) for upns/videosh hash embeds."""
    video_id = upns_video_id_from_url(embed_url)
    if not video_id:
        raise PlaylistError("Не найден id видео в ссылке плеера upns")
    parts = urllib.parse.urlsplit(embed_url)
    origin = f"{parts.scheme or 'https'}://{parts.netloc}"
    ref_host = referrer_hostname(page_referer) or referrer_hostname(embed_url)
    api = (
        f"{origin}/api/v1/video?id={urllib.parse.quote(video_id)}"
        f"&w=1920&h=1080&r={urllib.parse.quote(ref_host)}"
    )
    if events is not None:
        events.put(("log", f"upns API: {api}"))
    headers = browser_headers(api, embed_url or page_referer, client.headers)
    headers["Accept"] = "*/*"
    headers["Origin"] = origin
    response = client.session.get(api, headers=headers, timeout=60, allow_redirects=True)
    body = response.text
    if response.status_code >= 400:
        message = None
        try:
            message = json.loads(body).get("message")
        except Exception:
            message = None
        raise PlaylistError(message or f"Видео недоступно на upns ({response.status_code})")
    data = decrypt_upns_payload(body)
    streams = extract_upns_stream_urls(data, origin)
    if not streams:
        delivery = data.get("delivery") if isinstance(data.get("delivery"), dict) else {}
        if delivery.get("inHouse") == "hidden":
            raise PlaylistError("Видео ещё не готово на upns")
        raise PlaylistError("В ответе upns нет ссылки на поток")
    title = data.get("title") if isinstance(data.get("title"), str) else None
    poster = data.get("poster")
    thumbnail = None
    if isinstance(poster, str) and poster.strip() and poster.lower() not in {"false", "null"}:
        thumbnail = upns_abs_url(poster, origin)
    if events is not None:
        events.put(("log", f"Нашёл поток upns: {streams[0]}"))
    return streams[0], title or None, thumbnail


def resolve_page_to_playlist(
    page_url: str,
    headers: dict[str, str],
    insecure: bool,
    events: queue.Queue | None = None,
) -> PageResolve:
    """Open a page like a browser, follow embeds, and find the HLS playlist."""
    del insecure  # curl_cffi verifies TLS; unused but kept for call-site compatibility
    if prefers_ytdlp_download(page_url):
        if events is not None:
            events.put(("status", "Получение метаданных…"))
            events.put(("log", "Для этого сайта нужен yt-dlp (видео+аудио вместе)"))
            events.put(("autofill", {"referer": page_url, "user_agent": headers.get("User-Agent") or DEFAULT_UA}))
        title = None
        thumbnail = None
        probe_err = ""
        try:
            ytdlp = ensure_yt_dlp(events)
            probe_cmd = [
                str(ytdlp),
                "--no-playlist",
                "--no-warnings",
                "-J",
                page_url,
            ]
            if is_youtube_url(page_url):
                probe_cmd[3:3] = ["--extractor-args", YTDLP_YT_FAST_CLIENT]
            probe_cmd = append_ytdlp_cookies(probe_cmd)
            probe = subprocess.run(
                probe_cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=CREATE_NO_WINDOW,
                timeout=90,
            )
            if probe.returncode == 0 and probe.stdout.strip():
                info = json.loads(probe.stdout)
                title = info.get("title") or None
                thumbnail = pick_ytdlp_thumbnail(info)
                if thumbnail and events is not None:
                    events.put(("thumbnail", thumbnail))
            else:
                probe_err = (probe.stderr or probe.stdout or "").strip()
        except Exception:
            title = None
            thumbnail = None
        if not title and is_eporner_url(page_url):
            try:
                ensure_curl_cffi(events)
                client = BrowserClient(headers)
                _, _, body, _ = probe_browser_url(client, page_url, referer=page_url)
                reason = eporner_unavailable_reason(body)
                if reason:
                    raise PlaylistError(reason)
                title = extract_title(body) or title
                thumbnail = extract_page_thumbnail(body, page_url) or thumbnail
            except PlaylistError:
                raise
            except Exception:
                pass
            if probe_err and "unable to extract hash" in probe_err.lower():
                raise PlaylistError(
                    "Не удалось получить видео с eporner (страница без плеера).\n"
                    + probe_err[-280:]
                )
        return PageResolve(f"ytdlp:{page_url}", title, page_url, browser=False, thumbnail=thumbnail)

    ensure_curl_cffi(events)
    client = BrowserClient(headers)
    if events is not None:
        events.put(("status", "Читаю страницу…"))
        events.put(("log", f"Открываю страницу: {page_url}"))
        events.put(("autofill", {"referer": page_url, "user_agent": headers.get("User-Agent") or DEFAULT_UA}))

    queue_urls: list[tuple[str, str]] = [(page_url, page_origin(page_url))]
    seen: set[str] = set()
    title: str | None = None
    thumbnail: str | None = None
    last_referer = page_url
    all_candidates: list[str] = []
    all_direct: list[str] = []
    last_upns_error: str | None = None

    for _depth in range(6):
        if not queue_urls:
            break
        current, referer = queue_urls.pop(0)
        if current in seen:
            continue
        seen.add(current)
        try:
            kind, final_url, body, used = probe_browser_url(client, current, referer=referer)
        except PlaylistError as exc:
            if events is not None:
                events.put(("log", f"Пропуск {current}: {exc}"))
            continue
        last_referer = final_url
        if events is not None:
            events.put(("autofill", {"referer": last_referer}))
        if kind == "playlist":
            return PageResolve(final_url, title, last_referer, browser=True, thumbnail=thumbnail)
        if title is None:
            title = extract_title(body)
        poster = extract_player_poster(body, final_url)
        if thumbnail is None:
            thumbnail = extract_page_thumbnail(body, final_url) or poster
            if thumbnail and events is not None:
                events.put(("thumbnail", thumbnail))
        elif poster:
            thumbnail = poster
            if events is not None:
                events.put(("thumbnail", thumbnail))
        candidates = extract_m3u8_candidates(body, final_url)
        all_candidates.extend(candidates)
        if candidates:
            chosen = pick_best_playlist(candidates)
            if events is not None:
                events.put(("log", f"Нашёл плейлист: {chosen}"))
                events.put(("autofill", {"referer": final_url}))
            return PageResolve(chosen, title, final_url, browser=True, thumbnail=thumbnail)
        api_result = try_stream_api(client, final_url, body, events)
        if api_result:
            api_stream, api_thumb = api_result
            if api_thumb:
                thumbnail = api_thumb
                if events is not None:
                    events.put(("thumbnail", thumbnail))
            if events is not None:
                events.put(("log", f"Нашёл плейлист: {api_stream}"))
                events.put(("autofill", {"referer": final_url}))
            return PageResolve(api_stream, title, final_url, browser=True, thumbnail=thumbnail)
        direct = extract_direct_media_candidates(body, final_url)
        all_direct.extend(direct)
        if direct:
            chosen = pick_best_direct(direct)
            if events is not None:
                events.put(("log", f"Нашёл MP4: {chosen}"))
                events.put(("autofill", {"referer": final_url}))
            return PageResolve(chosen, title, final_url, browser=True, thumbnail=thumbnail)
        if looks_like_upns_player(final_url, body) and upns_video_id_from_url(final_url):
            try:
                stream, upns_title, upns_thumb = resolve_upns_embed(
                    client,
                    final_url,
                    page_referer=referer or page_url,
                    events=events,
                )
                if upns_title:
                    title = upns_title
                if upns_thumb:
                    thumbnail = upns_thumb
                    if events is not None:
                        events.put(("thumbnail", thumbnail))
                if events is not None:
                    events.put(("autofill", {"referer": final_url}))
                return PageResolve(stream, title, final_url, browser=True, thumbnail=thumbnail)
            except PlaylistError as exc:
                if events is not None:
                    events.put(("log", f"upns: {exc}"))
                # If this embed is the only lead, surface the hoster error later.
                last_upns_error = str(exc)
        for follow in extract_follow_urls(body, final_url):
            if follow not in seen:
                if events is not None:
                    events.put(("log", f"Открываю плеер: {follow}"))
                queue_urls.append((follow, final_url))

    if all_candidates:
        chosen = pick_best_playlist(all_candidates)
        return PageResolve(chosen, title, last_referer, browser=True, thumbnail=thumbnail)
    if all_direct:
        chosen = pick_best_direct(all_direct)
        return PageResolve(chosen, title, last_referer, browser=True, thumbnail=thumbnail)

    if last_upns_error:
        raise PlaylistError(last_upns_error)

    # Last resort: yt-dlp with impersonation
    ytdlp = ensure_yt_dlp(events)
    if events is not None:
        events.put(("status", "Ищу видео через yt-dlp…"))
        events.put(("log", "Пробую yt-dlp"))
        command = [
        str(ytdlp),
        "--no-playlist",
        "--no-warnings",
        "--impersonate",
        "Chrome-131:Macos-14",
        "-J",
        "--ffmpeg-location",
        str(FFMPEG_PATH.parent if FFMPEG_PATH.exists() else APP_DIR / "bin"),
        "--referer",
        page_url,
        page_url,
    ]
    command = append_ytdlp_cookies(command)
    try:
        process = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
            timeout=180,
        )
    except subprocess.TimeoutExpired as exc:
        raise PlaylistError("yt-dlp слишком долго искал видео на странице") from exc
    if process.returncode != 0:
        detail = (process.stderr or process.stdout or "").strip()
        raise PlaylistError(
            "Не удалось найти видео на этой странице.\n" + (detail[-400:] if detail else "")
        )
    info = json.loads(process.stdout)
    title = info.get("title") or title
    # YouTube-style extractors: never take raw HLS here — it is often audio-only (itag 234).
    extractor = str(info.get("extractor") or info.get("extractor_key") or "").lower()
    webpage = str(info.get("webpage_url") or page_url)
    if "youtube" in extractor or prefers_ytdlp_download(webpage):
        thumb = pick_ytdlp_thumbnail(info) or thumbnail
        if thumb and events is not None:
            events.put(("thumbnail", thumb))
        return PageResolve(f"ytdlp:{webpage}", title, page_url, browser=False, thumbnail=thumb)
    formats = info.get("formats") or []
    hls: list[str] = []
    for item in formats:
        if not format_has_video(item):
            continue
        url = item.get("url") or ""
        protocol = (item.get("protocol") or "").lower()
        if ".m3u8" in url.lower() or "m3u8" in protocol or protocol.startswith("hls"):
            hls.append(url)
    if info.get("url") and ".m3u8" in str(info.get("url")).lower() and format_has_video(info):
        hls.append(info["url"])
    hls = unique_urls(hls)
    if hls:
        chosen = pick_best_playlist(hls)
        thumb = pick_ytdlp_thumbnail(info) or thumbnail
        if thumb and events is not None:
            events.put(("thumbnail", thumb))
        return PageResolve(chosen, title, page_url, browser=True, thumbnail=thumb)
    thumb = pick_ytdlp_thumbnail(info) or thumbnail
    if thumb and events is not None:
        events.put(("thumbnail", thumb))
    return PageResolve(f"ytdlp:{page_url}", title, page_url, browser=False, thumbnail=thumb)


def download_hls_with_browser(
    playlist_url: str,
    output: Path,
    referer: str,
    headers: dict[str, str],
    choice_url: str | None,
    limit_seconds: int | None,
    events: queue.Queue,
    cancel: threading.Event,
    threads: int = DEFAULT_THREADS,
) -> None:
    """Download Cloudflare-protected HLS by fetching segments as Chrome, then remux with ffmpeg."""
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        raise PlaylistError("Не найден ffmpeg.exe")
    client = BrowserClient(headers)
    events.put(("status", "Читаю плейлист…"))
    events.put(("log", f"Скачиваю через браузерный режим: {playlist_url}"))
    text, final_url, _used = client.get_text(playlist_url, referer=referer)
    playlist = parse_playlist(text, final_url)
    media_url = final_url
    label = "поток"
    if playlist.is_master:
        variant = select_variant(playlist, choice_url)
        if variant is None:
            raise PlaylistError("В мастер-плейлисте нет видео")
        label = variant.label()
        media_text, media_url, _ = client.get_text(variant.url, referer=referer or final_url)
        playlist = parse_playlist(media_text, media_url)
    if image_segments(playlist):
        # reuse existing image/TS-wrapped path via Resolved
        resolved = Resolved(
            video_url=media_url,
            audio_url=None,
            is_vod=playlist.is_vod,
            duration=playlist.duration,
            label=label,
            headers={"Referer": referer or page_origin(playlist_url)},
            segments=image_segments(playlist),
            browser=True,
            page_referer=referer,
        )
        encode_slideshow(
            ffmpeg,
            resolved,
            output,
            resolved.headers,
            False,
            limit_seconds,
            events,
            cancel,
            threads=threads,
        )
        return

    segments = limit_segments(playlist.segments, limit_seconds)
    if not segments:
        raise PlaylistError("В плейлисте нет сегментов")
    folder = APP_DIR / "temp" / f"hls-{uuid_hex()}"
    folder.mkdir(parents=True, exist_ok=True)
    try:
        workers = min(clamp_threads(threads), len(segments))
        events.put(("log", f"Сегментов: {len(segments)}. Потоков: {workers}"))
        saved: list[Path | None] = [None] * len(segments)
        thread_local = threading.local()

        def worker_client() -> BrowserClient:
            client = getattr(thread_local, "client", None)
            if client is None:
                client = BrowserClient(headers)
                thread_local.client = client
            return client

        def worker(index: int, segment: Segment) -> Path:
            if cancel.is_set():
                raise PlaylistError("Остановлено")
            data = None
            last_error: Exception | None = None
            for attempt in range(5):
                try:
                    data = worker_client().get_bytes(segment.url, referer=referer or media_url, timeout=90)
                    break
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    # Drop the session after a hard failure so the next try is clean.
                    if hasattr(thread_local, "client"):
                        delattr(thread_local, "client")
                    time.sleep(min(2.0, 0.25 * (2**attempt)))
            if data is None:
                raise PlaylistError(f"Сегмент {index + 1}: {last_error}")
            path = folder / f"{index:05d}.ts"
            path.write_bytes(data)
            return path

        finished = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(worker, index, segment): index for index, segment in enumerate(segments)}
            for future in as_completed(futures):
                if cancel.is_set():
                    pool.shutdown(wait=False, cancel_futures=True)
                    events.put(("done", {"cancelled": True, "output": str(output), "vod": True}))
                    return
                index = futures[future]
                saved[index] = future.result()
                finished += 1
                if finished == 1 or finished == len(segments) or finished % max(1, len(segments) // 50) == 0:
                    events.put(("progress", {"percent": finished / len(segments) * 90}))
                    events.put(("status", f"Скачиваю сегменты {finished} из {len(segments)} · {workers} потоков"))

        parts = [path for path in saved if path is not None]
        if not parts:
            raise PlaylistError("Не удалось скачать сегменты")
        ts_path = folder / "video.ts"
        with ts_path.open("wb") as stream:
            for part in parts:
                with part.open("rb") as src:
                    shutil.copyfileobj(src, stream, length=1024 * 1024)
        duration = sum(segment.duration for segment in segments)
        remux_ts_to_mp4(ffmpeg, ts_path, output, duration, events, cancel)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def direct_download_headers(
    media_url: str,
    referer: str,
    headers: dict[str, str],
) -> dict[str, str]:
    result = {
        "User-Agent": headers.get("User-Agent") or DEFAULT_UA,
        "Referer": (referer or page_origin(media_url)).strip(),
        "Accept": "*/*",
    }
    origin = headers.get("Origin")
    if origin:
        result["Origin"] = origin
    return result


def direct_parallel_workers(threads: int, media_url: str = "") -> int:
    """Parallel Range connections for progressive MP4 (mixdrop/mxcontent/etc.)."""
    del media_url
    return max(1, clamp_threads(threads))


def probe_direct_media_size(
    media_url: str,
    referer: str,
    headers: dict[str, str],
) -> int | None:
    req_headers = direct_download_headers(media_url, referer, headers)
    req_headers["Range"] = "bytes=0-0"
    request = urllib.request.Request(media_url, headers=req_headers)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            content_range = response.headers.get("Content-Range") or ""
            if "/" in content_range:
                total = content_range.rsplit("/", 1)[-1].strip()
                if total.isdigit() and int(total) > 0:
                    return int(total)
            length = response.headers.get("Content-Length")
            if length and length.isdigit() and int(length) > 1:
                return int(length)
    except Exception:
        return None
    return None


def download_direct_with_browser(
    media_url: str,
    output: Path,
    referer: str,
    headers: dict[str, str],
    limit_seconds: int | None,
    events: queue.Queue,
    cancel: threading.Event,
    threads: int = DEFAULT_DIRECT_THREADS,
) -> None:
    """Download a progressive MP4/WebM. Full files use parallel Range requests via urllib."""
    events.put(("status", "Скачиваю MP4…"))
    events.put(("log", f"Прямой файл: {media_url}"))
    ffmpeg = find_ffmpeg()

    # Short clip: single ffmpeg connection is enough and stops early.
    if limit_seconds and ffmpeg is not None:
        header_lines = [
            f"User-Agent: {headers.get('User-Agent') or DEFAULT_UA}",
            f"Referer: {referer or page_origin(media_url)}",
            "Accept: */*",
        ]
        origin = headers.get("Origin")
        if origin:
            header_lines.append(f"Origin: {origin}")
        header_blob = "\r\n".join(header_lines) + "\r\n"
        command = [
            str(ffmpeg),
            "-hide_banner",
            "-y",
            "-loglevel",
            "warning",
            "-stats",
            "-headers",
            header_blob,
            "-t",
            str(limit_seconds),
            "-i",
            media_url,
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(output),
        ]
        events.put(("log", "Скачиваю фрагмент через ffmpeg…"))
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        events.put(("proc", process))
        assert process.stdout is not None
        last_pct = -1
        for line in process.stdout:
            if cancel.is_set():
                process.terminate()
                events.put(("done", {"cancelled": True, "output": str(output), "vod": True}))
                return
            text = line.strip()
            if not text:
                continue
            match = re.search(r"time=\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
            if match:
                seconds = int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))
                pct = min(99.0, seconds / max(limit_seconds, 1) * 100)
                if int(pct) != last_pct:
                    last_pct = int(pct)
                    events.put(("progress", {"percent": pct, "time": seconds}))
                    events.put(("status", f"Скачиваю MP4… {fmt_duration(seconds)}"))
        code = process.wait()
        if cancel.is_set():
            events.put(("done", {"cancelled": True, "output": str(output), "vod": True}))
            return
        if code == 0 and output.exists() and output.stat().st_size > 1024:
            events.put(("progress", {"percent": 100.0}))
            events.put(("done", {"ok": True, "output": str(output), "vod": True}))
            return
        events.put(("log", "ffmpeg не смог скачать фрагмент, пробую потоковую загрузку…"))

    total = probe_direct_media_size(media_url, referer, headers)
    workers = direct_parallel_workers(threads, media_url)
    base_headers = direct_download_headers(media_url, referer, headers)

    # Parallel Range download for sizable files (CDN often caps per-connection speed).
    if total and total >= 2 * 1024 * 1024 and workers > 1:
        # Fewer, larger chunks → less connection churn on soft CDNs.
        part_count = max(workers * 4, workers)
        chunk = (total + part_count - 1) // part_count
        ranges: list[tuple[int, int, int]] = []
        for index in range(part_count):
            start = index * chunk
            if start >= total:
                break
            end = min(total - 1, start + chunk - 1)
            ranges.append((index, start, end))
        events.put(
            (
                "log",
                f"Режим: параллельный MP4 · {workers} потоков · {len(ranges)} кусков · {fmt_size(total)}",
            )
        )
        part_path = output.with_suffix(output.suffix + ".part")
        progress_path = output.with_suffix(output.suffix + ".progress.json")
        completed: set[int] = set()
        if part_path.exists() and part_path.stat().st_size == total and progress_path.exists():
            try:
                raw = json.loads(progress_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and raw.get("total") == total and isinstance(raw.get("done"), list):
                    completed = {int(x) for x in raw["done"] if isinstance(x, int)}
                    events.put(("log", f"Продолжаю загрузку: уже готово {len(completed)}/{len(ranges)} кусков"))
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                completed = set()
        if not part_path.exists() or part_path.stat().st_size != total:
            if part_path.exists():
                part_path.unlink(missing_ok=True)
            progress_path.unlink(missing_ok=True)
            completed = set()
            with part_path.open("wb") as handle:
                handle.truncate(total)

        done_bytes = sum((end - start + 1) for index, start, end in ranges if index in completed)
        lock = threading.Lock()
        last_pct = -1
        meter = SpeedMeter()
        pending = [item for item in ranges if item[0] not in completed]
        if not pending:
            if output.exists():
                output.unlink(missing_ok=True)
            part_path.replace(output)
            progress_path.unlink(missing_ok=True)
            err = verify_mp4_file(output, total)
            if err:
                raise PlaylistError(err)
            events.put(("progress", {"percent": 100.0}))
            events.put(("done", {"ok": True, "output": str(output), "vod": True}))
            return

        def save_progress() -> None:
            try:
                progress_path.write_text(
                    json.dumps({"total": total, "done": sorted(completed)}, ensure_ascii=False),
                    encoding="utf-8",
                )
            except OSError:
                pass

        def fetch_range(item: tuple[int, int, int]) -> None:
            nonlocal done_bytes, last_pct
            index, start, end = item
            expected = end - start + 1
            last_error: Exception | None = None
            for attempt in range(8):
                if cancel.is_set():
                    raise PlaylistError("Остановлено")
                try:
                    req_headers = dict(base_headers)
                    req_headers["Range"] = f"bytes={start}-{end}"
                    request = urllib.request.Request(media_url, headers=req_headers)
                    with urllib.request.urlopen(request, timeout=180) as response:
                        status = getattr(response, "status", 200)
                        if status == 200:
                            raise PlaylistError("Сервер не поддерживает параллельную загрузку (Range)")
                        if status not in (206,):
                            raise PlaylistError(f"Ошибка Range {status}")
                        written = 0
                        with part_path.open("r+b") as handle:
                            handle.seek(start)
                            while True:
                                if cancel.is_set():
                                    raise PlaylistError("Остановлено")
                                block = response.read(256 * 1024)
                                if not block:
                                    break
                                handle.write(block)
                                written += len(block)
                    if written < expected:
                        raise PlaylistError(f"Неполный кусок: {written} из {expected}")
                    with lock:
                        completed.add(index)
                        done_bytes += written
                        save_progress()
                        pct = min(99.0, done_bytes / total * 100)
                        speed, eta = meter.update(done_bytes, total)
                        if int(pct) != last_pct:
                            last_pct = int(pct)
                            events.put(("progress", {"percent": pct, "speed": speed, "eta": eta}))
                            events.put(
                                (
                                    "status",
                                    f"Скачано {fmt_size(done_bytes)} / {fmt_size(total)}"
                                    + (f" · {speed}" if speed else "")
                                    + (f" · ETA {eta}" if eta else ""),
                                )
                            )
                    return
                except PlaylistError as exc:
                    if "Остановлено" in str(exc) or "не поддерживает параллельную" in str(exc):
                        raise
                    last_error = exc
                    time.sleep(min(8.0, 0.6 * (attempt + 1) ** 1.4))
                except urllib.error.HTTPError as exc:
                    last_error = exc
                    # Soft CDN rate-limit — back off harder on 429/503.
                    delay = 2.0 * (attempt + 1) if exc.code in {429, 503, 502} else 0.5 * (attempt + 1)
                    time.sleep(min(12.0, delay))
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    time.sleep(min(6.0, 0.5 * (attempt + 1)))
            raise PlaylistError(str(last_error) if last_error else "Ошибка параллельной загрузки")

        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(fetch_range, item) for item in pending]
                try:
                    for future in as_completed(futures):
                        if cancel.is_set():
                            for item in futures:
                                item.cancel()
                            pool.shutdown(wait=False, cancel_futures=True)
                            events.put(("done", {"cancelled": True, "output": str(output), "vod": True}))
                            return
                        future.result()
                except Exception:
                    for item in futures:
                        item.cancel()
                    pool.shutdown(wait=False, cancel_futures=True)
                    raise
            if output.exists():
                output.unlink(missing_ok=True)
            part_path.replace(output)
            progress_path.unlink(missing_ok=True)
            err = verify_mp4_file(output, total)
            if err:
                raise PlaylistError(err)
            events.put(("progress", {"percent": 100.0}))
            events.put(("done", {"ok": True, "output": str(output), "vod": True}))
            return
        except PlaylistError as exc:
            events.put(("log", f"Параллельная загрузка не удалась: {exc}; один поток"))
            if "Остановлено" in str(exc):
                events.put(("done", {"cancelled": True, "output": str(output), "vod": True}))
                return
            part_path.unlink(missing_ok=True)
            progress_path.unlink(missing_ok=True)
            time.sleep(1.5)
        except Exception as exc:  # noqa: BLE001
            events.put(("log", f"Параллельная загрузка не удалась: {exc}; один поток"))
            part_path.unlink(missing_ok=True)
            progress_path.unlink(missing_ok=True)
            time.sleep(1.5)

    # Single-connection fallback (urllib — curl_cffi иногда ловит 403 на этих CDN)
    request_headers = dict(base_headers)
    part = output.with_suffix(output.suffix + ".part")
    written = 0
    resume_from = 0
    if part.exists() and not limit_seconds:
        resume_from = part.stat().st_size
        if total and resume_from >= total:
            if output.exists():
                output.unlink(missing_ok=True)
            part.replace(output)
            events.put(("progress", {"percent": 100.0}))
            events.put(("done", {"ok": True, "output": str(output), "vod": True}))
            return
        if resume_from > 0:
            request_headers["Range"] = f"bytes={resume_from}-"
            written = resume_from
            events.put(("log", f"Продолжаю с {fmt_size(resume_from)}"))
    response = None
    last_open_error: Exception | None = None
    for attempt in range(5):
        if cancel.is_set():
            events.put(("done", {"cancelled": True, "output": str(output), "vod": True}))
            return
        try:
            request = urllib.request.Request(media_url, headers=request_headers)
            response = urllib.request.urlopen(request, timeout=180)
            break
        except urllib.error.HTTPError as exc:
            last_open_error = exc
            if exc.code in {429, 502, 503}:
                time.sleep(min(10.0, 1.5 * (attempt + 1)))
                continue
            raise PlaylistError(f"Ошибка запроса при скачивании MP4: {exc}") from exc
        except Exception as exc:
            last_open_error = exc
            time.sleep(min(6.0, 0.8 * (attempt + 1)))
    if response is None:
        raise PlaylistError(f"Ошибка запроса при скачивании MP4: {last_open_error}")
    status = getattr(response, "status", 200)
    if resume_from and status == 200:
        # Server ignored Range — restart.
        response.close()
        part.unlink(missing_ok=True)
        written = 0
        request = urllib.request.Request(media_url, headers=base_headers)
        response = urllib.request.urlopen(request, timeout=180)
    if not total:
        length = response.headers.get("Content-Length")
        if length and length.isdigit():
            total = int(length) + (written if status == 206 else 0)
    last_pct = -1
    meter = SpeedMeter()
    meter.last_bytes = written
    open_mode = "ab" if written > 0 else "wb"
    try:
        with part.open(open_mode) as out:
            while True:
                if cancel.is_set():
                    events.put(("done", {"cancelled": True, "output": str(output), "vod": True}))
                    return
                chunk_data = response.read(256 * 1024)
                if not chunk_data:
                    break
                out.write(chunk_data)
                written += len(chunk_data)
                if total > 0:
                    pct = min(99.0, written / total * (90.0 if limit_seconds else 99.0))
                    if int(pct) != last_pct:
                        last_pct = int(pct)
                        speed, eta = meter.update(written, total)
                        events.put(("progress", {"percent": pct, "speed": speed, "eta": eta}))
                        events.put(
                            (
                                "status",
                                f"Скачано {fmt_size(written)} / {fmt_size(total)}"
                                + (f" · {speed}" if speed else ""),
                            )
                        )
                elif written and written % (5 * 1024 * 1024) < 256 * 1024:
                    speed, _eta = meter.update(written, None)
                    events.put(("status", f"Скачано {fmt_size(written)}" + (f" · {speed}" if speed else "")))
        response.close()
        if written < 1024:
            raise PlaylistError("Скачанный файл слишком маленький — возможно, ссылка устарела")

        if limit_seconds:
            if ffmpeg is None:
                raise PlaylistError("Не найден ffmpeg.exe")
            events.put(("status", "Обрезаю и собираю MP4…"))
            command = [
                str(ffmpeg),
                "-hide_banner",
                "-y",
                "-loglevel",
                "warning",
                "-nostats",
                "-i",
                str(part),
                "-t",
                str(limit_seconds),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(output),
            ]
            process = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=CREATE_NO_WINDOW,
            )
            if process.returncode != 0:
                raise PlaylistError((process.stderr or process.stdout or "ffmpeg error").strip()[-400:])
        else:
            if output.exists():
                output.unlink(missing_ok=True)
            part.replace(output)
        err = verify_mp4_file(output, None if limit_seconds else (total or None))
        if err:
            events.put(("log", f"Предупреждение проверки: {err}"))
        events.put(("progress", {"percent": 100.0}))
        events.put(("done", {"ok": True, "output": str(output), "vod": True}))
    finally:
        try:
            response.close()
        except Exception:
            pass
        if limit_seconds and part.exists() and output.exists():
            part.unlink(missing_ok=True)


def youtube_safe_format(format_spec: str | None) -> str:
    spec = (format_spec or "").strip() or YTDLP_YT_FORMAT_BEST
    if spec in {"bv*+ba/b", "best", "b"}:
        return YTDLP_YT_FORMAT_BEST
    match = re.fullmatch(r"bv\*\[height<=(\d+)\]\+ba/b", spec)
    if match:
        return YTDLP_YT_FORMAT_HEIGHT.format(height=match.group(1))
    if "+ba/b" in spec and "ba[ext=m4a]" not in spec:
        return spec.replace("+ba/b", "+ba[ext=m4a]/bv*+ba/b")
    return spec


def download_with_ytdlp(
    page_url: str,
    output: Path,
    headers: dict[str, str],
    proxy: str,
    limit_seconds: int | None,
    events: queue.Queue,
    cancel: threading.Event,
    format_spec: str = "bv*+ba/b",
    threads: int = DEFAULT_THREADS,
    audio_multistreams: bool = False,
    use_fast_youtube_client: bool = True,
) -> None:
    ytdlp = ensure_yt_dlp(events)
    ffmpeg = find_ffmpeg()
    workers = clamp_threads(threads)
    # VK progressive url* is single-connection and slow; force HLS when caller still
    # passes the generic YouTube-style selector.
    if is_vk_url(page_url) and (not format_spec or format_spec in {"bv*+ba/b", "best", "b"}):
        format_spec = "b[protocol^=m3u8]/bv*[protocol^=m3u8]+ba/bv*+ba/b"
    # Prefer H.264 progressive over AV1 — wider remux/player compatibility.
    if is_eporner_url(page_url) and (not format_spec or format_spec in {"bv*+ba/b", "best", "b"}):
        format_spec = "1080p_HD/720p_HD/480p/360p/240p/best[format_id!*=av1]/best"
    if is_youtube_url(page_url) and not audio_multistreams:
        format_spec = youtube_safe_format(format_spec)

    try:
        proxy_norm = normalize_proxy_url(proxy) if proxy.strip() else ""
    except PlaylistError:
        proxy_norm = proxy.strip()

    # YouTube attempts: web+m4a first (full quality). Android clients often only expose 360p.
    attempts: list[tuple[str, bool, str | None]] = []
    if is_youtube_url(page_url) and not audio_multistreams:
        primary = format_spec or YTDLP_YT_FORMAT_BEST
        attempts.append((primary, False, None))
        attempts.append((primary, True, "android,tv,mweb"))
        attempts.append(("bv*[ext=mp4]+ba[ext=m4a]/b", False, None))
        attempts.append(("b", True, "android,tv,mweb"))
    else:
        attempts.append((format_spec or "bv*+ba/b", use_fast_youtube_client, None))

    template = output.with_suffix("")
    last_error = ""
    for attempt_index, (fmt, fast_client, client_args) in enumerate(attempts):
        if cancel.is_set():
            events.put(("done", {"cancelled": True, "output": str(output), "vod": True}))
            return
        # Clean partials from a failed attempt.
        for leftover in output.parent.glob(template.name + ".*"):
            if leftover.suffix.lower() in {".part", ".ytdl", ".temp"}:
                leftover.unlink(missing_ok=True)
        command = [
            str(ytdlp),
            "--no-playlist",
            "--no-warnings",
            "-f",
            fmt,
            "--merge-output-format",
            "mp4",
            "-N",
            str(workers),
            "--concurrent-fragments",
            str(workers),
            "--retries",
            "15",
            "--fragment-retries",
            "15",
            "--retry-sleep",
            "fragment:exp=1:20",
            "--socket-timeout",
            "30",
            "-o",
            str(template) + ".%(ext)s",
            "--newline",
        ]
        if not audio_multistreams:
            command += ["--remux-video", "mp4"]
        if audio_multistreams:
            command.append("--audio-multistreams")
        if is_youtube_url(page_url) and fast_client and not audio_multistreams:
            extractor = f"youtube:player_client={client_args}" if client_args else YTDLP_YT_FAST_CLIENT
            command += ["--extractor-args", extractor]
        aria2 = None if prefers_ytdlp_download(page_url) else find_aria2c()
        if aria2 is not None:
            conn = min(16, workers)
            command += [
                "--downloader",
                str(aria2),
                "--downloader-args",
                f"aria2c:-x {conn} -s {conn} -k 1M -j {conn}",
            ]
            events.put(("log", f"Режим: yt-dlp + aria2c · {conn} соединений"))
        else:
            mode = "мультиаудио" if audio_multistreams else "yt-dlp"
            events.put(("log", f"Режим: {mode} · {workers} фрагментов параллельно"))
            events.put(("log", f"Формат: {fmt}" + (f" · клиент {client_args}" if client_args else "")))
        if ffmpeg is not None:
            command += ["--ffmpeg-location", str(ffmpeg.parent)]
        if headers.get("User-Agent"):
            command += ["--user-agent", headers["User-Agent"]]
        if headers.get("Referer"):
            command += ["--referer", headers["Referer"]]
        cookies_file = str(getattr(_DOWNLOAD_CTX, "cookies", "") or "")
        command += ytdlp_cookies_args(cookies_file)
        if proxy_norm:
            command += ["--proxy", proxy_norm]
        if limit_seconds:
            command += ["--download-sections", f"*0-{limit_seconds}"]
        command.append(page_url)
        if attempt_index:
            events.put(("log", f"Повтор YouTube ({attempt_index + 1}/{len(attempts)})…"))
        events.put(("status", "Скачиваю через yt-dlp…"))
        events.put(("log", " ".join(command[:8]) + " …"))
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
            env=proxy_env(proxy_norm or current_proxy()),
        )
        events.put(("proc", process))
        lines: list[str] = []
        assert process.stdout is not None
        speed_re = re.compile(r"at\s+([0-9.,]+\s*[KMG]?i?B/s)", re.I)
        eta_re = re.compile(r"ETA\s+(\d+:\d+(?::\d+)?)", re.I)
        try:
            for line in process.stdout:
                if cancel.is_set():
                    process.terminate()
                    break
                text = line.strip()
                if not text:
                    continue
                lines.append(text)
                if not text.startswith("[download]") or "%" in text or "Destination" in text or "error" in text.lower():
                    if "Downloading" in text or "Destination" in text or "error" in text.lower() or "Merging" in text:
                        events.put(("log", text))
                percent = None
                match = re.search(r"(\d{1,3}(?:\.\d+)?)%", text)
                if match:
                    percent = float(match.group(1))
                    events.put(("progress", {"percent": min(percent, 99.0)}))
                speed_m = speed_re.search(text)
                eta_m = eta_re.search(text)
                progress_payload: dict = {}
                if percent is not None:
                    progress_payload["percent"] = min(percent, 99.0)
                if speed_m:
                    progress_payload["speed"] = speed_m.group(1).replace(" ", "")
                    events.put(("speed", progress_payload["speed"]))
                if eta_m:
                    progress_payload["eta"] = eta_m.group(1)
                if progress_payload:
                    events.put(("progress", progress_payload))
                if percent is not None or speed_m:
                    status_bits = [text[:80]]
                    if speed_m:
                        status_bits = [f"yt-dlp {percent or 0:.1f}% · {speed_m.group(1)}"]
                    events.put(("status", status_bits[0][:120]))
        finally:
            if cancel.is_set() and process.poll() is None:
                process.terminate()
            code = process.wait()
        if cancel.is_set():
            events.put(("done", {"cancelled": True, "output": str(output), "vod": True}))
            return
        if code == 0:
            produced = None
            for candidate in sorted(
                output.parent.glob(template.name + ".*"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            ):
                if candidate.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}:
                    produced = candidate
                    break
            if produced is None:
                last_error = "yt-dlp не создал видеофайл"
                continue
            if produced.resolve() != output.resolve():
                if output.exists():
                    output.unlink()
                produced.replace(output)
            warn = verify_mp4_file(output)
            if warn:
                events.put(("log", f"Проверка файла: {warn}"))
            events.put(("progress", {"percent": 100.0}))
            events.put(("done", {"ok": True, "output": str(output), "mode": "ytdlp"}))
            return
        last_error = "\n".join(lines[-20:]) or f"yt-dlp завершился с кодом {code}"
        joined = "\n".join(lines)
        if is_eporner_url(page_url) and "unable to extract hash" in joined.lower():
            try:
                ensure_curl_cffi(events)
                client = BrowserClient(headers)
                _, _, body, _ = probe_browser_url(client, page_url, referer=page_url)
                reason = eporner_unavailable_reason(body)
                if reason:
                    raise PlaylistError(reason)
            except PlaylistError:
                raise
            except Exception:
                pass
            raise PlaylistError("Не удалось получить видео с eporner (страница без плеера).")
        retryable = any(token in joined for token in ("403", "Forbidden", "HTTP Error 403", "ffmpeg exited"))
        if not retryable or attempt_index >= len(attempts) - 1:
            break
        events.put(("log", "YouTube CDN отказал (403) — пробую другой клиент/формат"))
    raise PlaylistError(last_error)

def materialize_source(source: str) -> tuple[str, Path | None]:
    text = source.strip().lstrip("\ufeff")
    if text.startswith("#EXTM3U"):
        if not re.search(r"https?://", text, flags=re.IGNORECASE):
            raise PlaylistError(
                "В тексте плейлиста нет полных ссылок. Вставьте адрес m3u8, а не сам список сегментов."
            )
        folder = APP_DIR / "temp"
        folder.mkdir(exist_ok=True)
        path = folder / "playlist.m3u8"
        path.write_text(text, encoding="utf-8")
        return str(path), path
    first = text.splitlines()[0].strip().strip('"')
    if first.startswith("http://") or first.startswith("https://") or Path(first).exists():
        return first, None
    raise PlaylistError("Вставьте ссылку на страницу с видео или на m3u8 (http:// / https://).")


def parse_url_list(source: str) -> list[str]:
    text = source.strip().lstrip("\ufeff")
    if not text:
        return []
    if text.startswith("#EXTM3U"):
        return [text]
    urls: list[str] = []
    for line in text.splitlines():
        item = line.strip().strip('"').strip("'")
        if not item or item.startswith("#"):
            continue
        if item.startswith("http://") or item.startswith("https://") or Path(item).exists():
            urls.append(item)
    return unique_urls(urls)


class ProxyEvents:
    """Forwards worker events to the UI queue, optionally holding back 'done'."""

    def __init__(self, real: queue.Queue, suppress_done: bool = False) -> None:
        self.real = real
        self.suppress_done = suppress_done
        self.last_done: dict | None = None

    def put(self, item: tuple) -> None:
        kind, payload = item
        if self.suppress_done and kind == "done":
            self.last_done = payload if isinstance(payload, dict) else {"ok": False, "error": str(payload)}
            return
        self.real.put(item)


class IndexedEvents:
    """Tag worker events with a queue card index for parallel UI updates."""

    def __init__(self, real: queue.Queue, index: int, suppress_done: bool = False) -> None:
        self.real = real
        self.index = index
        self.suppress_done = suppress_done
        self.last_done: dict | None = None

    def put(self, item: tuple) -> None:
        kind, payload = item
        if self.suppress_done and kind == "done":
            self.last_done = payload if isinstance(payload, dict) else {"ok": False, "error": str(payload)}
            return
        self.real.put(("card_event", {"index": self.index, "kind": kind, "payload": payload}))


def formats_look_muxed(info: dict) -> bool:
    """True when video formats already include audio (no separate video+audio pair)."""
    video_only = 0
    muxed = 0
    for item in info.get("formats") or []:
        if not isinstance(item, dict) or not format_has_video(item):
            continue
        acodec = str(item.get("acodec") or "none").lower()
        if acodec in ("none", ""):
            video_only += 1
        else:
            muxed += 1
    return muxed > 0 and video_only == 0


def prefers_hls_download(info: dict) -> bool:
    """Prefer HLS when progressive single-connection formats are also listed (VK url1080 etc.)."""
    extractor = str(info.get("extractor") or info.get("extractor_key") or "").lower()
    if extractor.startswith("vk"):
        return True
    has_hls = False
    has_progressive = False
    for item in info.get("formats") or []:
        if not isinstance(item, dict) or not format_has_video(item):
            continue
        protocol = str(item.get("protocol") or "").lower()
        url = str(item.get("url") or "").lower()
        format_id = str(item.get("format_id") or "").lower()
        if "m3u8" in protocol or ".m3u8" in url:
            has_hls = True
        elif format_id.startswith("url") and protocol.startswith("http"):
            has_progressive = True
    return has_hls and has_progressive


def ytdlp_quality_options(info: dict) -> list[dict]:
    """Build quality list; first item is always maximum/best."""
    heights: list[int] = []
    seen: set[int] = set()
    for item in info.get("formats") or []:
        if not isinstance(item, dict) or not format_has_video(item):
            continue
        height = item.get("height")
        if not isinstance(height, int) or height <= 0 or height in seen:
            continue
        seen.add(height)
        heights.append(height)
    heights.sort(reverse=True)
    muxed = formats_look_muxed(info)
    prefer_hls = prefers_hls_download(info)
    if prefer_hls:
        # Progressive VK urls are single-connection and throttled; HLS uses -N fragments.
        options = [
            {
                "label": "Максимальное (лучшее)",
                "ytdlp_format": "b[protocol^=m3u8]/bv*[protocol^=m3u8]+ba/bv*+ba/b",
                "video_format": "b[protocol^=m3u8]/bv*[protocol^=m3u8]",
                "choice_url": None,
            }
        ]
        for height in heights:
            options.append(
                {
                    "label": f"{height}p",
                    "ytdlp_format": (
                        f"b[height<={height}][protocol^=m3u8]/"
                        f"bv*[height<={height}][protocol^=m3u8]+ba/"
                        f"bv*[height<={height}]+ba/b"
                    ),
                    "video_format": (
                        f"b[height<={height}][protocol^=m3u8]/"
                        f"bv*[height<={height}][protocol^=m3u8]"
                    ),
                    "choice_url": None,
                }
            )
        return options
    if muxed:
        options = [
            {
                "label": "Максимальное (лучшее)",
                "ytdlp_format": "best",
                "video_format": "best",
                "choice_url": None,
            }
        ]
        for height in heights:
            options.append(
                {
                    "label": f"{height}p",
                    "ytdlp_format": f"best[height<={height}]",
                    "video_format": f"best[height<={height}]",
                    "choice_url": None,
                }
            )
        return options
    options = [
        {
            "label": "Максимальное (лучшее)",
            "ytdlp_format": YTDLP_YT_FORMAT_BEST,
            "video_format": "bv*",
            "choice_url": None,
        }
    ]
    for height in heights:
        options.append(
            {
                "label": f"{height}p",
                "ytdlp_format": YTDLP_YT_FORMAT_HEIGHT.format(height=height),
                "video_format": f"bv*[height<={height}]",
                "choice_url": None,
            }
        )
    return options


def ytdlp_audio_tracks(info: dict) -> list[dict]:
    """Unique language audio tracks (best m4a/opus per language) from yt-dlp JSON."""
    best: dict[str, dict] = {}
    for item in info.get("formats") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("vcodec") or "none").lower() not in ("none", ""):
            continue
        if str(item.get("acodec") or "none").lower() in ("none", ""):
            continue
        protocol = str(item.get("protocol") or "").lower()
        if "m3u8" in protocol:
            continue
        lang = str(item.get("language") or "und")
        note = str(item.get("format_note") or "")
        abr = float(item.get("tbr") or item.get("abr") or 0)
        ext = str(item.get("ext") or "")
        format_id = str(item.get("format_id") or "")
        if not format_id:
            continue
        is_original = "original" in note.lower() or "default" in note.lower()
        # Prefer AAC/m4a for MP4 merge, then higher bitrate, then original flag.
        score = (1 if ext == "m4a" else 0, abr, 1 if is_original else 0)
        current = best.get(lang)
        if current is not None and score <= current["_score"]:
            continue
        label = note.split(",")[0].strip() if note else lang
        if is_original and "original" not in label.lower():
            label = f"{label} (оригинал)" if label else "Оригинал"
        best[lang] = {
            "language": lang,
            "format_id": format_id,
            "label": label or lang,
            "ext": ext,
            "abr": abr,
            "original": is_original,
            "_score": score,
        }
    rows = list(best.values())
    for row in rows:
        row.pop("_score", None)
    rows.sort(key=lambda row: (0 if row.get("original") else 1, str(row.get("label") or "").lower()))
    return rows


def video_format_part(quality: dict) -> str:
    explicit = quality.get("video_format")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    spec = str(quality.get("ytdlp_format") or "bv*+ba/b")
    if "+ba" in spec:
        spec = spec.split("+ba", 1)[0]
    if spec.endswith("/b"):
        spec = spec[: -len("/b")]
    return spec.strip() or "bv*"


def build_ytdlp_audio_format(
    quality: dict,
    selected_tracks: list[dict] | None,
) -> tuple[str, bool, bool]:
    """Return (format_spec, audio_multistreams, use_fast_youtube_client)."""
    video = video_format_part(quality)
    default_fmt = quality.get("ytdlp_format") or YTDLP_YT_FORMAT_BEST
    tracks = [t for t in (selected_tracks or []) if isinstance(t, dict) and t.get("format_id")]
    if not tracks:
        return default_fmt, False, True
    # One language = default path. Explicit itag + web client often 403 on googlevideo.
    if len(tracks) == 1:
        return default_fmt, False, True
    ids = "+".join(f"{t['format_id']}" for t in tracks)
    return f"{video}+{ids}", True, False


def resolve_audio_tracks_by_langs(all_tracks: list[dict], langs: list[str] | None) -> list[dict]:
    """Map selected language codes back to probe track dicts (stable across UI)."""
    if not langs:
        return []
    by_lang = {str(t.get("language")): t for t in all_tracks if isinstance(t, dict)}
    resolved: list[dict] = []
    for lang in langs:
        track = by_lang.get(str(lang))
        if track and track.get("format_id"):
            resolved.append(track)
    return resolved


def hls_quality_options(playlist: Playlist) -> list[dict]:
    options = [{"label": "Максимальное (лучшее)", "ytdlp_format": None, "choice_url": None}]
    for variant in playlist.variants:
        options.append(
            {
                "label": variant.label(),
                "ytdlp_format": None,
                "choice_url": variant_key(variant),
            }
        )
    return options


def probe_media_info(
    page_url: str,
    headers: dict[str, str],
    insecure: bool,
    events: queue.Queue | None = None,
) -> dict:
    """Fetch title/thumbnail/qualities without downloading media (OVD-style configure step)."""
    if events is not None:
        events.put(("status", "Получение метаданных…"))

    # YouTube playlist / channel / watch+list → picker instead of single video.
    if prefers_ytdlp_download(page_url) and is_youtube_playlist_url(page_url):
        if events is not None:
            events.put(("status", "Читаю плейлист YouTube…"))
            events.put(("log", "Обнаружен плейлист / канал YouTube"))
        playlist = probe_ytdlp_playlist(page_url, events)
        if playlist and playlist.get("entries"):
            # Prefer selecting the current watch video when URL has v=.
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(page_url).query)
            current_id = (query.get("v") or [None])[0]
            playlist["current_id"] = current_id
            return playlist

    page_info = resolve_page_to_playlist(page_url, headers, insecure, events)
    result: dict = {
        "url": page_url,
        "title": page_info.title,
        "thumbnail": page_info.thumbnail,
        "referer": page_info.referer or page_url,
        "playlist_url": page_info.playlist_url,
        "browser": page_info.browser,
        "qualities": [{"label": "Максимальное (лучшее)", "ytdlp_format": "bv*+ba/b", "choice_url": None}],
        "mode": "ytdlp" if page_info.playlist_url.startswith("ytdlp:") else "hls",
    }
    if page_info.playlist_url.startswith("ytdlp:"):
        target = page_info.playlist_url[len("ytdlp:") :]
        ytdlp = ensure_yt_dlp(events)
        # Default client exposes multi-language audio tracks; android clients hide them.
        probe = subprocess.run(
            append_ytdlp_cookies(
                [
                    str(ytdlp),
                    "--no-playlist",
                    "--no-warnings",
                    "-J",
                    target,
                ]
            ),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
            timeout=120,
        )
        if probe.returncode == 0 and probe.stdout.strip():
            info = json.loads(probe.stdout)
            result["title"] = info.get("title") or result["title"]
            result["thumbnail"] = pick_ytdlp_thumbnail(info) or result["thumbnail"]
            result["qualities"] = ytdlp_quality_options(info)
            result["audio_tracks"] = ytdlp_audio_tracks(info)
            live_status = str(info.get("live_status") or "").lower()
            result["live"] = bool(info.get("is_live")) or live_status in {"is_live", "is_upcoming"}
            if result["live"] and events is not None:
                events.put(("log", "Это прямой эфир — запись до нажатия «Стоп»"))
        result["mode"] = "ytdlp"
        result["ytdlp_url"] = target
        return result

    if looks_like_direct_media_url(page_info.playlist_url):
        result["mode"] = "direct"
        result["browser"] = True
        result["qualities"] = [{"label": "Максимальное (лучшее)", "ytdlp_format": None, "choice_url": None}]
        return result

    # Do NOT fetch the signed CDN playlist during probe — many hosts invalidate
    # or rate-limit the token after the first hit. Qualities are selected at download.
    result["mode"] = "browser" if page_info.browser else "hls"
    result["qualities"] = [{"label": "Максимальное (лучшее)", "ytdlp_format": None, "choice_url": None}]
    result["browser"] = bool(page_info.browser)
    return result


def queue_download_job(params: dict, events: queue.Queue, cancel: threading.Event) -> None:
    sources = parse_url_list(params["source"])
    if not sources:
        events.put(("done", {"ok": False, "error": "Нет ссылок для скачивания"}))
        return

    total = len(sources)
    ok_count = 0
    fail_count = 0
    last_output = ""
    events.put(("queue_init", [{"url": url, "state": "wait", "detail": ""} for url in sources]))
    if total > 1:
        events.put(("log", f"В очереди {total} видео. Качаю по одному."))

    for index, url in enumerate(sources):
        if cancel.is_set():
            events.put(("queue_item", {"index": index, "state": "stop", "detail": "остановлено"}))
            for rest in range(index + 1, total):
                events.put(("queue_item", {"index": rest, "state": "stop", "detail": "пропущено"}))
            events.put(
                (
                    "done",
                    {
                        "cancelled": True,
                        "output": last_output,
                        "vod": True,
                        "queue_summary": f"Остановлено. Готово: {ok_count}, ошибок: {fail_count}",
                    },
                )
            )
            return

        events.put(("queue_item", {"index": index, "state": "run", "detail": "Получение метаданных…"}))
        events.put(("status", f"Очередь {index + 1}/{total}" if total > 1 else "Подготовка…"))
        if total > 1:
            events.put(("log", f"——— {index + 1}/{total}: {url}"))
        item = dict(params)
        item["source"] = url
        item["choice_url"] = None
        item["audio_choice"] = None
        if total > 1:
            item["name_edited"] = False
            item["filename"] = name_from_url(url)
            item["referer"] = ""
        proxy = ProxyEvents(events, suppress_done=True)
        download_job(item, proxy, cancel)  # type: ignore[arg-type]
        result = proxy.last_done or {"ok": False, "error": "Нет ответа от загрузчика"}
        if cancel.is_set() or result.get("cancelled"):
            events.put(("queue_item", {"index": index, "state": "stop", "detail": "остановлено", "output": result.get("output")}))
            for rest in range(index + 1, total):
                events.put(("queue_item", {"index": rest, "state": "stop", "detail": "пропущено"}))
            events.put(
                (
                    "done",
                    {
                        "cancelled": True,
                        "output": result.get("output") or last_output,
                        "vod": True,
                        "queue_summary": f"Остановлено. Готово: {ok_count}, ошибок: {fail_count}",
                    },
                )
            )
            return
        if result.get("ok"):
            ok_count += 1
            last_output = result.get("output") or last_output
            name = Path(last_output).name if last_output else "готово"
            events.put(
                (
                    "queue_item",
                    {
                        "index": index,
                        "state": "ok",
                        "detail": name,
                        "output": last_output,
                        "percent": 100,
                    },
                )
            )
            events.put(("log", f"Готово: {last_output}"))
        else:
            fail_count += 1
            error = (result.get("error") or "ошибка").splitlines()[0][:120]
            events.put(
                (
                    "queue_item",
                    {
                        "index": index,
                        "state": "err",
                        "detail": error,
                        "output": result.get("output"),
                    },
                )
            )
            events.put(("log", f"Ошибка: {result.get('error') or 'неизвестно'}"))

    summary = f"Очередь завершена: {ok_count} из {total}"
    if fail_count:
        summary += f", ошибок: {fail_count}"
    if total == 1 and ok_count == 1:
        summary = None
    events.put(
        (
            "done",
            {
                "ok": ok_count > 0 and fail_count == 0,
                "error": None if fail_count == 0 else summary,
                "output": last_output,
                "queue_summary": summary,
                "partial_ok": ok_count > 0 and fail_count > 0,
            },
        )
    )


def download_job(params: dict, events: queue.Queue, cancel: threading.Event) -> None:
    temp_playlist: Path | None = None
    temp_files: list[Path] = []
    try:
        cleanup_stale_temp()
        raw_proxy = str(params.get("proxy") or "")
        try:
            proxy = normalize_proxy_url(raw_proxy) if raw_proxy.strip() else ""
        except PlaylistError as exc:
            raise PlaylistError(f"Прокси: {exc}") from exc
        params["proxy"] = proxy
        cookies = str(params.get("cookies") or "").strip()
        with use_download_proxy(proxy, cookies):
            if proxy:
                events.put(("log", f"Прокси: {proxy_display(proxy)}"))
            if cookies:
                events.put(("log", f"Cookies: {Path(cookies).name}"))
            _download_job_inner(params, events, cancel, temp_playlist_holder := [])
            if temp_playlist_holder:
                temp_playlist = temp_playlist_holder[0]
    except PlaylistError as exc:
        events.put(("done", {"ok": False, "error": str(exc)}))
    except Exception as exc:
        events.put(("done", {"ok": False, "error": str(exc)}))
    finally:
        if temp_playlist is not None:
            temp_playlist.unlink(missing_ok=True)
        for path in temp_files:
            path.unlink(missing_ok=True)


def _download_job_inner(
    params: dict,
    events: queue.Queue,
    cancel: threading.Event,
    temp_playlist_holder: list,
) -> None:
    temp_playlist: Path | None = None
    temp_files: list[Path] = []
    try:
        cleanup_stale_temp()
        ffmpeg = find_ffmpeg()
        if ffmpeg is None:
            raise PlaylistError("Не найден ffmpeg.exe. Положите его в папку bin рядом с программой.")
        source_url, temp_playlist = materialize_source(params["source"])
        if temp_playlist is not None:
            temp_playlist_holder.append(temp_playlist)
        headers = build_headers(
            params["user_agent"],
            params["referer"],
            params["extra"],
            cookies_file=str(params.get("cookies") or ""),
        )
        page_info: PageResolve | None = None
        use_cached = (
            bool(params.get("probed"))
            and bool(params.get("playlist_url"))
            and not bool(params.get("refresh_stream"))
        )
        if use_cached:
            source_url = params["playlist_url"]
            page_info = PageResolve(
                playlist_url=params["playlist_url"],
                title=params.get("title"),
                referer=params.get("probe_referer") or params.get("referer") or "",
                browser=bool(params.get("browser")),
                thumbnail=params.get("thumbnail"),
            )
            if page_info.referer:
                headers["Referer"] = page_info.referer
                headers.setdefault("Origin", page_origin(page_info.referer).rstrip("/"))
            if page_info.title and not params["name_edited"]:
                safe = sanitize_filename(page_info.title) + ".mp4"
                events.put(("suggested_name", safe))
                params["filename"] = safe
            if page_info.thumbnail:
                events.put(("thumbnail", page_info.thumbnail))
        else:
            is_remote = source_url.startswith(("http://", "https://", "file:"))
            is_local_page = Path(source_url).exists() and not looks_like_playlist_url(source_url)
            if temp_playlist is None and (is_remote or is_local_page) and not looks_like_playlist_url(source_url):
                if source_url.startswith(("http://", "https://")) and not headers.get("Referer"):
                    headers["Referer"] = source_url
                events.put(("status", "Обновляю ссылку на видео…"))
                page_info = resolve_page_to_playlist(
                    source_url,
                    headers,
                    params["insecure"],
                    events,
                )
                source_url = page_info.playlist_url
                if page_info.referer:
                    headers["Referer"] = page_info.referer
                    headers.setdefault("Origin", page_origin(page_info.referer).rstrip("/"))
                # Prefer title/thumbnail collected during probe when refreshing.
                if params.get("title") and not page_info.title:
                    page_info.title = str(params["title"])
                if params.get("thumbnail") and not page_info.thumbnail:
                    page_info.thumbnail = str(params["thumbnail"])
                if page_info.title and not params["name_edited"]:
                    safe = sanitize_filename(page_info.title) + ".mp4"
                    events.put(("suggested_name", safe))
                    params["filename"] = safe
                if page_info.thumbnail:
                    events.put(("thumbnail", page_info.thumbnail))
            elif params.get("probed") and params.get("title"):
                # Local playlist / already-resolved URL with probe metadata only.
                if params.get("title") and not params["name_edited"]:
                    safe = sanitize_filename(str(params["title"])) + ".mp4"
                    events.put(("suggested_name", safe))
                    params["filename"] = safe
                if params.get("thumbnail"):
                    events.put(("thumbnail", params["thumbnail"]))
                if params.get("probe_referer"):
                    headers["Referer"] = str(params["probe_referer"])
                page_info = PageResolve(
                    playlist_url=source_url,
                    title=params.get("title"),
                    referer=params.get("probe_referer") or "",
                    browser=bool(params.get("browser")),
                    thumbnail=params.get("thumbnail"),
                )
        if source_url.startswith("ytdlp:"):
            page_url = source_url[len("ytdlp:") :]
            output_dir = Path(params["output_dir"])
            output_dir.mkdir(parents=True, exist_ok=True)
            filename = sanitize_filename(params["filename"])
            if not filename.lower().endswith(".mp4"):
                filename += ".mp4"
            if params.get("live"):
                stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                stem = Path(filename).stem
                filename = f"{stem}_{stamp}.mp4"
            output = unique_path(output_dir / filename)
            events.put(("output", str(output)))
            events.put(("log", f"Файл: {output}"))
            if params.get("live"):
                download_live_stream(
                    page_url,
                    output,
                    headers,
                    params["proxy"],
                    params["insecure"],
                    events,
                    cancel,
                    format_spec=params.get("ytdlp_format") or "bv*+ba/b",
                    limit_seconds=params.get("limit_seconds"),
                )
                return
            download_with_ytdlp(
                page_url,
                output,
                headers,
                params["proxy"],
                params["limit_seconds"],
                events,
                cancel,
                format_spec=params.get("ytdlp_format") or "bv*+ba/b",
                threads=params.get("threads", DEFAULT_THREADS),
                audio_multistreams=bool(params.get("audio_multistreams")),
                use_fast_youtube_client=bool(params.get("use_fast_youtube_client", True)),
            )
            return

        output_dir = Path(params["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)
        filename = sanitize_filename(params["filename"])
        if not filename.lower().endswith(".mp4"):
            filename += ".mp4"
        output = unique_path(output_dir / filename)

        if page_info is not None and page_info.browser:
            events.put(("output", str(output)))
            events.put(("log", f"Файл: {output}"))
            if looks_like_direct_media_url(source_url):
                download_direct_with_browser(
                    source_url,
                    output,
                    page_info.referer or headers.get("Referer", ""),
                    headers,
                    params["limit_seconds"],
                    events,
                    cancel,
                    threads=params.get("direct_threads", DEFAULT_DIRECT_THREADS),
                )
            else:
                download_hls_with_browser(
                    source_url,
                    output,
                    page_info.referer or headers.get("Referer", ""),
                    headers,
                    params["choice_url"],
                    params["limit_seconds"],
                    events,
                    cancel,
                    threads=params.get("threads", DEFAULT_THREADS),
                )
            return

        events.put(("status", "Читаю плейлист…"))
        events.put(("log", "Открываю плейлист"))
        resolved = resolve_source(
            source_url,
            headers,
            params["insecure"],
            params["choice_url"],
            params["audio_choice"],
        )
        temp_files.extend(resolved.temp_files)
        headers = resolved.headers or headers
        if cancel.is_set():
            events.put(("done", {"cancelled": True}))
            return
        kind = "запись" if resolved.is_vod else "эфир, пишу до нажатия «Стоп»"
        length = fmt_duration(resolved.duration)
        detail = resolved.label
        if length:
            detail = f"{detail} · {length}"
        events.put(("status", f"{detail} · {kind}"))
        events.put(("log", f"Качество: {resolved.label}"))

        if not resolved.is_vod and not params["name_edited"]:
            stamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
            output = output.with_name(f"{output.stem}_{stamp}{output.suffix}")
            output = unique_path(output)
        events.put(("output", str(output)))
        events.put(("log", f"Файл: {output}"))

        if resolved.segments:
            encode_slideshow(
                ffmpeg,
                resolved,
                output,
                headers,
                params["insecure"],
                params["limit_seconds"],
                events,
                cancel,
                threads=params.get("threads", DEFAULT_THREADS),
            )
            return

        limit = params["limit_seconds"]
        codecs = resolved.label.lower()
        attempts = (False,) if any(token in codecs for token in ("ac-3", "ec-3", "opus")) else (True, False)
        for use_bsf in attempts:
            if cancel.is_set():
                events.put(("done", {"cancelled": True, "output": str(output)}))
                return
            command = build_ffmpeg_command(
                ffmpeg,
                resolved,
                output,
                headers,
                params["proxy"],
                params["insecure"],
                limit,
                use_bsf,
            )
            events.put(("status", "Скачиваю…"))
            code, err, seen = run_ffmpeg(command, resolved.duration if not limit else float(limit), cancel, events)
            if code == 0:
                events.put(("progress", {"time": resolved.duration or seen, "percent": 100.0}))
                events.put(("done", {"ok": True, "output": str(output)}))
                return
            if code == -2:
                if output.exists() and output.stat().st_size > 8 * 1024:
                    final = remux_recording_to_mp4(output, output, events)
                    events.put(("progress", {"time": seen, "percent": 100.0}))
                    events.put(("done", {"ok": True, "output": str(final), "live": True, "stopped": True}))
                else:
                    events.put(("done", {"cancelled": True, "output": str(output), "vod": resolved.is_vod}))
                return
            # Cloudflare / bot wall: retry through browser downloader
            if "403" in err or "Forbidden" in err:
                events.put(("log", "Обычный способ заблокирован. Переключаюсь на браузерный режим."))
                download_hls_with_browser(
                    source_url,
                    output,
                    headers.get("Referer", page_origin(source_url)),
                    headers,
                    params["choice_url"],
                    params["limit_seconds"],
                    events,
                    cancel,
                    threads=params.get("threads", DEFAULT_THREADS),
                )
                return
            aac_problem = "aac_adtstoasc" in err.lower() or "adts" in err.lower()
            if use_bsf and aac_problem and seen < 15:
                events.put(("log", "Повторяю без фильтра AAC"))
                if output.exists():
                    output.unlink()
                continue
            message = err.strip() or f"ffmpeg завершился с кодом {code}"
            if output.exists() and output.stat().st_size > 0:
                message += f"\nЧастичный файл: {output}"
            raise PlaylistError(message)
        return
    except PlaylistError as exc:
        events.put(("done", {"ok": False, "error": str(exc)}))
    except Exception as exc:
        events.put(("done", {"ok": False, "error": str(exc)}))
    finally:
        if temp_playlist is not None:
            temp_playlist.unlink(missing_ok=True)
        for path in temp_files:
            path.unlink(missing_ok=True)


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_settings(data: dict) -> None:
    try:
        SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass




OG_IMAGE_RE = re.compile(
    r"""<meta[^>]+(?:property|name)\s*=\s*["'](?:og:image|twitter:image|thumbnail)["'][^>]+content\s*=\s*["']([^"']+)["']""",
    re.IGNORECASE,
)
OG_IMAGE_RE_ALT = re.compile(
    r"""<meta[^>]+content\s*=\s*["']([^"']+)["'][^>]+(?:property|name)\s*=\s*["'](?:og:image|twitter:image|thumbnail)["']""",
    re.IGNORECASE,
)
LINK_IMAGE_RE = re.compile(
    r"""<link[^>]+rel\s*=\s*["'](?:image_src|thumbnail)["'][^>]+href\s*=\s*["']([^"']+)["']""",
    re.IGNORECASE,
)


def extract_page_thumbnail(text: str, base_url: str = "") -> str | None:
    for pattern in (OG_IMAGE_RE, OG_IMAGE_RE_ALT, LINK_IMAGE_RE):
        match = pattern.search(text)
        if not match:
            continue
        candidate = unescape_candidate(match.group(1)).strip()
        if candidate.startswith("//"):
            candidate = "https:" + candidate
        elif base_url and not candidate.startswith(("http://", "https://")):
            candidate = urllib.parse.urljoin(base_url, candidate)
        if candidate.startswith(("http://", "https://")):
            return candidate
    return None


def pick_ytdlp_thumbnail(info: dict) -> str | None:
    """Same idea as Open Video Downloader: largest entry from thumbnails[]."""
    thumbs = info.get("thumbnails") or []
    best_url = None
    best_area = -1
    if isinstance(thumbs, list):
        for thumb in thumbs:
            if not isinstance(thumb, dict):
                continue
            url = thumb.get("url")
            if not isinstance(url, str) or not url.startswith("http"):
                continue
            area = int(thumb.get("width") or 0) * int(thumb.get("height") or 0)
            if area >= best_area:
                best_area = area
                best_url = url
    if best_url:
        return best_url
    direct = info.get("thumbnail")
    if isinstance(direct, str) and direct.startswith("http"):
        return direct
    return None


def extract_json_thumbnail(data: dict) -> str | None:
    for key in ("thumbnail", "thumb", "poster", "image", "preview"):
        value = data.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value
    return None


def thumbs_dir() -> Path:
    path = APP_DIR / "temp" / "thumbs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_thumbnail_image(url: str, headers: dict[str, str] | None = None) -> Path | None:
    """Download remote thumb and convert to PNG for tkinter PhotoImage."""
    if not url:
        return None
    digest = hashlib.sha1(url.encode("utf-8", errors="ignore")).hexdigest()[:20]
    out = thumbs_dir() / f"{digest}.png"
    if out.exists() and out.stat().st_size > 0:
        return out
    try:
        ensure_curl_cffi()
        from curl_cffi import requests as crequests

        session = crequests.Session(impersonate="chrome131")
        req_headers = {"User-Agent": (headers or {}).get("User-Agent") or DEFAULT_UA, "Accept": "image/*,*/*"}
        if headers and headers.get("Referer"):
            req_headers["Referer"] = headers["Referer"]
        response = session.get(url, headers=req_headers, timeout=45, allow_redirects=True)
        if response.status_code >= 400 or not response.content:
            return None
        from PIL import Image
        import io

        image = Image.open(io.BytesIO(response.content))
        image = image.convert("RGB")
        image.thumbnail((320, 180), Image.Resampling.LANCZOS)
        # Pad to 16:9 canvas for consistent card layout
        canvas = Image.new("RGB", (320, 180), (20, 24, 36))
        x = (320 - image.width) // 2
        y = (180 - image.height) // 2
        canvas.paste(image, (x, y))
        canvas.save(out, format="PNG")
        return out
    except Exception:
        return None


# Soft modern palette — charcoal / slate + cyan accent (no purple)
THEMES = {
    "dark": {
        "bg": "#0b0d10",
        "header": "#0f1217",
        "card": "#14181f",
        "input": "#1a1f28",
        "text": "#f2f4f7",
        "muted": "#8b93a1",
        "accent": "#3dd6c6",
        "accent_hover": "#2bbfb0",
        "accent_text": "#041412",
        "accent_dim": "#163833",
        "border": "#262c36",
        "danger": "#ff5c6c",
        "warning": "#f0b429",
        "icon": "#c8ced8",
        "icon_disabled": "#555e6c",
        "thumb": "#0e1116",
        "separator": "#1e2430",
        "entry_insert": "#f2f4f7",
        "shadow": "#000000",
        "chip": "#1e2430",
        "chip_hover": "#2a3140",
        "success": "#3ecf8e",
        "surface": "#11151b",
        "rail": "#3dd6c6",
        "focus": "#3dd6c6",
        "overlay": "#0b0d10",
    },
    "light": {
        "bg": "#f3f5f8",
        "header": "#ffffff",
        "card": "#ffffff",
        "input": "#eef1f5",
        "text": "#12161c",
        "muted": "#667180",
        "accent": "#0d9f90",
        "accent_hover": "#0b877a",
        "accent_text": "#ffffff",
        "accent_dim": "#d7f3ef",
        "border": "#dde3ea",
        "danger": "#d64550",
        "warning": "#b8860b",
        "icon": "#3a4554",
        "icon_disabled": "#9aa5b3",
        "thumb": "#e8ecf1",
        "separator": "#e6ebf0",
        "entry_insert": "#12161c",
        "shadow": "#c5ced8",
        "chip": "#e8ecf1",
        "chip_hover": "#dce2e9",
        "success": "#0f8a4b",
        "surface": "#f8fafc",
        "rail": "#0d9f90",
        "focus": "#0d9f90",
        "overlay": "#ffffff",
    },
}


DEFAULT_GEOMETRY = "1360x900"
MIN_WINDOW = (1000, 680)
UI_FONT = "Segoe UI"
UI_FONT_SEMI = "Segoe UI Semibold"
UI_ICON_FONT = "Segoe MDL2 Assets"


def configure_ttk_style(theme: dict) -> None:
    style = ttk.Style()
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    style.configure(
        ".",
        background=theme["bg"],
        foreground=theme["text"],
        fieldbackground=theme["input"],
        troughcolor=theme["chip"],
        bordercolor=theme["border"],
        lightcolor=theme["border"],
        darkcolor=theme["border"],
        focuscolor=theme["accent"],
        font=(UI_FONT, 10),
    )
    style.configure(
        "TLabel",
        background=theme["card"],
        foreground=theme["text"],
        font=(UI_FONT, 10),
    )
    style.configure(
        "TFrame",
        background=theme["card"],
    )
    style.configure(
        "TCheckbutton",
        background=theme["card"],
        foreground=theme["text"],
        focuscolor=theme["card"],
        font=(UI_FONT, 10),
    )
    style.map(
        "TCheckbutton",
        background=[("active", theme["card"])],
        foreground=[("active", theme["text"])],
        indicatorcolor=[
            ("selected", theme["accent"]),
            ("!selected", theme["input"]),
        ],
    )
    style.configure(
        "TRadiobutton",
        background=theme["card"],
        foreground=theme["text"],
        focuscolor=theme["card"],
        font=(UI_FONT, 10),
    )
    style.map(
        "TRadiobutton",
        background=[("active", theme["card"])],
        foreground=[("active", theme["text"])],
        indicatorcolor=[
            ("selected", theme["accent"]),
            ("!selected", theme["input"]),
        ],
    )
    style.configure(
        "TEntry",
        fieldbackground=theme["input"],
        foreground=theme["text"],
        insertcolor=theme["entry_insert"],
        bordercolor=theme["border"],
        lightcolor=theme["border"],
        darkcolor=theme["border"],
        padding=8,
    )
    style.map(
        "TEntry",
        fieldbackground=[("focus", theme["input"])],
        bordercolor=[("focus", theme["accent"])],
        lightcolor=[("focus", theme["accent"])],
        darkcolor=[("focus", theme["accent"])],
    )
    style.configure(
        "TSpinbox",
        fieldbackground=theme["input"],
        foreground=theme["text"],
        background=theme["input"],
        arrowcolor=theme["muted"],
        bordercolor=theme["border"],
        lightcolor=theme["border"],
        darkcolor=theme["border"],
        insertcolor=theme["entry_insert"],
        padding=6,
    )
    style.map(
        "TSpinbox",
        fieldbackground=[("focus", theme["input"])],
        bordercolor=[("focus", theme["accent"])],
        arrowcolor=[("active", theme["text"])],
    )
    style.configure(
        "TButton",
        background=theme["chip"],
        foreground=theme["text"],
        bordercolor=theme["border"],
        lightcolor=theme["chip"],
        darkcolor=theme["chip"],
        focuscolor=theme["chip"],
        padding=(12, 7),
        font=(UI_FONT, 9),
    )
    style.map(
        "TButton",
        background=[("active", theme["chip_hover"]), ("pressed", theme["border"])],
        foreground=[("disabled", theme["icon_disabled"])],
    )
    style.configure(
        "Accent.TButton",
        background=theme["accent"],
        foreground=theme["accent_text"],
        bordercolor=theme["accent"],
        lightcolor=theme["accent"],
        darkcolor=theme["accent"],
        focuscolor=theme["accent"],
        padding=(14, 8),
        font=(UI_FONT_SEMI, 10),
    )
    style.map(
        "Accent.TButton",
        background=[("active", theme["accent_hover"]), ("pressed", theme["accent_hover"])],
        foreground=[("disabled", theme["icon_disabled"])],
    )
    style.configure(
        "Metro.TCombobox",
        fieldbackground=theme["input"],
        background=theme["input"],
        foreground=theme["text"],
        arrowcolor=theme["muted"],
        bordercolor=theme["border"],
        lightcolor=theme["border"],
        darkcolor=theme["border"],
        padding=8,
        font=(UI_FONT, 10),
    )
    style.map(
        "Metro.TCombobox",
        fieldbackground=[("readonly", theme["input"]), ("focus", theme["input"])],
        foreground=[("readonly", theme["text"])],
        bordercolor=[("focus", theme["accent"]), ("readonly", theme["border"])],
        arrowcolor=[("active", theme["text"])],
        selectbackground=[("readonly", theme["accent"])],
        selectforeground=[("readonly", theme["accent_text"])],
    )
    style.configure(
        "Metro.Vertical.TScrollbar",
        background=theme["chip"],
        troughcolor=theme["bg"],
        bordercolor=theme["bg"],
        arrowcolor=theme["muted"],
        relief="flat",
        width=10,
    )
    style.map("Metro.Vertical.TScrollbar", background=[("active", theme["border"])])


def ui_icon(char: str, size: int = 12) -> tuple:
    return (UI_ICON_FONT, size)


def make_nav_chip(parent: tk.Misc, text: str, theme: dict, command=None) -> tk.Label:
    label = tk.Label(
        parent,
        text=text,
        bg=theme["header"],
        fg=theme["muted"],
        font=(UI_FONT, 9),
        cursor="hand2",
        padx=12,
        pady=7,
    )
    if command:
        label.bind("<Button-1>", lambda _e: command())

    def on_enter(_e, w=label, chip=theme["chip"], fg=theme["text"]) -> None:
        w.configure(bg=chip, fg=fg)

    def on_leave(_e, w=label, bg=theme["header"], fg=theme["muted"]) -> None:
        # theme may change; callers refresh colors via apply_theme
        app = getattr(parent, "app_theme_owner", None)
        if app is not None:
            w.configure(bg=app.theme["header"], fg=app.theme["muted"])
        else:
            w.configure(bg=bg, fg=fg)

    label.bind("<Enter>", on_enter)
    label.bind("<Leave>", on_leave)
    return label


def center_window(win: tk.Misc, width: int, height: int) -> None:
    win.update_idletasks()
    sw = win.winfo_screenwidth()
    sh = win.winfo_screenheight()
    x = max(0, (sw - width) // 2)
    y = max(0, (sh - height) // 2)
    win.geometry(f"{width}x{height}+{x}+{y}")


class AudioTrackPickerDialog(tk.Toplevel):
    """Select which YouTube audio languages to embed into the MP4."""

    def __init__(self, master: "App", tracks: list[dict], selected_langs: list[str] | None = None) -> None:
        super().__init__(master)
        self.app = master
        self.tracks = list(tracks)
        self.result: list[dict] | None = None
        theme = master.theme
        self.title("Звуковые дорожки")
        self.configure(bg=theme["bg"])
        self.transient(master)
        self.grab_set()
        self.geometry("560x520")
        self.minsize(440, 400)

        # Footer first (side=bottom) so «Готово» never gets clipped by the list.
        footer = tk.Frame(self, bg=theme["bg"])
        footer.pack(side="bottom", fill="x", padx=16, pady=(8, 14))
        tk.Button(
            footer,
            text="Готово",
            command=self._confirm,
            bg=theme["accent"],
            fg=theme["accent_text"],
            activebackground=theme["accent_hover"],
            activeforeground=theme["accent_text"],
            relief="flat",
            font=("Segoe UI", 10, "bold"),
            padx=18,
            pady=6,
            cursor="hand2",
            bd=0,
        ).pack(side="right")
        ttk.Button(footer, text="Отмена", command=self._cancel).pack(side="right", padx=(0, 8))

        tk.Label(
            self,
            text="Отметьте дорожки — затем нажмите «Готово». Они попадут в один MP4.",
            bg=theme["bg"],
            fg=theme["muted"],
            font=("Segoe UI", 9),
            anchor="w",
        ).pack(fill="x", padx=16, pady=(14, 6))

        toolbar = tk.Frame(self, bg=theme["bg"])
        toolbar.pack(fill="x", padx=16, pady=(0, 8))
        ttk.Button(toolbar, text="Все", command=self._select_all).pack(side="left")
        ttk.Button(toolbar, text="Снять", command=self._clear_all).pack(side="left", padx=(6, 0))
        ttk.Button(toolbar, text="Только оригинал", command=self._select_original).pack(side="left", padx=(6, 0))
        self.count_var = tk.StringVar()
        tk.Label(toolbar, textvariable=self.count_var, bg=theme["bg"], fg=theme["muted"], font=("Segoe UI", 9)).pack(
            side="right"
        )

        list_wrap = tk.Frame(self, bg=theme["bg"])
        list_wrap.pack(fill="both", expand=True, padx=16, pady=(0, 4))
        canvas = tk.Canvas(list_wrap, bg=theme["input"], highlightthickness=1, highlightbackground=theme["border"], bd=0)
        scroll = ttk.Scrollbar(list_wrap, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        self.rows_frame = tk.Frame(canvas, bg=theme["input"])
        window_id = canvas.create_window((0, 0), window=self.rows_frame, anchor="nw")
        self.rows_frame.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window_id, width=e.width))

        selected = set(selected_langs or [])
        if not selected:
            for track in self.tracks:
                if track.get("original"):
                    selected.add(str(track.get("language")))
                    break
            if not selected and self.tracks:
                selected.add(str(self.tracks[0].get("language")))

        self.vars: list[tk.BooleanVar] = []
        for track in self.tracks:
            lang = str(track.get("language") or "")
            var = tk.BooleanVar(master=self, value=lang in selected)
            self.vars.append(var)
            mark = " ★" if track.get("original") else ""
            text = f"{track.get('label') or lang}{mark}   [{lang}]"
            chk = tk.Checkbutton(
                self.rows_frame,
                text=text,
                variable=var,
                command=self._update_count,
                bg=theme["input"],
                fg=theme["text"],
                activebackground=theme["input"],
                activeforeground=theme["text"],
                selectcolor=theme["card"],
                anchor="w",
                justify="left",
                font=("Segoe UI", 10),
                padx=10,
                pady=4,
                cursor="hand2",
            )
            chk.pack(fill="x", anchor="w")

        self._update_count()
        # Closing via ✕ also applies current ticks (users expect that).
        self.protocol("WM_DELETE_WINDOW", self._confirm)
        self.bind("<Escape>", lambda _e: self._cancel())
        self.bind("<Return>", lambda _e: self._confirm())
        self.focus_set()

    def _selected_tracks(self) -> list[dict]:
        return [track for track, var in zip(self.tracks, self.vars) if bool(var.get())]

    def _update_count(self) -> None:
        selected = len(self._selected_tracks())
        self.count_var.set(f"выбрано {selected} / {len(self.vars)}")

    def _select_all(self) -> None:
        for var in self.vars:
            var.set(True)
        self._update_count()

    def _clear_all(self) -> None:
        for var in self.vars:
            var.set(False)
        self._update_count()

    def _select_original(self) -> None:
        for track, var in zip(self.tracks, self.vars):
            var.set(bool(track.get("original")))
        if not any(bool(var.get()) for var in self.vars) and self.vars:
            self.vars[0].set(True)
        self._update_count()

    def _confirm(self) -> None:
        selected = self._selected_tracks()
        if not selected:
            messagebox.showinfo("Аудио", "Выберите хотя бы одну дорожку.", parent=self)
            return
        self.result = selected
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.destroy()


class PlaylistPickerDialog(tk.Toplevel):
    """Choose which YouTube playlist videos to add for download."""

    def __init__(self, master: "App", playlist: dict) -> None:
        super().__init__(master)
        self.app = master
        self.playlist = playlist
        self.result: list[dict] | None = None
        theme = master.theme
        title = str(playlist.get("title") or "Плейлист YouTube")
        self.title(f"Плейлист · {title[:80]}")
        self.configure(bg=theme["bg"])
        self.transient(master)
        self.grab_set()
        self.geometry("720x520")
        self.minsize(560, 400)

        header = tk.Frame(self, bg=theme["bg"])
        header.pack(fill="x", padx=16, pady=(14, 6))
        tk.Label(
            header,
            text=title,
            bg=theme["bg"],
            fg=theme["text"],
            font=("Segoe UI", 12, "bold"),
            anchor="w",
            wraplength=560,
            justify="left",
        ).pack(side="left", fill="x", expand=True)
        self.count_var = tk.StringVar()
        tk.Label(header, textvariable=self.count_var, bg=theme["bg"], fg=theme["muted"], font=("Segoe UI", 9)).pack(
            side="right"
        )

        toolbar = tk.Frame(self, bg=theme["bg"])
        toolbar.pack(fill="x", padx=16, pady=(0, 8))
        ttk.Button(toolbar, text="Выбрать все", command=self._select_all).pack(side="left")
        ttk.Button(toolbar, text="Снять все", command=self._clear_all).pack(side="left", padx=(6, 0))
        ttk.Button(toolbar, text="Только текущее", command=self._select_current).pack(side="left", padx=(6, 0))

        list_wrap = tk.Frame(self, bg=theme["bg"])
        list_wrap.pack(fill="both", expand=True, padx=16, pady=(0, 8))
        canvas = tk.Canvas(list_wrap, bg=theme["input"], highlightthickness=1, highlightbackground=theme["border"], bd=0)
        scroll = ttk.Scrollbar(list_wrap, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        self.rows_frame = tk.Frame(canvas, bg=theme["input"])
        window_id = canvas.create_window((0, 0), window=self.rows_frame, anchor="nw")
        self.rows_frame.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window_id, width=e.width))

        self.vars: list[tk.BooleanVar] = []
        self.entries: list[dict] = list(playlist.get("entries") or [])
        current_id = playlist.get("current_id")
        for entry in self.entries:
            var = tk.BooleanVar(value=True if not current_id else entry.get("id") == current_id)
            self.vars.append(var)
            row = tk.Frame(self.rows_frame, bg=theme["input"])
            row.pack(fill="x", padx=8, pady=3)
            ttk.Checkbutton(row, variable=var, command=self._update_count).pack(side="left")
            dur = entry.get("duration")
            dur_txt = f"  ·  {fmt_duration(float(dur))}" if isinstance(dur, (int, float)) and dur else ""
            label = f"{entry.get('title') or entry.get('url')}{dur_txt}"
            tk.Label(
                row,
                text=label,
                bg=theme["input"],
                fg=theme["text"],
                font=("Segoe UI", 9),
                anchor="w",
                justify="left",
                wraplength=580,
            ).pack(side="left", fill="x", expand=True, padx=(4, 0))

        footer = tk.Frame(self, bg=theme["bg"])
        footer.pack(fill="x", padx=16, pady=(4, 14))
        ttk.Button(footer, text="Отмена", command=self._cancel).pack(side="right")
        ttk.Button(footer, text="Добавить выбранные", command=self._confirm).pack(side="right", padx=(0, 8))
        self._update_count()
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind("<Escape>", lambda _e: self._cancel())
        self.focus_set()

    def _update_count(self) -> None:
        selected = sum(1 for var in self.vars if var.get())
        self.count_var.set(f"{selected} / {len(self.vars)}")

    def _select_all(self) -> None:
        for var in self.vars:
            var.set(True)
        self._update_count()

    def _clear_all(self) -> None:
        for var in self.vars:
            var.set(False)
        self._update_count()

    def _select_current(self) -> None:
        current_id = self.playlist.get("current_id")
        if not current_id:
            return
        for entry, var in zip(self.entries, self.vars):
            var.set(entry.get("id") == current_id)
        self._update_count()

    def _confirm(self) -> None:
        selected = [entry for entry, var in zip(self.entries, self.vars) if var.get()]
        if not selected:
            messagebox.showinfo("Плейлист", "Выберите хотя бы одно видео.", parent=self)
            return
        self.result = selected
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.destroy()


class DownloadCard(tk.Frame):
    """Media card — modern shell layout."""

    def __init__(self, master: tk.Misc, app: "App", index: int, url: str) -> None:
        theme = app.theme
        super().__init__(
            master,
            bg=theme["card"],
            highlightthickness=1,
            highlightbackground=theme["border"],
            highlightcolor=theme["border"],
        )
        self.app = app
        self.index = index
        self.url = url
        self.state = "fetch"
        self.detail = "Получение метаданных…"
        self.output = ""
        self.percent: float | None = None
        self.thumbnail_url = ""
        self.qualities: list[dict] = []
        self.audio_tracks: list[dict] = []
        self.selected_audio: list[dict] = []
        self.selected_audio_langs: list[str] = []
        self.probe: dict = {}
        self._photo: tk.PhotoImage | None = None
        self._bar_width = 1
        self._speed = ""
        self._eta = ""
        self.cancel = threading.Event()
        self.worker: threading.Thread | None = None

        self.rail = tk.Frame(self, bg=theme["rail"], width=3)
        self.rail.pack(side="left", fill="y")

        body = tk.Frame(self, bg=theme["card"])
        body.pack(fill="both", expand=True, padx=18, pady=16)

        self.thumb_frame = tk.Frame(body, bg=theme["thumb"], width=220, height=124)
        self.thumb_frame.pack(side="left", padx=(0, 16))
        self.thumb_frame.pack_propagate(False)
        self.thumb_label = tk.Label(
            self.thumb_frame,
            text="▶",
            bg=theme["thumb"],
            fg=theme["muted"],
            font=(UI_FONT, 22),
        )
        self.thumb_label.place(relx=0.5, rely=0.5, anchor="center")

        mid = tk.Frame(body, bg=theme["card"])
        mid.pack(side="left", fill="both", expand=True)

        self.title_var = tk.StringVar(value=url)
        self.title_label = tk.Label(
            mid,
            textvariable=self.title_var,
            bg=theme["card"],
            fg=theme["text"],
            font=(UI_FONT_SEMI, 13),
            anchor="w",
            justify="left",
            wraplength=540,
        )
        self.title_label.pack(fill="x", pady=(0, 8))

        self.configure_frame = tk.Frame(mid, bg=theme["card"])
        self.configure_frame.pack(fill="x")
        tk.Label(
            self.configure_frame,
            text="Качество",
            bg=theme["card"],
            fg=theme["muted"],
            font=(UI_FONT, 9),
        ).pack(side="left")
        self.quality_var = tk.StringVar(value="Максимальное (лучшее)")
        self.quality_box = ttk.Combobox(
            self.configure_frame,
            textvariable=self.quality_var,
            state="readonly",
            values=["Максимальное (лучшее)"],
            width=26,
            style="Metro.TCombobox",
        )
        self.quality_box.pack(side="left", padx=(8, 10))
        self.quality_box.current(0)
        self.audio_btn = tk.Button(
            self.configure_frame,
            text="Аудио",
            command=self._pick_audio,
            bg=theme["chip"],
            fg=theme["text"],
            activebackground=theme["chip_hover"],
            activeforeground=theme["text"],
            relief="flat",
            font=(UI_FONT, 9),
            padx=12,
            pady=6,
            cursor="hand2",
            bd=0,
        )
        self.download_btn = tk.Button(
            self.configure_frame,
            text="Скачать",
            command=self._start_download,
            bg=theme["accent"],
            fg=theme["accent_text"],
            activebackground=theme["accent_hover"],
            activeforeground=theme["accent_text"],
            relief="flat",
            font=(UI_FONT_SEMI, 10),
            padx=18,
            pady=6,
            cursor="hand2",
            bd=0,
            state="disabled",
        )
        self.download_btn.pack(side="left")

        self.progress_frame = tk.Frame(mid, bg=theme["card"])
        self.bar_wrap = tk.Frame(self.progress_frame, bg=theme["accent_dim"], height=8)
        self.bar_wrap.pack(fill="x", pady=(2, 8))
        self.bar_wrap.pack_propagate(False)
        self.bar_fill = tk.Frame(self.bar_wrap, bg=theme["accent"], width=0, height=8)
        self.bar_fill.place(x=0, y=0, relheight=1)
        self.status_overlay = tk.Label(
            self.progress_frame,
            text=self.detail,
            bg=theme["card"],
            fg=theme["muted"],
            font=(UI_FONT, 9),
            anchor="w",
        )
        self.status_overlay.pack(fill="x")
        self.bar_wrap.bind("<Configure>", self._on_bar_configure)

        self.meta_var = tk.StringVar(value="Получение метаданных…")
        self.meta_label = tk.Label(
            mid,
            textvariable=self.meta_var,
            bg=theme["card"],
            fg=theme["muted"],
            font=(UI_FONT, 9),
            anchor="w",
        )
        self.meta_label.pack(fill="x", pady=(10, 0))

        actions = tk.Frame(body, bg=theme["card"])
        actions.pack(side="right", fill="y", padx=(12, 0))

        self.btn_close = self._icon_btn(actions, "×", self._remove, theme["danger"])
        self.btn_open_url = self._icon_btn(actions, "↗", self._open_url)
        self.btn_download = self._icon_btn(actions, "↓", self._start_download, enabled=False)
        self.btn_folder = self._icon_btn(actions, "▣", self._open_folder)
        self.btn_info = self._icon_btn(actions, "i", self._show_info)
        for btn in (self.btn_close, self.btn_open_url, self.btn_download, self.btn_folder, self.btn_info):
            btn.pack(pady=3, anchor="center")

        self._show_fetching()

    def _icon_btn(self, parent: tk.Misc, text: str, command, color: str | None = None, enabled: bool = True) -> tk.Label:
        theme = self.app.theme
        fg = color or (theme["icon"] if enabled else theme["icon_disabled"])
        label = tk.Label(
            parent,
            text=text,
            bg=theme["chip"],
            fg=fg,
            font=(UI_FONT, 11),
            width=3,
            cursor="hand2" if enabled else "",
            padx=2,
            pady=5,
        )
        if enabled:
            label.bind("<Button-1>", lambda _e: command())
            label.bind("<Enter>", lambda _e, w=label: w.configure(bg=self.app.theme["chip_hover"]))
            label.bind("<Leave>", lambda _e, w=label: w.configure(bg=self.app.theme["chip"]))
        label._enabled = enabled  # type: ignore[attr-defined]
        label._command = command  # type: ignore[attr-defined]
        label._base_color = color  # type: ignore[attr-defined]
        return label

    def _set_btn_enabled(self, label: tk.Label, enabled: bool) -> None:
        theme = self.app.theme
        label._enabled = enabled  # type: ignore[attr-defined]
        if enabled:
            label.configure(fg=label._base_color or theme["icon"], cursor="hand2", bg=theme["chip"])  # type: ignore[attr-defined]
            label.bind("<Button-1>", lambda _e: label._command())  # type: ignore[attr-defined]
        else:
            label.configure(fg=theme["icon_disabled"], cursor="", bg=theme["chip"])
            label.unbind("<Button-1>")

    def _show_fetching(self) -> None:
        self.configure_frame.pack_forget()
        self.progress_frame.pack_forget()
        self.meta_var.set("Получение метаданных…")
        self.download_btn.configure(state="disabled")
        self._set_btn_enabled(self.btn_download, False)

    def _show_ready(self) -> None:
        self.progress_frame.pack_forget()
        self.configure_frame.pack(fill="x")
        self._refresh_audio_button()
        self._update_audio_meta()
        self.download_btn.configure(state="normal")
        self._set_btn_enabled(self.btn_download, True)

    def _refresh_audio_button(self) -> None:
        self.audio_btn.pack_forget()
        if len(self.audio_tracks) > 1:
            self.audio_btn.pack(side="left", padx=(0, 10), before=self.download_btn)

    def _default_audio_selection(self) -> list[dict]:
        for track in self.audio_tracks:
            if track.get("original"):
                return [track]
        return [self.audio_tracks[0]] if self.audio_tracks else []

    def _set_selected_audio(self, tracks: list[dict]) -> None:
        self.selected_audio = list(tracks)
        self.selected_audio_langs = [str(t.get("language")) for t in tracks if t.get("language") is not None]
        self._update_audio_meta()

    def _update_audio_meta(self) -> None:
        if len(self.audio_tracks) <= 1:
            self.meta_var.set(
                "Готово к скачиванию · выбрано максимальное качество"
                if self.quality_box.current() == 0
                else "Готово к скачиванию"
            )
            return
        selected = self.selected_audio_tracks()
        if len(selected) == len(self.audio_tracks):
            audio_txt = f"Аудио: все ({len(selected)})"
        elif len(selected) == 1:
            audio_txt = f"Аудио: {selected[0].get('label') or selected[0].get('language')}"
        else:
            names = ", ".join(str(t.get("language") or t.get("label")) for t in selected[:4])
            if len(selected) > 4:
                names += f" +{len(selected) - 4}"
            audio_txt = f"Аудио: {names}"
        self.meta_var.set(audio_txt)
        self.audio_btn.configure(text=f"Аудио ({len(selected)}/{len(self.audio_tracks)})")

    def _pick_audio(self) -> None:
        if len(self.audio_tracks) <= 1:
            return
        current = list(self.selected_audio_langs) or [
            str(t.get("language")) for t in self._default_audio_selection()
        ]
        dialog = AudioTrackPickerDialog(self.app, self.audio_tracks, current)
        self.app.wait_window(dialog)
        if dialog.result is not None:
            self._set_selected_audio(list(dialog.result))

    def selected_audio_tracks(self) -> list[dict]:
        # Single-track videos use yt-dlp default audio (fast clients). Explicit itags 403 often.
        if len(self.audio_tracks) <= 1:
            return []
        if self.selected_audio_langs:
            resolved = resolve_audio_tracks_by_langs(self.audio_tracks, self.selected_audio_langs)
            if resolved:
                return resolved
        if self.selected_audio:
            return list(self.selected_audio)
        return self._default_audio_selection()

    def _show_progress(self) -> None:
        self.configure_frame.pack_forget()
        self.progress_frame.pack(fill="x")
        self.download_btn.configure(state="disabled")
        self._set_btn_enabled(self.btn_download, False)

    def _on_bar_configure(self, event) -> None:
        self._bar_width = max(1, event.width)
        self._paint_progress()

    def _paint_progress(self) -> None:
        theme = self.app.theme
        if self.state == "err":
            fill, track = theme["danger"], "#3a1518" if self.app.theme_name == "dark" else "#fee2e2"
            fg = theme["danger"]
        elif self.state == "ok":
            fill, track = theme["success"], theme["accent_dim"]
            fg = theme["success"]
        else:
            fill, track = theme["accent"], theme["accent_dim"]
            fg = theme["muted"]
        if self.percent is None:
            width = int(self._bar_width * (0.22 if self.state == "run" else 0.0))
            if self.state == "ok":
                width = self._bar_width
        else:
            width = int(self._bar_width * max(0.0, min(100.0, float(self.percent))) / 100.0)
        self.bar_wrap.configure(bg=track)
        self.bar_fill.configure(bg=fill, width=max(0, width))
        self.status_overlay.configure(bg=theme["card"], fg=fg)

    def apply_theme(self) -> None:
        theme = self.app.theme
        self.configure(bg=theme["card"], highlightbackground=theme["border"], highlightcolor=theme["border"])
        if hasattr(self, "rail"):
            self.rail.configure(bg=theme["rail"])
        for widget in self.winfo_children():
            self._recolor(widget, theme["card"])
        self.thumb_frame.configure(bg=theme["thumb"])
        if self._photo is None:
            self.thumb_label.configure(bg=theme["thumb"], fg=theme["muted"])
        else:
            self.thumb_label.configure(bg=theme["thumb"])
        self.title_label.configure(bg=theme["card"], fg=theme["text"])
        self.meta_label.configure(bg=theme["card"], fg=theme["muted"])
        self.status_overlay.configure(bg=theme["card"])
        self.download_btn.configure(bg=theme["accent"], fg=theme["accent_text"], activebackground=theme["accent_hover"])
        self.audio_btn.configure(
            bg=theme["chip"],
            fg=theme["text"],
            activebackground=theme["chip_hover"],
        )
        for btn in (self.btn_close, self.btn_open_url, self.btn_download, self.btn_folder, self.btn_info):
            btn.configure(bg=theme["chip"])
            if btn is self.btn_close:
                btn._base_color = theme["danger"]  # type: ignore[attr-defined]
            self._set_btn_enabled(btn, bool(getattr(btn, "_enabled", True)))
        self._paint_progress()

    def _recolor(self, widget: tk.Misc, color: str) -> None:
        try:
            if widget not in (
                self.thumb_frame,
                self.thumb_label,
                self.bar_wrap,
                self.bar_fill,
                self.status_overlay,
                self.download_btn,
                self.audio_btn,
                getattr(self, "rail", None),
            ):
                widget.configure(bg=color)  # type: ignore[call-arg]
        except tk.TclError:
            pass
        for child in widget.winfo_children():
            self._recolor(child, color)

    def set_thumbnail_path(self, path: str) -> None:
        try:
            photo = tk.PhotoImage(file=path)
            self._photo = photo
            self.thumb_label.configure(image=photo, text="")
            self.thumb_label.image = photo  # type: ignore[attr-defined]
        except tk.TclError:
            pass

    def apply_probe(self, data: dict) -> None:
        self.probe = dict(data)
        self.state = "ready"
        if data.get("title"):
            self.title_var.set(str(data["title"]))
        if data.get("thumbnail"):
            self.thumbnail_url = data["thumbnail"]
            self.app.request_thumbnail(self.index, data["thumbnail"])
        self.qualities = list(data.get("qualities") or [])
        if not self.qualities:
            self.qualities = [
                {
                    "label": "Максимальное (лучшее)",
                    "ytdlp_format": "bv*+ba/b",
                    "video_format": "bv*",
                    "choice_url": None,
                }
            ]
        labels = [str(item.get("label") or "Качество") for item in self.qualities]
        self.quality_box.configure(values=labels)
        self.quality_box.current(0)  # maximum by default
        self.quality_var.set(labels[0])
        self.audio_tracks = list(data.get("audio_tracks") or [])
        self._set_selected_audio(self._default_audio_selection())
        self._show_ready()

    def apply_error(self, message: str) -> None:
        self.state = "err"
        self.detail = friendly_download_error(message)
        self.meta_var.set(self.detail)
        self.status_overlay.configure(text=self.detail[:80])
        self._show_progress()
        self.percent = 8
        self._paint_progress()
        if self._photo is None:
            self.thumb_label.configure(text="!", fg=self.app.theme["danger"])
        self.download_btn.configure(text="Повтор", state="normal", command=self._retry)
        if not self.download_btn.winfo_ismapped():
            self.configure_frame.pack(fill="x")
            self.download_btn.pack(side="left")
        self._set_btn_enabled(self.btn_download, True)
        self.btn_download.configure(text="↻")
        self.btn_download._command = self._retry  # type: ignore[attr-defined]

    def _retry(self) -> None:
        if self.state == "run":
            return
        if self.probe:
            self.app.start_card_download(self.index)
        else:
            self.app.reprobe_card(self.index)

    def selected_quality(self) -> dict:
        index = max(0, self.quality_box.current())
        if index < len(self.qualities):
            return self.qualities[index]
        return {
            "label": "Максимальное (лучшее)",
            "ytdlp_format": "bv*+ba/b",
            "video_format": "bv*",
            "choice_url": None,
        }

    def update_item(self, data: dict) -> None:
        if data.get("url"):
            self.url = data["url"]
            if not data.get("title") and self.state == "fetch":
                self.title_var.set(data["url"])
        if data.get("title"):
            self.title_var.set(str(data["title"]))
        if "state" in data:
            self.state = data["state"]
        if data.get("detail") is not None:
            self.detail = str(data["detail"])
            self.status_overlay.configure(text=self.detail)
        if data.get("output"):
            self.output = data["output"]
            self._set_btn_enabled(self.btn_download, True)
            self.btn_download.configure(text="📂")
            self.btn_download._command = self._open_file  # type: ignore[attr-defined]
        if "percent" in data:
            self.percent = data["percent"]
        if data.get("thumbnail"):
            self.thumbnail_url = data["thumbnail"]
            self.app.request_thumbnail(self.index, data["thumbnail"])
        if data.get("speed"):
            self._speed = str(data["speed"])
        if data.get("eta"):
            self._eta = str(data["eta"])
        parts = [p for p in (self._eta, self._speed) if p]
        if parts:
            self.meta_var.set("   ".join(parts))
        if self.state == "ok":
            self.percent = 100
            self.meta_var.set(self.detail if self.detail else "Готово")
            if self._photo is None:
                self.thumb_label.configure(text="✓", fg=self.app.theme["accent"])
        elif self.state == "err" and self._photo is None:
            self.thumb_label.configure(text="!", fg=self.app.theme["danger"])
        elif self.state == "run":
            self._show_progress()
        self._paint_progress()

    def set_progress(self, percent: float | None, status: str | None = None, speed: str = "", eta: str = "") -> None:
        self.percent = percent
        if status:
            self.detail = status
            self.status_overlay.configure(text=status)
        if speed:
            self._speed = speed
        if eta:
            self._eta = eta
        parts = [p for p in (self._eta, self._speed) if p]
        if parts:
            self.meta_var.set("   ".join(parts))
        self._paint_progress()

    def _start_download(self) -> None:
        if self.state not in ("ready", "err", "ok", "stop"):
            if self.state == "run":
                return
        if self.state == "ok" and self.output:
            self._open_file()
            return
        self.app.start_card_download(self.index)

    def _remove(self) -> None:
        if self.state == "run":
            self.cancel.set()
        self.app.remove_card(self.index)

    def _open_url(self) -> None:
        if self.url.startswith(("http://", "https://")):
            open_path(self.url)

    def _open_file(self) -> None:
        path = self.output or self.app.last_output
        if path and Path(path).exists():
            open_path(path)

    def _open_folder(self) -> None:
        path = self.output or self.app.last_output or self.app.dir_var.get().strip()
        folder = Path(path)
        if folder.is_file():
            folder = folder.parent
        if folder.exists():
            open_path(folder)

    def _show_info(self) -> None:
        quality = self.selected_quality().get("label", "")
        lines = [f"URL: {self.url}", f"Статус: {self.state}", f"Качество: {quality}", f"Детали: {self.detail}"]
        audios = self.selected_audio_tracks()
        if audios:
            lines.append("Аудио: " + ", ".join(str(a.get("label") or a.get("language")) for a in audios))
        if self.output:
            lines.append(f"Файл: {self.output}")
        if self.thumbnail_url:
            lines.append(f"Превью: {self.thumbnail_url}")
        messagebox.showinfo("Информация", "\n".join(lines), parent=self.app)


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("HLS Загрузчик")
        self.minsize(*MIN_WINDOW)
        if ICON_PATH.exists():
            try:
                self.iconbitmap(default=str(ICON_PATH))
            except tk.TclError:
                pass
        self.settings = load_settings()
        self.theme_name = self.settings.get("theme") if self.settings.get("theme") in THEMES else "dark"
        self.theme = THEMES[self.theme_name]
        self.configure(bg=self.theme["bg"])
        configure_ttk_style(self.theme)
        self.events: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.worker: threading.Thread | None = None
        self.proc: subprocess.Popen | None = None
        self.variants: list[Variant] = []
        self.audios: list[AudioTrack] = []
        self.last_output = ""
        self.progress_time = 0.0
        self.progress_size: int | None = None
        self.progress_speed = ""
        self._setting_name = False
        self._name_edited = False
        self._indeterminate = False
        self._queue_items: list[dict] = []
        self._cards: list[DownloadCard] = []
        self._active_index = 0
        self._settings_win: tk.Toplevel | None = None
        self._thumb_jobs: set[str] = set()
        self._build()
        self._restore_geometry()
        self.after(100, self._poll)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _restore_geometry(self) -> None:
        if bool(self.settings.get("remember_geometry")):
            saved = str(self.settings.get("geometry") or "").strip()
            if saved and "x" in saved:
                try:
                    size = saved.split("+", 1)[0]
                    width_s, height_s = size.split("x", 1)
                    width, height = int(width_s), int(height_s)
                    if width >= MIN_WINDOW[0] and height >= MIN_WINDOW[1]:
                        self.geometry(saved)
                        return
                except (ValueError, tk.TclError):
                    pass
        w, h = (int(x) for x in DEFAULT_GEOMETRY.split("x"))
        # Fit default size onto smaller screens.
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w = min(w, max(MIN_WINDOW[0], sw - 80))
        h = min(h, max(MIN_WINDOW[1], sh - 100))
        center_window(self, w, h)

    def _build(self) -> None:
        t = self.theme
        # Slim top nav
        self.header = tk.Frame(self, bg=t["header"])
        self.header.pack(fill="x")
        self.header_line = tk.Frame(self, bg=t["separator"], height=1)
        self.header_line.pack(fill="x")

        header_pad = tk.Frame(self.header, bg=t["header"])
        header_pad.pack(fill="x", padx=28, pady=14)
        header_pad.app_theme_owner = self  # type: ignore[attr-defined]

        brand_row = tk.Frame(header_pad, bg=t["header"])
        brand_row.pack(fill="x")
        brand_row.app_theme_owner = self  # type: ignore[attr-defined]

        self.brand_mark = tk.Frame(brand_row, bg=t["accent"], width=10, height=10)
        self.brand_mark.pack(side="left", padx=(0, 10))
        self.brand_mark.pack_propagate(False)

        self.brand_label = tk.Label(
            brand_row,
            text="HLS",
            bg=t["header"],
            fg=t["text"],
            font=(UI_FONT_SEMI, 15),
            anchor="w",
        )
        self.brand_label.pack(side="left")
        self.brand_sub = tk.Label(
            brand_row,
            text="Downloader",
            bg=t["header"],
            fg=t["muted"],
            font=(UI_FONT, 15),
            anchor="w",
        )
        self.brand_sub.pack(side="left", padx=(6, 0))

        self.settings_btn = make_nav_chip(brand_row, "Настройки", t, self.open_settings)
        self.settings_btn.pack(side="right")
        self.clear_btn = make_nav_chip(brand_row, "Очистить", t, self.clear_all_downloads)
        self.clear_btn.pack(side="right", padx=(0, 4))
        self.history_btn = make_nav_chip(brand_row, "История", t, self.open_history)
        self.history_btn.pack(side="right", padx=(0, 4))
        self.proxy_chip = make_nav_chip(brand_row, "Прокси: выкл", t, self.open_settings)
        self.proxy_chip.pack(side="right", padx=(0, 4))

        # Composer band
        self.composer = tk.Frame(self, bg=t["surface"])
        self.composer.pack(fill="x")
        composer_inner = tk.Frame(self.composer, bg=t["surface"])
        composer_inner.pack(fill="x", padx=28, pady=18)

        self.search_shell = tk.Frame(
            composer_inner,
            bg=t["input"],
            highlightthickness=1,
            highlightbackground=t["border"],
            highlightcolor=t["accent"],
        )
        self.search_shell.pack(fill="x")
        shell_inner = tk.Frame(self.search_shell, bg=t["input"])
        shell_inner.pack(fill="x", padx=4, pady=4)
        self.search_shell.bind("<Button-1>", lambda _e: self.url_entry.focus_set())
        shell_inner.bind("<Button-1>", lambda _e: self.url_entry.focus_set())

        self.url_var = tk.StringVar()
        self._url_placeholder = "Вставьте ссылку на видео…  Ctrl+V"
        self.url_entry = tk.Entry(
            shell_inner,
            textvariable=self.url_var,
            font=(UI_FONT, 13),
            bg=t["input"],
            fg=t["muted"],
            insertbackground=t["entry_insert"],
            relief="flat",
            highlightthickness=0,
            bd=0,
        )
        self.url_entry.pack(side="left", fill="x", expand=True, ipady=14, padx=(14, 8))
        self.url_entry.insert(0, self._url_placeholder)
        self.url_entry.bind("<FocusIn>", self._url_focus_in)
        self.url_entry.bind("<FocusOut>", self._url_focus_out)
        self.url_entry.bind("<Return>", lambda _e: self.start_download())
        self._bind_clipboard_shortcuts()
        self._menu = tk.Menu(self, tearoff=0)
        self._menu.add_command(label="Вставить", command=self.paste_url)
        self._menu.add_command(label="Копировать", command=self._copy_url)
        self._menu.add_command(label="Вырезать", command=self._cut_url)
        self.url_entry.bind("<Button-3>", lambda e: self._menu.tk_popup(e.x_root, e.y_root))
        self.after(50, self.url_entry.focus_set)
        self.after(200, self._maybe_autofill_clipboard)
        self.bind("<FocusIn>", self._on_app_focus_in)

        self.add_btn = tk.Button(
            shell_inner,
            text="Добавить",
            command=self.start_download,
            bg=t["accent"],
            fg=t["accent_text"],
            activebackground=t["accent_hover"],
            activeforeground=t["accent_text"],
            relief="flat",
            font=(UI_FONT_SEMI, 11),
            padx=22,
            pady=12,
            cursor="hand2",
            bd=0,
        )
        self.add_btn.pack(side="left", padx=(0, 6))

        self.stop_btn = tk.Button(
            shell_inner,
            text="Стоп",
            command=self.stop_download,
            bg=t["chip"],
            fg=t["muted"],
            activebackground=t["chip_hover"],
            activeforeground=t["text"],
            relief="flat",
            font=(UI_FONT, 10),
            padx=16,
            pady=12,
            state="disabled",
            bd=0,
        )
        self.stop_btn.pack(side="left", padx=(0, 4))

        self.dir_var = tk.StringVar(value=self.settings.get("output_dir") or str(default_output_dir()))
        self.name_var = tk.StringVar(value="video.mp4")
        self.name_var.trace_add("write", self._on_name_write)
        self.ua_var = tk.StringVar(value=self.settings.get("user_agent") or DEFAULT_UA)
        saved_preset = str(self.settings.get("ua_preset") or "")
        if saved_preset not in USER_AGENT_PRESETS:
            saved_preset = detect_ua_preset(self.ua_var.get())
        self.ua_preset_var = tk.StringVar(value=saved_preset)
        self.ua_spoof_var = tk.BooleanVar(value=bool(self.settings.get("ua_spoof")))
        self.cookies_var = tk.StringVar(value=self.settings.get("cookies") or "")
        self.referer_var = tk.StringVar(value=self.settings.get("referer") or "")
        saved_proxy = self.settings.get("proxy") or ""
        self.proxy_scheme_var = tk.StringVar(
            value=str(self.settings.get("proxy_scheme") or detect_proxy_scheme(str(saved_proxy)))
        )
        self.proxy_var = tk.StringVar(value=str(saved_proxy))
        self.limit_var = tk.StringVar(value="")
        self.threads_var = tk.StringVar(value=str(self.settings.get("threads") or DEFAULT_THREADS))
        self.direct_threads_var = tk.StringVar(
            value=str(self.settings.get("direct_threads") or DEFAULT_DIRECT_THREADS)
        )
        self.insecure_var = tk.BooleanVar(value=bool(self.settings.get("insecure")))
        self.remember_geometry_var = tk.BooleanVar(value=bool(self.settings.get("remember_geometry")))
        self.auto_clipboard_var = tk.BooleanVar(
            value=bool(self.settings["auto_clipboard"]) if "auto_clipboard" in self.settings else True
        )
        self.notify_var = tk.BooleanVar(
            value=bool(self.settings["notify"]) if "notify" in self.settings else True
        )
        self.quality_var = tk.StringVar(value="Авто (лучшее)")
        self.audio_var = tk.StringVar(value="По умолчанию")
        self.extra_storage = self.settings.get("extra") or ""

        self.list_wrap = tk.Frame(self, bg=t["bg"])
        self.list_wrap.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(self.list_wrap, bg=t["bg"], highlightthickness=0, bd=0)
        self.scrollbar = ttk.Scrollbar(self.list_wrap, orient="vertical", command=self.canvas.yview, style="Metro.Vertical.TScrollbar")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True, padx=28, pady=(8, 16))
        self.cards_frame = tk.Frame(self.canvas, bg=t["bg"])
        self._canvas_window = self.canvas.create_window((0, 0), window=self.cards_frame, anchor="nw")
        self.cards_frame.bind("<Configure>", lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)

        self.empty_wrap = tk.Frame(self.cards_frame, bg=t["bg"])
        self.empty_wrap.pack(fill="both", expand=True, pady=72)
        self.empty_badge = tk.Frame(
            self.empty_wrap,
            bg=t["surface"],
            highlightthickness=1,
            highlightbackground=t["border"],
            width=72,
            height=72,
        )
        self.empty_badge.pack()
        self.empty_badge.pack_propagate(False)
        self.empty_icon = tk.Label(
            self.empty_badge,
            text="↓",
            bg=t["surface"],
            fg=t["accent"],
            font=(UI_FONT_SEMI, 28),
        )
        self.empty_icon.place(relx=0.5, rely=0.5, anchor="center")
        self.empty_label = tk.Label(
            self.empty_wrap,
            text="Очередь пуста",
            bg=t["bg"],
            fg=t["text"],
            font=(UI_FONT_SEMI, 16),
            pady=14,
        )
        self.empty_label.pack()
        self.empty_hint = tk.Label(
            self.empty_wrap,
            text="YouTube · VK · Rutube · Chaturbate · HLS\nВставьте ссылку сверху и нажмите Добавить",
            bg=t["bg"],
            fg=t["muted"],
            font=(UI_FONT, 10),
            justify="center",
        )
        self.empty_hint.pack()

        self.footer = tk.Frame(self, bg=t["header"])
        self.footer.pack(fill="x")
        self.footer_line = tk.Frame(self, bg=t["separator"], height=1)
        self.footer_line.pack(fill="x", before=self.footer)
        foot_inner = tk.Frame(self.footer, bg=t["header"])
        foot_inner.pack(fill="x", padx=28, pady=10)
        foot_inner.app_theme_owner = self  # type: ignore[attr-defined]
        self.status_var = tk.StringVar(value="Готово")
        self.status = tk.Label(
            foot_inner,
            textvariable=self.status_var,
            bg=t["header"],
            fg=t["muted"],
            font=(UI_FONT, 9),
            anchor="w",
        )
        self.status.pack(side="left", fill="x", expand=True)
        self.theme_btn = make_nav_chip(
            foot_inner,
            "Светлая тема" if self.theme_name == "dark" else "Тёмная тема",
            t,
            self.toggle_theme,
        )
        self.theme_btn.pack(side="right")

        # Compat aliases
        self.download_btn = self.add_btn
        self.open_btn = self.add_btn
        self.refresh_btn = self.add_btn
        self.quality_box = ttk.Combobox(self, textvariable=self.quality_var, values=["Авто (лучшее)"])
        self.audio_box = ttk.Combobox(self, textvariable=self.audio_var)
        self.progress = ttk.Progressbar(self)
        self.log = ScrolledText(self, height=1)
        self.extra_text = ScrolledText(self, height=1)
        if self.extra_storage:
            self.extra_text.insert("1.0", self.extra_storage)
        self.queue_frame = tk.Frame(self)
        self.queue_list = tk.Listbox(self.queue_frame)
        self._refresh_proxy_chip()
        self.audio_row = tk.Frame(self)
        self.folder_row = tk.Frame(self)
        self.adv = tk.Frame(self)
        self.actions = tk.Frame(self)
        self.top = self.header
        self.empty_label_alias = self.empty_wrap

    def _on_canvas_configure(self, event) -> None:
        self.canvas.itemconfigure(self._canvas_window, width=event.width)

    def _on_mousewheel(self, event) -> None:
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def toggle_theme(self) -> None:
        self.set_theme("light" if self.theme_name == "dark" else "dark")

    def set_theme(self, name: str) -> None:
        if name not in THEMES:
            return
        self.theme_name = name
        self.theme = THEMES[name]
        self._apply_theme()
        self._persist()

    def _apply_theme(self) -> None:
        t = self.theme
        configure_ttk_style(t)
        self.configure(bg=t["bg"])
        self.header.configure(bg=t["header"])
        if hasattr(self, "header_line"):
            self.header_line.configure(bg=t["separator"])
        if hasattr(self, "footer_line"):
            self.footer_line.configure(bg=t["separator"])
        if hasattr(self, "brand_mark"):
            self.brand_mark.configure(bg=t["accent"])
        if hasattr(self, "composer"):
            self.composer.configure(bg=t["surface"])
            self._recolor_tree(self.composer, t["surface"])
        self._recolor_tree(self.header, t["header"])
        self.brand_label.configure(bg=t["header"], fg=t["text"])
        self.brand_sub.configure(bg=t["header"], fg=t["muted"])
        self.search_shell.configure(bg=t["input"], highlightbackground=t["border"], highlightcolor=t["accent"])
        self._recolor_tree(self.search_shell, t["input"])
        self.list_wrap.configure(bg=t["bg"])
        self.canvas.configure(bg=t["bg"])
        self.cards_frame.configure(bg=t["bg"])
        self.footer.configure(bg=t["header"])
        self._recolor_tree(self.footer, t["header"])
        placeholder_on = self._url_is_placeholder()
        self.url_entry.configure(
            bg=t["input"],
            fg=t["muted"] if placeholder_on else t["text"],
            insertbackground=t["entry_insert"],
        )
        self.add_btn.configure(
            bg=t["accent"],
            fg=t["accent_text"],
            activebackground=t["accent_hover"],
            activeforeground=t["accent_text"],
        )
        busy = any(c.state == "run" for c in self._cards)
        self.stop_btn.configure(
            bg=t["danger"] if busy else t["chip"],
            fg=("#ffffff" if busy else t["muted"]),
            activebackground=t["danger"] if busy else t["chip_hover"],
            activeforeground="#ffffff" if busy else t["text"],
        )
        for btn in (self.settings_btn, self.history_btn, getattr(self, "clear_btn", None), self.theme_btn):
            if btn is None:
                continue
            btn.configure(bg=t["header"], fg=t["muted"])
        self.status.configure(bg=t["header"], fg=t["muted"])
        self.theme_btn.configure(text="Светлая тема" if self.theme_name == "dark" else "Тёмная тема")
        self.empty_wrap.configure(bg=t["bg"])
        if hasattr(self, "empty_badge"):
            self.empty_badge.configure(bg=t["surface"], highlightbackground=t["border"])
            self.empty_icon.configure(bg=t["surface"], fg=t["accent"])
        else:
            self.empty_icon.configure(bg=t["bg"], fg=t["accent"])
        self.empty_label.configure(bg=t["bg"], fg=t["text"])
        self.empty_hint.configure(bg=t["bg"], fg=t["muted"])
        self._refresh_proxy_chip()
        for card in self._cards:
            card.apply_theme()

    def _recolor_tree(self, widget: tk.Misc, color: str) -> None:
        try:
            widget.configure(bg=color)  # type: ignore[call-arg]
        except tk.TclError:
            pass
        skip = {
            self.add_btn,
            self.stop_btn,
            self.settings_btn,
            self.history_btn,
            getattr(self, "clear_btn", None),
            getattr(self, "proxy_chip", None),
            self.theme_btn,
            self.url_entry,
            self.brand_label,
            self.brand_sub,
            self.search_shell,
            getattr(self, "brand_mark", None),
        }
        for child in widget.winfo_children():
            if child in skip:
                continue
            self._recolor_tree(child, color)

    def _url_is_placeholder(self) -> bool:
        return self.url_var.get() == getattr(self, "_url_placeholder", "")

    def _url_focus_in(self, _event=None) -> None:
        if self._url_is_placeholder():
            self.url_entry.delete(0, "end")
            self.url_entry.configure(fg=self.theme["text"])
        self.search_shell.configure(highlightbackground=self.theme["accent"], highlightcolor=self.theme["accent"])

    def _url_focus_out(self, _event=None) -> None:
        if not self.url_var.get().strip():
            self.url_entry.delete(0, "end")
            self.url_entry.insert(0, self._url_placeholder)
            self.url_entry.configure(fg=self.theme["muted"])
        self.search_shell.configure(highlightbackground=self.theme["border"], highlightcolor=self.theme["accent"])

    def _url_text(self) -> str:
        text = self.url_var.get().strip()
        if text == getattr(self, "_url_placeholder", ""):
            return ""
        return text

    def _on_app_focus_in(self, _event=None) -> None:
        # Debounce FocusIn spam from child widgets.
        if getattr(self, "_autofill_after", None):
            try:
                self.after_cancel(self._autofill_after)
            except Exception:
                pass
        self._autofill_after = self.after(250, self._maybe_autofill_clipboard)

    def _maybe_autofill_clipboard(self) -> None:
        self._autofill_after = None
        if not bool(self.auto_clipboard_var.get()):
            return
        if self._url_text():
            return
        try:
            text = self.clipboard_get().strip()
        except tk.TclError:
            return
        if not looks_like_video_url(text):
            return
        self.url_entry.focus_set()
        if self._url_is_placeholder():
            self.url_entry.delete(0, "end")
            self.url_entry.configure(fg=self.theme["text"])
        self.url_var.set(text.splitlines()[0].strip() if "\n" not in text else text)
        self._maybe_suggest_name()
        self.status_var.set("Ссылка из буфера")

    def show_toast(self, title: str, message: str, *, ok: bool = True) -> None:
        if not bool(self.notify_var.get()):
            return
        play_alert_sound(ok)
        theme = self.theme
        toast = tk.Toplevel(self)
        toast.overrideredirect(True)
        toast.attributes("-topmost", True)
        try:
            toast.attributes("-alpha", 0.96)
        except tk.TclError:
            pass
        toast.configure(bg=theme["card"])
        frame = tk.Frame(toast, bg=theme["card"], highlightthickness=1, highlightbackground=theme["border"])
        frame.pack(fill="both", expand=True)
        accent = theme["success"] if ok else theme["danger"]
        tk.Frame(frame, bg=accent, width=4).pack(side="left", fill="y")
        body = tk.Frame(frame, bg=theme["card"])
        body.pack(side="left", fill="both", expand=True, padx=12, pady=10)
        tk.Label(body, text=title, bg=theme["card"], fg=theme["text"], font=(UI_FONT_SEMI, 10), anchor="w").pack(fill="x")
        tk.Label(
            body,
            text=message[:120],
            bg=theme["card"],
            fg=theme["muted"],
            font=(UI_FONT, 9),
            anchor="w",
            wraplength=280,
            justify="left",
        ).pack(fill="x", pady=(4, 0))
        self.update_idletasks()
        width, height = 320, 78
        x = self.winfo_rootx() + self.winfo_width() - width - 24
        y = self.winfo_rooty() + self.winfo_height() - height - 48
        toast.geometry(f"{width}x{height}+{max(0, x)}+{max(0, y)}")
        toast.after(3500, toast.destroy)

    def reprobe_card(self, index: int) -> None:
        if not (0 <= index < len(self._cards)):
            return
        card = self._cards[index]
        if card.state == "run":
            return
        card.state = "fetch"
        card.probe = {}
        card.percent = None
        card.download_btn.configure(text="Скачать", command=card._start_download, state="disabled")
        card._show_fetching()
        self._start_probe(index, card.url)

    def _resolved_proxy(self) -> str:
        raw = self.proxy_var.get().strip()
        if not raw:
            return ""
        return normalize_proxy_url(raw, self.proxy_scheme_var.get())

    def _effective_ua(self) -> str:
        return resolve_user_agent(
            self.ua_preset_var.get(),
            self.ua_var.get(),
            bool(self.ua_spoof_var.get()),
        )

    def _cookies_path(self) -> str:
        return self.cookies_var.get().strip()

    def _refresh_proxy_chip(self) -> None:
        if not hasattr(self, "proxy_chip"):
            return
        t = self.theme
        try:
            proxy = self._resolved_proxy()
        except PlaylistError:
            proxy = self.proxy_var.get().strip()
        if proxy:
            self.proxy_chip.configure(
                text=f"Прокси · {proxy_display(proxy)}",
                bg=t["accent_dim"],
                fg=t["accent"],
            )
        else:
            self.proxy_chip.configure(text="Прокси: выкл", bg=t["header"], fg=t["muted"])
        if not self.proxy_chip.winfo_ismapped():
            self.proxy_chip.pack(side="right", padx=(0, 4), before=self.history_btn)

    def open_settings(self) -> None:
        if self._settings_win is not None and self._settings_win.winfo_exists():
            self._settings_win.lift()
            return
        t = self.theme
        win = tk.Toplevel(self)
        self._settings_win = win
        win.title("Настройки")
        win.configure(bg=t["bg"])
        win.resizable(True, True)
        win.minsize(560, 420)
        win.transient(self)
        # Cap height to the usable screen so content stays reachable via scroll.
        max_h = max(480, win.winfo_screenheight() - 100)
        center_window(win, 680, min(820, max_h))

        # Footer first so Save/Cancel never get clipped by long content.
        footer = tk.Frame(win, bg=t["bg"])
        footer.pack(side="bottom", fill="x", padx=22, pady=(8, 16))

        header = tk.Frame(win, bg=t["bg"])
        header.pack(side="top", fill="x", padx=22, pady=(16, 0))
        tk.Label(
            header,
            text="Настройки",
            bg=t["bg"],
            fg=t["text"],
            font=(UI_FONT_SEMI, 18),
            anchor="w",
        ).pack(fill="x", pady=(0, 6))
        tk.Label(
            header,
            text="Внешний вид, прокси, User-Agent и cookies",
            bg=t["bg"],
            fg=t["muted"],
            font=(UI_FONT, 9),
            anchor="w",
        ).pack(fill="x", pady=(0, 12))

        scroll_wrap = tk.Frame(win, bg=t["bg"])
        scroll_wrap.pack(fill="both", expand=True, padx=22, pady=(0, 4))
        canvas = tk.Canvas(scroll_wrap, bg=t["bg"], highlightthickness=0, bd=0)
        scroll = ttk.Scrollbar(scroll_wrap, orient="vertical", command=canvas.yview, style="Metro.Vertical.TScrollbar")
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        outer = tk.Frame(canvas, bg=t["bg"])
        outer_id = canvas.create_window((0, 0), window=outer, anchor="nw")

        def _sync_scroll(_event=None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _sync_width(event) -> None:
            canvas.itemconfigure(outer_id, width=event.width)

        outer.bind("<Configure>", _sync_scroll)
        canvas.bind("<Configure>", _sync_width)

        def _on_mousewheel(event) -> None:
            if getattr(event, "delta", 0):
                canvas.yview_scroll(int(-event.delta / 120), "units")
            elif getattr(event, "num", None) == 4:
                canvas.yview_scroll(-1, "units")
            elif getattr(event, "num", None) == 5:
                canvas.yview_scroll(1, "units")

        def _bind_wheel(_event=None) -> None:
            canvas.bind_all("<MouseWheel>", _on_mousewheel)
            canvas.bind_all("<Button-4>", _on_mousewheel)
            canvas.bind_all("<Button-5>", _on_mousewheel)

        def _unbind_wheel(_event=None) -> None:
            canvas.unbind_all("<MouseWheel>")
            canvas.unbind_all("<Button-4>")
            canvas.unbind_all("<Button-5>")

        win.bind("<Enter>", _bind_wheel)
        win.bind("<Leave>", _unbind_wheel)
        win.bind("<Destroy>", lambda _e: _unbind_wheel())

        def section(title: str) -> tk.Frame:
            box = tk.Frame(outer, bg=t["card"], highlightthickness=1, highlightbackground=t["border"])
            box.pack(fill="x", pady=(0, 12))
            inner = tk.Frame(box, bg=t["card"])
            inner.pack(fill="x", padx=18, pady=16)
            head = tk.Frame(inner, bg=t["card"])
            head.pack(fill="x", pady=(0, 12))
            tk.Frame(head, bg=t["accent"], width=3, height=14).pack(side="left", padx=(0, 10))
            tk.Label(head, text=title, bg=t["card"], fg=t["text"], font=(UI_FONT_SEMI, 11), anchor="w").pack(
                side="left"
            )
            return inner

        # Theme + folder
        general = section("Основные")
        theme_row = tk.Frame(general, bg=t["card"])
        theme_row.pack(fill="x", pady=(0, 10))
        tk.Label(theme_row, text="Тема", bg=t["card"], fg=t["muted"], font=("Segoe UI", 9), width=12, anchor="w").pack(
            side="left"
        )
        theme_var = tk.StringVar(value=self.theme_name)

        def on_theme() -> None:
            self.set_theme(theme_var.get())

        ttk.Radiobutton(theme_row, text="Тёмная", value="dark", variable=theme_var, command=on_theme).pack(side="left")
        ttk.Radiobutton(theme_row, text="Светлая", value="light", variable=theme_var, command=on_theme).pack(
            side="left", padx=(12, 0)
        )

        folder_row = tk.Frame(general, bg=t["card"])
        folder_row.pack(fill="x", pady=(0, 8))
        tk.Label(folder_row, text="Папка", bg=t["card"], fg=t["muted"], font=("Segoe UI", 9), width=12, anchor="w").pack(
            side="left"
        )
        ttk.Entry(folder_row, textvariable=self.dir_var).pack(side="left", fill="x", expand=True)
        ttk.Button(folder_row, text="Обзор", command=self.browse_dir).pack(side="left", padx=(8, 0))

        threads_row = tk.Frame(general, bg=t["card"])
        threads_row.pack(fill="x", pady=(0, 8))
        tk.Label(threads_row, text="Потоки HLS", bg=t["card"], fg=t["muted"], font=("Segoe UI", 9), width=12, anchor="w").pack(
            side="left"
        )
        ttk.Spinbox(threads_row, from_=1, to=MAX_THREADS, textvariable=self.threads_var, width=8).pack(side="left")
        tk.Label(threads_row, text="Секунд (тест)", bg=t["card"], fg=t["muted"], font=("Segoe UI", 9)).pack(
            side="left", padx=(16, 6)
        )
        ttk.Entry(threads_row, textvariable=self.limit_var, width=10).pack(side="left")

        direct_row = tk.Frame(general, bg=t["card"])
        direct_row.pack(fill="x")
        tk.Label(
            direct_row,
            text="Потоки MP4",
            bg=t["card"],
            fg=t["muted"],
            font=("Segoe UI", 9),
            width=12,
            anchor="w",
        ).pack(side="left")
        ttk.Spinbox(direct_row, from_=1, to=MAX_THREADS, textvariable=self.direct_threads_var, width=8).pack(side="left")
        tk.Label(
            general,
            text="HLS — сегменты/yt-dlp (обычно 16–24). MP4 — параллель для mixdrop/mxcontent (лучше 8–12).",
            bg=t["card"],
            fg=t["muted"],
            font=(UI_FONT, 8),
            anchor="w",
            wraplength=560,
            justify="left",
        ).pack(fill="x", pady=(6, 0))

        ttk.Checkbutton(
            general,
            text="Запоминать размер и положение окна после закрытия",
            variable=self.remember_geometry_var,
        ).pack(anchor="w", pady=(12, 0))
        ttk.Checkbutton(
            general,
            text="Подставлять ссылку из буфера при фокусе окна",
            variable=self.auto_clipboard_var,
        ).pack(anchor="w", pady=(8, 0))
        ttk.Checkbutton(
            general,
            text="Звук и всплывающее уведомление по завершении",
            variable=self.notify_var,
        ).pack(anchor="w", pady=(8, 0))

        # Proxy section — clear UX
        proxy_box = section("Прокси · HTTP / HTTPS / SOCKS5")
        tk.Label(
            proxy_box,
            text="Выберите тип, затем вставьте адрес. Логин и пароль — по желанию.",
            bg=t["card"],
            fg=t["muted"],
            font=("Segoe UI", 9),
            anchor="w",
            wraplength=540,
            justify="left",
        ).pack(fill="x", pady=(0, 10))

        scheme_row = tk.Frame(proxy_box, bg=t["card"])
        scheme_row.pack(fill="x", pady=(0, 8))
        scheme_btns: dict[str, tk.Label] = {}

        def paint_schemes() -> None:
            current = self.proxy_scheme_var.get()
            for key, btn in scheme_btns.items():
                active = key == current
                btn.configure(
                    bg=t["accent"] if active else t["chip"],
                    fg=t["accent_text"] if active else t["text"],
                )

        def pick_scheme(scheme: str) -> None:
            self.proxy_scheme_var.set(scheme)
            paint_schemes()
            raw = self.proxy_var.get().strip()
            if raw and "://" in raw:
                try:
                    parts = urllib.parse.urlsplit(raw)
                    rebuilt = normalize_proxy_url(
                        f"{parts.netloc}{parts.path}",
                        scheme,
                    )
                    self.proxy_var.set(rebuilt)
                except PlaylistError:
                    pass

        for scheme, label in (("http", "HTTP"), ("https", "HTTPS"), ("socks5", "SOCKS5")):
            btn = tk.Label(
                scheme_row,
                text=label,
                bg=t["chip"],
                fg=t["text"],
                font=("Segoe UI Semibold", 9),
                cursor="hand2",
                padx=14,
                pady=6,
            )
            btn.pack(side="left", padx=(0, 8))
            btn.bind("<Button-1>", lambda _e, s=scheme: pick_scheme(s))
            scheme_btns[scheme] = btn
        paint_schemes()

        entry_row = tk.Frame(proxy_box, bg=t["card"])
        entry_row.pack(fill="x", pady=(0, 8))
        proxy_entry = ttk.Entry(entry_row, textvariable=self.proxy_var, font=("Consolas", 10))
        proxy_entry.pack(side="left", fill="x", expand=True, ipady=4)

        def paste_proxy() -> None:
            try:
                text = (self.clipboard_get() or "").strip()
            except tk.TclError:
                return
            if text:
                self.proxy_var.set(text)
                self.proxy_scheme_var.set(detect_proxy_scheme(text))
                paint_schemes()

        ttk.Button(entry_row, text="Вставить", command=paste_proxy).pack(side="left", padx=(8, 0))
        ttk.Button(entry_row, text="Очистить", command=lambda: self.proxy_var.set("")).pack(side="left", padx=(6, 0))

        examples = tk.Frame(proxy_box, bg=t["card"])
        examples.pack(fill="x")
        for sample in (
            "127.0.0.1:1080",
            "socks5://127.0.0.1:1080",
            "http://user:pass@1.2.3.4:8080",
            "https://1.2.3.4:8443",
        ):
            chip = tk.Label(
                examples,
                text=sample,
                bg=t["input"],
                fg=t["muted"],
                font=("Consolas", 8),
                cursor="hand2",
                padx=8,
                pady=4,
            )
            chip.pack(side="left", padx=(0, 6), pady=(4, 0))
            chip.bind("<Button-1>", lambda _e, s=sample: (self.proxy_var.set(s), self.proxy_scheme_var.set(detect_proxy_scheme(s)), paint_schemes()))

        # User-Agent + Cookies (OVD-style)
        ua_box = section("User-Agent")
        tk.Label(
            ua_box,
            text="Выберите браузер или задайте свой. «Случайный» меняет UA на каждую загрузку.",
            bg=t["card"],
            fg=t["muted"],
            font=("Segoe UI", 9),
            anchor="w",
            wraplength=540,
            justify="left",
        ).pack(fill="x", pady=(0, 8))

        preset_row = tk.Frame(ua_box, bg=t["card"])
        preset_row.pack(fill="x", pady=(0, 8))
        tk.Label(preset_row, text="Пресет", bg=t["card"], fg=t["muted"], font=("Segoe UI", 9), width=12, anchor="w").pack(
            side="left"
        )
        ua_combo = ttk.Combobox(
            preset_row,
            textvariable=self.ua_preset_var,
            values=list(USER_AGENT_PRESETS.keys()),
            state="readonly",
            width=28,
            style="Metro.TCombobox",
        )
        ua_combo.pack(side="left", fill="x", expand=True)

        ua_entry = ttk.Entry(ua_box, textvariable=self.ua_var, font=("Consolas", 9))
        ua_entry.pack(fill="x", pady=(0, 8), ipady=3)

        def on_ua_preset(_event=None) -> None:
            preset = self.ua_preset_var.get()
            value = USER_AGENT_PRESETS.get(preset)
            if value is None:
                ua_entry.configure(state="normal")
                if not self.ua_var.get().strip() or detect_ua_preset(self.ua_var.get()) != UA_PRESET_CUSTOM:
                    # keep current custom text if already custom
                    pass
            elif value == "":
                self.ua_var.set("")
                ua_entry.configure(state="normal")
            else:
                self.ua_var.set(value)
                ua_entry.configure(state="normal")

        ua_combo.bind("<<ComboboxSelected>>", on_ua_preset)
        on_ua_preset()
        ttk.Checkbutton(
            ua_box,
            text="Случайный User-Agent на каждую загрузку (как Spoof в OVD)",
            variable=self.ua_spoof_var,
        ).pack(anchor="w")

        cookies_box = section("Cookies")
        tk.Label(
            cookies_box,
            text="Файл cookies.txt (Netscape) — для приватных / age-gate / «Sign in to confirm».",
            bg=t["card"],
            fg=t["muted"],
            font=("Segoe UI", 9),
            anchor="w",
            wraplength=540,
            justify="left",
        ).pack(fill="x", pady=(0, 8))
        cookies_row = tk.Frame(cookies_box, bg=t["card"])
        cookies_row.pack(fill="x")
        ttk.Entry(cookies_row, textvariable=self.cookies_var).pack(side="left", fill="x", expand=True, ipady=3)

        def browse_cookies() -> None:
            path = filedialog.askopenfilename(
                parent=win,
                title="Выберите cookies.txt",
                filetypes=[("Cookies", "*.txt"), ("Все файлы", "*.*")],
            )
            if path:
                self.cookies_var.set(path)

        ttk.Button(cookies_row, text="Обзор", command=browse_cookies).pack(side="left", padx=(8, 0))
        ttk.Button(cookies_row, text="Убрать", command=lambda: self.cookies_var.set("")).pack(side="left", padx=(6, 0))

        # Advanced
        advanced = section("Дополнительно")
        for label, var in (("Referer", self.referer_var), ("Имя файла", self.name_var)):
            row = tk.Frame(advanced, bg=t["card"])
            row.pack(fill="x", pady=(0, 8))
            tk.Label(row, text=label, bg=t["card"], fg=t["muted"], font=("Segoe UI", 9), width=12, anchor="w").pack(
                side="left"
            )
            ttk.Entry(row, textvariable=var).pack(side="left", fill="x", expand=True)

        tk.Label(advanced, text="Доп. заголовки", bg=t["card"], fg=t["muted"], font=("Segoe UI", 9), anchor="w").pack(
            fill="x"
        )
        extra = ScrolledText(
            advanced,
            height=3,
            font=("Consolas", 9),
            bg=t["input"],
            fg=t["text"],
            insertbackground=t["entry_insert"],
            relief="flat",
            highlightthickness=1,
            highlightbackground=t["border"],
        )
        extra.pack(fill="x", pady=(4, 8))
        extra.insert("1.0", self.extra_text.get("1.0", "end").strip())
        ttk.Checkbutton(advanced, text="Не проверять сертификат HTTPS", variable=self.insecure_var).pack(anchor="w")

        def save_and_close() -> None:
            try:
                resolved = self._resolved_proxy()
            except PlaylistError as exc:
                messagebox.showerror("Прокси", str(exc), parent=win)
                return
            cookies_path = self.cookies_var.get().strip()
            if cookies_path and not Path(cookies_path).is_file():
                messagebox.showerror("Cookies", f"Файл не найден:\n{cookies_path}", parent=win)
                return
            if self.ua_preset_var.get() != UA_PRESET_CUSTOM:
                value = USER_AGENT_PRESETS.get(self.ua_preset_var.get())
                if isinstance(value, str):
                    self.ua_var.set(value)
            if resolved:
                self.proxy_var.set(resolved)
                self.proxy_scheme_var.set(detect_proxy_scheme(resolved))
            self.extra_text.delete("1.0", "end")
            self.extra_text.insert("1.0", extra.get("1.0", "end").strip())
            self._refresh_proxy_chip()
            self._persist()
            win.destroy()
            bits = []
            if resolved:
                bits.append(f"прокси {proxy_display(resolved)}")
            if cookies_path:
                bits.append(f"cookies {Path(cookies_path).name}")
            bits.append(f"UA: {self.ua_preset_var.get()}")
            self.status_var.set("Настройки сохранены · " + " · ".join(bits))

        tk.Button(
            footer,
            text="Сохранить",
            command=save_and_close,
            bg=t["accent"],
            fg=t["accent_text"],
            activebackground=t["accent_hover"],
            activeforeground=t["accent_text"],
            relief="flat",
            font=(UI_FONT_SEMI, 10),
            padx=20,
            pady=9,
            cursor="hand2",
            bd=0,
        ).pack(side="right")
        ttk.Button(footer, text="Отмена", command=win.destroy).pack(side="right", padx=(0, 8))
        win.protocol("WM_DELETE_WINDOW", win.destroy)
        win.after_idle(_sync_scroll)

    def request_thumbnail(self, index: int, url: str) -> None:
        if not url or url in self._thumb_jobs:
            return
        self._thumb_jobs.add(url)
        headers = {"User-Agent": self._effective_ua() or DEFAULT_UA, "Referer": self.referer_var.get() or url}
        try:
            cookies = self._cookies_path()
            if cookies:
                headers = apply_cookies_file(headers, cookies)
        except PlaylistError:
            pass

        def work() -> None:
            path = cache_thumbnail_image(url, headers)
            if path is not None:
                self.events.put(("thumb_ready", {"index": index, "path": str(path), "url": url}))

        threading.Thread(target=work, daemon=True).start()

    def _on_name_write(self, *_args) -> None:
        if not self._setting_name:
            self._name_edited = True

    def _set_name(self, name: str) -> None:
        self._setting_name = True
        self.name_var.set(name)
        self._setting_name = False

    def _safe_bind(self, widget: tk.Misc, sequence: str, handler) -> None:
        try:
            widget.bind(sequence, handler)
        except tk.TclError:
            pass

    def _bind_clipboard_shortcuts(self) -> None:
        """Bind paste/copy/cut; use keycode so Russian layout Ctrl+V works on Windows."""
        paste_seqs = (
            "<<Paste>>",
            "<Control-v>",
            "<Control-V>",
            "<Control-Key-v>",
            "<Control-Key-V>",
            "<Shift-Insert>",
        )
        for seq in paste_seqs:
            self._safe_bind(self.url_entry, seq, self._on_paste)
            self._safe_bind(self.search_shell, seq, self._on_paste)
            self._safe_bind(self, seq, self._on_paste_if_url_focused)
        for seq in ("<Control-c>", "<Control-C>", "<Control-Key-c>", "<Control-Key-C>"):
            self._safe_bind(self.url_entry, seq, self._on_copy)
        for seq in ("<Control-x>", "<Control-X>", "<Control-Key-x>", "<Control-Key-X>"):
            self._safe_bind(self.url_entry, seq, self._on_cut)
        for seq in ("<Control-a>", "<Control-A>", "<Control-Key-a>", "<Control-Key-A>"):
            self._safe_bind(self.url_entry, seq, self._on_select_all)
        # Layout-independent: physical V/C/X/A keys via Windows keycodes.
        self._safe_bind(self.url_entry, "<Control-KeyPress>", self._on_ctrl_keypress)
        self._safe_bind(self, "<Control-KeyPress>", self._on_ctrl_keypress_root)

    def _focus_is_url(self) -> bool:
        try:
            focused = self.focus_get()
        except tk.TclError:
            return False
        return focused is self.url_entry

    def _on_ctrl_keypress(self, event):
        # Windows virtual-key codes (layout-independent).
        code = int(getattr(event, "keycode", 0) or 0)
        if code == 86:  # V
            return self._on_paste(event)
        if code == 67:  # C
            return self._on_copy(event)
        if code == 88:  # X
            return self._on_cut(event)
        if code == 65:  # A
            return self._on_select_all(event)
        return None

    def _on_ctrl_keypress_root(self, event):
        if not self._focus_is_url():
            return None
        return self._on_ctrl_keypress(event)

    def _on_paste_if_url_focused(self, _event=None):
        if self._focus_is_url() or not self.focus_get():
            self.url_entry.focus_set()
            self.paste_url()
            return "break"
        return None

    def _on_paste(self, _event=None):
        self.url_entry.focus_set()
        self.paste_url()
        return "break"

    def _on_copy(self, _event=None):
        self._copy_url()
        return "break"

    def _on_cut(self, _event=None):
        self._cut_url()
        return "break"

    def _on_select_all(self, _event=None):
        self.url_entry.select_range(0, "end")
        self.url_entry.icursor("end")
        return "break"

    def _copy_url(self) -> None:
        try:
            if self.url_entry.selection_present():
                text = self.url_entry.selection_get()
            else:
                text = self.url_var.get()
        except tk.TclError:
            text = self.url_var.get()
        if text:
            try:
                self.clipboard_clear()
                self.clipboard_append(text)
                self.update_idletasks()
            except tk.TclError:
                pass

    def _cut_url(self) -> None:
        self._copy_url()
        try:
            if self.url_entry.selection_present():
                self.url_entry.delete("sel.first", "sel.last")
            else:
                self.url_entry.delete(0, "end")
        except tk.TclError:
            self.url_entry.delete(0, "end")

    def paste_url(self) -> None:
        text = ""
        try:
            text = self.clipboard_get()
        except tk.TclError:
            try:
                self.update()
                text = self.clipboard_get()
            except tk.TclError:
                return
        text = (text or "").strip()
        if not text:
            return
        self.url_entry.focus_set()
        if self._url_is_placeholder():
            self.url_entry.delete(0, "end")
            self.url_entry.configure(fg=self.theme["text"])
        try:
            if self.url_entry.selection_present():
                self.url_entry.delete("sel.first", "sel.last")
        except tk.TclError:
            pass
        try:
            self.url_entry.insert("insert", text)
        except tk.TclError:
            current = self._url_text()
            if current and "\n" not in text and not text.startswith("#EXTM3U"):
                self.url_var.set(current + "\n" + text)
            else:
                self.url_var.set(text)
        self.url_entry.icursor("end")
        self.url_entry.xview_moveto(1.0)
        self._maybe_suggest_name()

    def _after_paste(self) -> None:
        self._maybe_suggest_name()

    def _maybe_suggest_name(self) -> None:
        if self._name_edited:
            return
        urls = parse_url_list(self._url_text())
        if len(urls) == 1 and urls[0].startswith(("http://", "https://")):
            self._set_name(name_from_url(urls[0]))
        elif len(urls) > 1:
            self._set_name("очередь.mp4")

    def _clear_cards(self) -> None:
        for card in self._cards:
            card.destroy()
        self._cards.clear()
        self._queue_items.clear()
        self.empty_wrap.pack(fill="both", expand=True, pady=40)

    def _rebuild_cards(self, items: list[dict]) -> None:
        self._clear_cards()
        self.empty_wrap.pack_forget()
        for index, item in enumerate(items):
            card = DownloadCard(self.cards_frame, self, index, item.get("url", ""))
            card.pack(fill="x", pady=8)
            card.update_item(item)
            self._cards.append(card)
            self._queue_items.append(dict(item))

    def remove_card(self, index: int) -> None:
        if 0 <= index < len(self._cards):
            card = self._cards[index]
            if card.state == "run":
                card.cancel.set()
            card.destroy()
            del self._cards[index]
            if index < len(self._queue_items):
                del self._queue_items[index]
            for i, item in enumerate(self._cards):
                item.index = i
        if not self._cards:
            self.empty_wrap.pack(fill="both", expand=True, pady=40)
        busy = any(c.state == "run" for c in self._cards)
        self._set_busy(busy)

    def browse_dir(self) -> None:
        current = self.dir_var.get().strip() or str(default_output_dir())
        selected = filedialog.askdirectory(initialdir=current if Path(current).exists() else str(default_output_dir()))
        if selected:
            self.dir_var.set(selected)

    def _append_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip() + "\n")
        end = int(self.log.index("end-1c").split(".")[0])
        if end > 400:
            self.log.delete("1.0", f"{end - 300}.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _choice_url(self) -> str | None:
        return None

    def _audio_choice(self) -> str | None:
        return None

    def _snapshot(self) -> dict:
        limit_raw = self.limit_var.get().strip()
        limit_seconds = None
        if limit_raw:
            try:
                limit_seconds = int(float(limit_raw))
            except ValueError:
                limit_seconds = None
            if limit_seconds is not None and limit_seconds <= 0:
                limit_seconds = None
        self._maybe_suggest_name()
        try:
            proxy = self._resolved_proxy()
        except PlaylistError:
            proxy = self.proxy_var.get().strip()
        return {
            "source": self._url_text(),
            "choice_url": self._choice_url(),
            "audio_choice": self._audio_choice(),
            "output_dir": self.dir_var.get().strip() or str(default_output_dir()),
            "filename": self.name_var.get().strip() or "video.mp4",
            "name_edited": self._name_edited,
            "user_agent": self._effective_ua(),
            "referer": self.referer_var.get(),
            "extra": self.extra_text.get("1.0", "end"),
            "proxy": proxy,
            "cookies": self._cookies_path(),
            "insecure": bool(self.insecure_var.get()),
            "limit_seconds": limit_seconds,
            "threads": clamp_threads(self.threads_var.get()),
            "direct_threads": clamp_threads(self.direct_threads_var.get()),
        }

    def _set_busy(self, busy: bool) -> None:
        # Keep «Добавить» enabled so new links can join the queue while others download.
        self.add_btn.configure(state="normal")
        t = self.theme
        if busy:
            self.stop_btn.configure(
                state="normal",
                bg=t["danger"],
                fg="#ffffff",
                activebackground=t["danger"],
                activeforeground="#ffffff",
                cursor="hand2",
            )
        else:
            self.stop_btn.configure(
                state="disabled",
                bg=t["chip"],
                fg=t["muted"],
                activebackground=t["chip_hover"],
                activeforeground=t["text"],
                cursor="",
            )

    def start_download(self) -> None:
        """Add URLs to the queue and fetch metadata (no download yet)."""
        params = self._snapshot()
        urls = parse_url_list(params["source"])
        if not urls:
            messagebox.showinfo("Нет ссылки", "Вставьте одну или несколько ссылок.")
            return
        self._persist()
        self.empty_wrap.pack_forget()
        for url in urls:
            index = len(self._cards)
            card = DownloadCard(self.cards_frame, self, index, url)
            card.pack(fill="x", pady=8)
            self._cards.append(card)
            self._queue_items.append({"url": url, "state": "fetch", "detail": "Получение метаданных…"})
            self._start_probe(index, url)
        self.url_entry.delete(0, "end")
        self.url_entry.insert(0, self._url_placeholder)
        self.url_entry.configure(fg=self.theme["muted"])
        self.status_var.set(f"В очереди: {len(self._cards)}")

    def _start_probe(self, index: int, url: str) -> None:
        headers = {
            "User-Agent": self._effective_ua() or DEFAULT_UA,
            "Referer": self.referer_var.get() or url,
        }
        try:
            proxy = self._resolved_proxy()
        except PlaylistError as exc:
            self.events.put(("queue_meta_error", {"index": index, "error": f"Прокси: {exc}"}))
            return
        cookies = self._cookies_path()
        try:
            if cookies:
                headers = apply_cookies_file(headers, cookies)
        except PlaylistError as exc:
            self.events.put(("queue_meta_error", {"index": index, "error": str(exc)}))
            return

        def work() -> None:
            try:
                with use_download_proxy(proxy, cookies):
                    info = probe_media_info(url, headers, bool(self.insecure_var.get()), None)
                self.events.put(("queue_meta", {"index": index, **info}))
            except Exception as exc:
                self.events.put(("queue_meta_error", {"index": index, "error": str(exc)}))

        threading.Thread(target=work, daemon=True).start()

    def _handle_playlist_probe(self, index: int, payload: dict) -> None:
        """Show playlist picker and replace the playlist card with selected videos."""
        if not (0 <= index < len(self._cards)):
            return
        entries = payload.get("entries") or []
        if not entries:
            self._cards[index].apply_error("В плейлисте нет видео")
            return
        self._cards[index].title_var.set(str(payload.get("title") or "Плейлист YouTube"))
        self._cards[index].meta_var.set(f"Плейлист: {len(entries)} видео — выберите что скачать")
        self._cards[index].state = "fetch"
        dialog = PlaylistPickerDialog(self, payload)
        self.wait_window(dialog)
        selected = dialog.result
        if not selected:
            self.remove_card(index)
            return
        # Remove playlist placeholder card, then add selected videos as ready cards.
        self.remove_card(index)
        self.empty_wrap.pack_forget()
        qualities = [{"label": "Максимальное (лучшее)", "ytdlp_format": "bv*+ba/b", "choice_url": None}]
        for entry in selected:
            url = str(entry.get("url") or "")
            if not url:
                continue
            card_index = len(self._cards)
            card = DownloadCard(self.cards_frame, self, card_index, url)
            card.pack(fill="x", pady=8)
            self._cards.append(card)
            self._queue_items.append({"url": url, "state": "ready", "title": entry.get("title")})
            card.apply_probe(
                {
                    "url": url,
                    "title": entry.get("title") or url,
                    "thumbnail": entry.get("thumbnail"),
                    "referer": url,
                    "playlist_url": f"ytdlp:{url}",
                    "browser": False,
                    "mode": "ytdlp",
                    "ytdlp_url": url,
                    "qualities": list(qualities),
                }
            )
        self.status_var.set(f"Добавлено из плейлиста: {len(selected)}")

    def clear_download_history_file(self) -> None:
        try:
            HISTORY_PATH.write_text("[]", encoding="utf-8")
        except OSError:
            pass

    def clear_all_downloads(self) -> None:
        """Clear finished cards from the list and wipe download history."""
        running = [c for c in self._cards if c.state == "run"]
        finished = [c for c in self._cards if c.state != "run"]
        if not finished and not load_download_history():
            messagebox.showinfo("Очистка", "Нечего очищать.")
            return
        msg = "Удалить завершённые закачки из списка и очистить историю?"
        if running:
            msg = "Идёт скачивание. Удалить только завершённые и очистить историю?\n(Текущие загрузки останутся.)"
        if not messagebox.askyesno("Очистить", msg, parent=self):
            return
        # Remove finished cards (keep running ones).
        keep = list(running)
        for card in list(self._cards):
            if card.state == "run":
                continue
            card.destroy()
        self._cards = keep
        for i, card in enumerate(self._cards):
            card.index = i
        self._queue_items = [{"url": c.url, "state": c.state, "detail": c.detail} for c in self._cards]
        self.clear_download_history_file()
        if not self._cards:
            self.empty_wrap.pack(fill="both", expand=True, pady=40)
        self.status_var.set("Список и история очищены")
        self._set_busy(any(c.state == "run" for c in self._cards))

    def open_history(self) -> None:
        items = load_download_history()
        theme = self.theme
        win = tk.Toplevel(self)
        win.title("История загрузок")
        win.configure(bg=theme["bg"])
        win.geometry("720x440")
        win.transient(self)

        head = tk.Frame(win, bg=theme["bg"])
        head.pack(fill="x", padx=14, pady=(14, 6))
        tk.Label(
            head,
            text="История загрузок",
            bg=theme["bg"],
            fg=theme["text"],
            font=("Segoe UI", 12, "bold"),
            anchor="w",
        ).pack(side="left")
        count_var = tk.StringVar(value=f"{len(items)} записей")
        tk.Label(head, textvariable=count_var, bg=theme["bg"], fg=theme["muted"], font=("Segoe UI", 9)).pack(side="right")

        list_wrap = tk.Frame(win, bg=theme["bg"])
        list_wrap.pack(fill="both", expand=True, padx=14, pady=8)
        box = tk.Listbox(
            list_wrap,
            bg=theme["input"],
            fg=theme["text"],
            font=("Segoe UI", 9),
            activestyle="dotbox",
            highlightthickness=1,
            highlightbackground=theme["border"],
            selectbackground=theme["accent"],
            selectforeground=theme["accent_text"],
        )
        scroll = ttk.Scrollbar(list_wrap, orient="vertical", command=box.yview)
        box.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        box.pack(side="left", fill="both", expand=True)

        def refill() -> None:
            box.delete(0, "end")
            nonlocal_items = load_download_history()
            items.clear()
            items.extend(nonlocal_items)
            count_var.set(f"{len(items)} записей")
            if not items:
                box.insert("end", "История пуста")
            else:
                for item in items:
                    when = str(item.get("when") or "")
                    title = str(item.get("title") or item.get("url") or "")
                    mark = "✓" if item.get("ok") else "✗"
                    out = Path(str(item.get("output") or "")).name if item.get("output") else (item.get("error") or "")
                    box.insert("end", f"{mark}  {when}  ·  {title[:70]}  ·  {out}")

        refill()

        def open_selected() -> None:
            sel = box.curselection()
            if not sel or not items:
                return
            item = items[int(sel[0])]
            path = item.get("output")
            if path and Path(str(path)).exists():
                open_path(Path(str(path)).parent)
            elif item.get("url"):
                open_path(str(item["url"]))

        def retry_selected() -> None:
            sel = box.curselection()
            if not sel or not items:
                return
            item = items[int(sel[0])]
            url = str(item.get("url") or "").strip()
            if not url:
                messagebox.showinfo("История", "У записи нет ссылки.", parent=win)
                return
            win.destroy()
            self.url_entry.focus_set()
            if self._url_is_placeholder():
                self.url_entry.delete(0, "end")
                self.url_entry.configure(fg=self.theme["text"])
            self.url_var.set(url)
            self.start_download()

        def clear_history() -> None:
            if not messagebox.askyesno("История", "Очистить всю историю загрузок?", parent=win):
                return
            self.clear_download_history_file()
            refill()
            self.status_var.set("История очищена")

        buttons = tk.Frame(win, bg=theme["bg"])
        buttons.pack(fill="x", padx=14, pady=(4, 14))
        ttk.Button(buttons, text="Открыть папку", command=open_selected).pack(side="left")
        ttk.Button(buttons, text="Скачать снова", command=retry_selected).pack(side="left", padx=(8, 0))
        tk.Button(
            buttons,
            text="Очистить историю",
            command=clear_history,
            bg=theme["danger"],
            fg="#ffffff",
            activebackground=theme["danger"],
            activeforeground="#ffffff",
            relief="flat",
            font=("Segoe UI", 9, "bold"),
            padx=12,
            pady=4,
            cursor="hand2",
            bd=0,
        ).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="Закрыть", command=win.destroy).pack(side="right")

    def start_card_download(self, index: int) -> None:
        if not (0 <= index < len(self._cards)):
            return
        card = self._cards[index]
        if card.worker and card.worker.is_alive():
            return
        if card.state == "run":
            return
        quality = card.selected_quality()
        params = self._snapshot()
        params["source"] = card.url
        params["name_edited"] = False
        if card.probe.get("title"):
            params["filename"] = sanitize_filename(str(card.probe["title"])) + ".mp4"
        else:
            params["filename"] = name_from_url(card.url)
        params["choice_url"] = quality.get("choice_url")
        audio_tracks = card.selected_audio_tracks()
        format_spec, multistreams, fast_client = build_ytdlp_audio_format(quality, audio_tracks)
        params["ytdlp_format"] = format_spec
        params["audio_multistreams"] = multistreams
        params["use_fast_youtube_client"] = fast_client
        params["selected_audio"] = audio_tracks
        params["title"] = card.probe.get("title")
        params["thumbnail"] = card.probe.get("thumbnail")
        params["live"] = bool(card.probe.get("live"))
        if card.probe.get("mode") == "ytdlp" and card.probe.get("ytdlp_url"):
            params["probed"] = True
            params["playlist_url"] = "ytdlp:" + str(card.probe["ytdlp_url"])
            params["probe_referer"] = card.probe.get("referer") or card.url
            params["browser"] = False
            params["refresh_stream"] = False
        else:
            # Re-resolve media URL at download time so signed CDN links stay fresh.
            params["probed"] = True
            params["refresh_stream"] = True
            params["playlist_url"] = card.probe.get("playlist_url") or ""
            params["probe_referer"] = card.probe.get("referer") or card.url
            params["browser"] = bool(card.probe.get("browser"))
            params["probe_mode"] = card.probe.get("mode")

        if audio_tracks:
            self._append_log(
                f"Аудиодорожки ({len(audio_tracks)}): "
                + ", ".join(str(t.get("label") or t.get("language")) for t in audio_tracks)
            )
            self._append_log(f"Формат yt-dlp: {format_spec} · multistream={multistreams}")
        card.cancel.clear()
        card.state = "run"
        card.percent = None
        card._show_progress()
        start_label = "Запись эфира…" if params.get("live") else "Скачиваю…"
        card.set_progress(None, start_label)
        self.status_var.set(f"{start_label} {card.title_var.get()[:60]}")
        self._set_busy(True)

        def work() -> None:
            proxy = IndexedEvents(self.events, index, suppress_done=True)
            try:
                download_job(params, proxy, card.cancel)  # type: ignore[arg-type]
                result = proxy.last_done or {"ok": False, "error": "Нет ответа"}
            except Exception as exc:
                result = {"ok": False, "error": str(exc)}
            self.events.put(("card_done", {"index": index, **result}))

        card.worker = threading.Thread(target=work, daemon=True)
        card.worker.start()

    def refresh_playlist(self) -> None:
        messagebox.showinfo("Качество", "Качество выбирается автоматически (лучшее).")

    def stop_download(self) -> None:
        self.cancel.set()
        process = self.proc
        if process is not None and process.poll() is None:
            process.terminate()
        for card in self._cards:
            if card.state == "run":
                card.cancel.set()
                card.set_progress(None, "Останавливаю…")
        self.status_var.set("Останавливаю…")

    def _status_for_active(self) -> str:
        parts = []
        if self.progress_time > 0:
            parts.append(fmt_duration(self.progress_time))
        if self.progress_size:
            parts.append(fmt_size(self.progress_size))
        if parts:
            return "Скачиваю · " + " · ".join(parts)
        return "Скачиваю…"

    def _update_active_card(self, percent: float | None = None, status: str | None = None) -> None:
        if 0 <= self._active_index < len(self._cards):
            speed = self.progress_speed if self.progress_speed and self.progress_speed.upper() != "N/A" else ""
            self._cards[self._active_index].set_progress(percent, status, speed=speed)

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "queue_meta":
                    index = int(payload.get("index", -1))
                    if 0 <= index < len(self._cards):
                        if payload.get("mode") == "ytdlp_playlist":
                            self._handle_playlist_probe(index, payload)
                        else:
                            self._cards[index].apply_probe(payload)
                            if index < len(self._queue_items):
                                self._queue_items[index].update({"state": "ready", "title": payload.get("title")})
                    self.status_var.set(f"В очереди: {len(self._cards)}")
                elif kind == "queue_meta_error":
                    index = int(payload.get("index", -1))
                    if 0 <= index < len(self._cards):
                        self._cards[index].apply_error(str(payload.get("error") or "Ошибка"))
                elif kind == "thumb_ready":
                    index = int(payload.get("index", -1))
                    if 0 <= index < len(self._cards):
                        self._cards[index].set_thumbnail_path(payload["path"])
                elif kind == "card_event":
                    index = int(payload.get("index", -1))
                    event_kind = payload.get("kind")
                    data = payload.get("payload")
                    if not (0 <= index < len(self._cards)):
                        continue
                    card = self._cards[index]
                    self._active_index = index
                    if event_kind == "proc":
                        self.proc = data
                    elif event_kind == "log":
                        self._append_log(str(data))
                    elif event_kind == "status":
                        card.set_progress(card.percent, str(data))
                        self.status_var.set(str(data)[:120])
                    elif event_kind == "output":
                        self.last_output = str(data)
                        card.output = str(data)
                    elif event_kind == "suggested_name":
                        card.title_var.set(str(data).removesuffix(".mp4"))
                    elif event_kind == "thumbnail":
                        card.update_item({"thumbnail": data})
                    elif event_kind == "progress":
                        percent = data.get("percent") if isinstance(data, dict) else None
                        speed = ""
                        eta = ""
                        if isinstance(data, dict):
                            if data.get("time"):
                                self.progress_time = float(data["time"])
                            speed = str(data.get("speed") or "")
                            eta = str(data.get("eta") or "")
                            if speed:
                                self.progress_speed = speed
                        card.set_progress(
                            None if percent is None else float(percent),
                            self._status_for_active() if not speed else None,
                            speed=speed,
                            eta=eta,
                        )
                    elif event_kind == "size":
                        self.progress_size = int(data)
                        card.set_progress(card.percent, self._status_for_active())
                    elif event_kind == "speed":
                        self.progress_speed = str(data)
                        card.set_progress(card.percent, self._status_for_active(), speed=str(data))
                elif kind == "card_done":
                    index = int(payload.get("index", -1))
                    if 0 <= index < len(self._cards):
                        card = self._cards[index]
                        if payload.get("cancelled"):
                            card.state = "stop"
                            card.update_item({"state": "stop", "detail": "Остановлено", "output": payload.get("output")})
                            card.meta_var.set("Остановлено")
                            card._show_ready()
                        elif payload.get("ok"):
                            out = payload.get("output") or ""
                            self.last_output = out or self.last_output
                            if payload.get("live") or payload.get("stopped"):
                                detail = Path(out).name if out else "Запись сохранена"
                                status_detail = "Запись сохранена"
                            else:
                                detail = Path(out).name if out else "Готово"
                                status_detail = detail
                            if out:
                                check = verify_mp4_file(Path(out))
                                if check:
                                    self._append_log(f"Проверка файла: {check}")
                            card.update_item({"state": "ok", "detail": detail, "output": out, "percent": 100})
                            card._show_progress()
                            card.set_progress(100, status_detail)
                            card.meta_var.set(out or status_detail)
                            append_download_history(
                                {
                                    "title": card.title_var.get(),
                                    "url": card.url,
                                    "output": out,
                                    "ok": True,
                                    "live": bool(payload.get("live")),
                                    "when": datetime.now().isoformat(timespec="seconds"),
                                }
                            )
                            self.show_toast("Готово", card.title_var.get()[:80] or detail, ok=True)
                        else:
                            err = friendly_download_error(payload.get("error") or "Ошибка скачивания")
                            card.apply_error(err)
                            append_download_history(
                                {
                                    "title": card.title_var.get(),
                                    "url": card.url,
                                    "error": err[:200],
                                    "ok": False,
                                    "when": datetime.now().isoformat(timespec="seconds"),
                                }
                            )
                            self.show_toast("Ошибка", err[:100], ok=False)
                    busy = any(c.state == "run" for c in self._cards)
                    self._set_busy(busy)
                    if not busy:
                        self.status_var.set("Готово" if any(c.state == "ok" for c in self._cards) else "Очередь")
                elif kind == "proc":
                    self.proc = payload
                elif kind == "log":
                    self._append_log(payload)
                elif kind == "status":
                    self.status_var.set(payload)
                elif kind == "done":
                    self._finish(payload)
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _drain(self) -> None:
        while True:
            try:
                self.events.get_nowait()
            except queue.Empty:
                return

    def _finish(self, payload: dict) -> None:
        self._set_busy(False)
        self.proc = None
        summary = payload.get("queue_summary")
        if payload.get("cancelled"):
            self.status_var.set(summary or "Остановлено")
            path = payload.get("output") or ""
            if path and Path(path).exists() and Path(path).stat().st_size > 0:
                self.last_output = path
            self._append_log(self.status_var.get())
            return
        if payload.get("ok") or payload.get("partial_ok"):
            self.last_output = payload.get("output") or self.last_output
            if summary:
                self.status_var.set(summary)
            elif self.last_output and Path(self.last_output).exists():
                self.status_var.set(f"Готово · {fmt_size(Path(self.last_output).stat().st_size)} · {self.last_output}")
            else:
                self.status_var.set("Готово")
            self._append_log(self.status_var.get())
            return
        self.status_var.set(summary or payload.get("error") or "Ошибка скачивания")
        self._append_log(self.status_var.get())
        if payload.get("output"):
            self.last_output = payload["output"]

    def open_output(self) -> None:
        if self.last_output and Path(self.last_output).exists():
            open_path(self.last_output)
            return
        folder = self.dir_var.get().strip()
        if folder and Path(folder).exists():
            open_path(folder)

    def _persist(self) -> None:
        try:
            proxy = self._resolved_proxy()
        except PlaylistError:
            proxy = self.proxy_var.get().strip()
        remember = bool(self.remember_geometry_var.get())
        data = {
            "output_dir": self.dir_var.get().strip(),
            "user_agent": self.ua_var.get().strip(),
            "ua_preset": self.ua_preset_var.get(),
            "ua_spoof": bool(self.ua_spoof_var.get()),
            "cookies": self.cookies_var.get().strip(),
            "referer": self.referer_var.get().strip(),
            "proxy": proxy,
            "proxy_scheme": self.proxy_scheme_var.get(),
            "extra": self.extra_text.get("1.0", "end").strip(),
            "insecure": bool(self.insecure_var.get()),
            "threads": clamp_threads(self.threads_var.get()),
            "direct_threads": clamp_threads(self.direct_threads_var.get()),
            "theme": self.theme_name,
            "remember_geometry": remember,
            "auto_clipboard": bool(self.auto_clipboard_var.get()),
            "notify": bool(self.notify_var.get()),
        }
        if remember:
            data["geometry"] = self.geometry()
        save_settings(data)

    def _on_close(self) -> None:
        busy = (self.worker and self.worker.is_alive()) or any(c.state == "run" for c in self._cards)
        if busy:
            if not messagebox.askyesno("Выход", "Скачивание ещё идёт. Остановить и закрыть?"):
                return
            self.stop_download()
        try:
            self.canvas.unbind_all("<MouseWheel>")
        except tk.TclError:
            pass
        self._persist()
        self.destroy()



def _self_test() -> None:
    master = """#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aac",NAME="English",DEFAULT=YES,LANGUAGE="en",URI="audio/en.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aac",NAME="Russian",LANGUAGE="ru",URI="audio/ru.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360,CODECS="avc1.42e01e,mp4a.40.2",AUDIO="aac"
low/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080,CODECS="avc1.640028",AUDIO="aac"
hi/index.m3u8
"""
    parsed = parse_playlist(master, "https://cdn.example/live/master.m3u8")
    assert parsed.is_master and len(parsed.variants) == 2
    assert parsed.variants[0].resolution == "1920x1080"
    assert parsed.variants[0].url == "https://cdn.example/live/hi/index.m3u8"
    assert separate_audio(parsed.variants[0], parsed.audios, None) == "https://cdn.example/live/audio/en.m3u8"
    assert separate_audio(parsed.variants[1], parsed.audios, None) == "https://cdn.example/live/audio/en.m3u8"
    media = """#EXTM3U
#EXT-X-TARGETDURATION:10
#EXTINF:10.0,
https://cdn.example/a.ts
#EXTINF:9.5,
https://cdn.example/b.ts
#EXT-X-ENDLIST
"""
    vod = parse_playlist(media, "https://cdn.example/playlist.m3u8")
    assert vod.is_vod and abs((vod.duration or 0) - 19.5) < 0.01 and vod.segment_count == 2
    assert len(vod.segments) == 2 and vod.segments[0].url.endswith("/a.ts")
    assert is_image_url("https://cdn.example/a.image?x=1")
    assert not is_image_url("https://cdn.example/a.ts")
    packet = bytes([0x47]) + b"\x00" * 187
    wrapped = b"\x89PNG\r\n\x1a\nIEND" + b"\x00\x00\x00\x00" + packet
    assert extract_mpegts(wrapped) == packet
    assert extract_mpegts(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16) is None
    page = '''<html><script>var u="https:\\/\\/cdn.example\\/live\\/master.m3u8?token=1";</script>
<video src="https://cdn.example/other/chunklist.m3u8"></video></html>'''
    found = extract_m3u8_candidates(page, "https://site.example/watch/1")
    assert any("master.m3u8" in item for item in found)
    assert "master.m3u8" in pick_best_playlist(found)
    assert looks_like_playlist_url("https://x/y/master.m3u8?v=1")
    assert not looks_like_playlist_url("https://tiktok.com/@user/video/123")
    assert parse_url_list("https://a.com/1\n\nhttps://b.com/2\n# skip\nhttps://a.com/1") == [
        "https://a.com/1",
        "https://b.com/2",
    ]
    assert clamp_threads(0) == 1
    assert clamp_threads(100) == MAX_THREADS
    assert clamp_threads("24") == 24
    live = parse_playlist(media.replace("#EXT-X-ENDLIST", ""), "https://cdn.example/playlist.m3u8")
    assert not live.is_vod and live.duration is None
    signed = parse_playlist(master, "https://cdn.example/live/master.m3u8?token=abc")
    assert signed.variants[0].url == "https://cdn.example/live/hi/index.m3u8?token=abc"
    absolute = absolutize_playlist(
        '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key.key"\n#EXTINF:1,\nseg.ts\n#EXT-X-ENDLIST\n',
        "https://cdn.example/v/hi.m3u8?token=abc",
    )
    assert 'URI="https://cdn.example/v/key.key?token=abc"' in absolute
    assert "https://cdn.example/v/seg.ts?token=abc" in absolute
    assert name_from_url("https://host/channels/news/index.m3u8?token=1") == "news.mp4"
    assert sanitize_filename('a<>:"b') == "ab"
    try:
        parse_playlist("<html><body>login</body></html>", "https://cdn.example/x.m3u8")
        raise AssertionError("html should fail")
    except PlaylistError:
        pass
    resolved = Resolved("https://cdn.example/hi.m3u8", None, True, 19.5, "1080p")
    command = build_ffmpeg_command(Path("ffmpeg"), resolved, Path("out.mp4"), {"User-Agent": "test"}, "", False, None, True)
    assert command[command.index("-i") + 1] == "https://cdn.example/hi.m3u8"
    assert "-c" in command and "copy" in command
    print("self-test ok")


def main() -> None:
    if "--test" in sys.argv:
        _self_test()
        return
    try:
        from ctypes import windll

        windll.shcore.SetProcessDpiAwareness(1)
        windll.shell32.SetCurrentProcessExplicitAppUserModelID("hls.downloader")
    except (AttributeError, OSError):
        pass
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()

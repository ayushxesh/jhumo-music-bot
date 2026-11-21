import os
import time
import asyncio
import urllib.request
from collections import OrderedDict

import yt_dlp

import config

COOKIE_FILE = "/tmp/cookies.txt"
SOURCE_TIMEOUT = 40

_cache: "OrderedDict[str, tuple[float, dict]]" = OrderedDict()
_yt_last_block = 0.0
_yt_last_ok = 0.0
_last_alert = 0.0
_block_callback = None


# ---------------- cookies ----------------

def _init_cookies() -> str | None:
    content = os.environ.get("YOUTUBE_COOKIES", "").strip()
    if content:
        with open(COOKIE_FILE, "w", encoding="utf-8", newline="\n") as f:
            f.write(content.replace("\r\n", "\n") + "\n")
        return COOKIE_FILE
    if os.path.exists("cookies.txt"):
        return "cookies.txt"
    return None


ACTIVE_COOKIE_FILE = _init_cookies()


def set_cookies(content: str) -> None:
    """Owner ne Telegram se naya cookies.txt bheja: bina redeploy ke lagao."""
    global ACTIVE_COOKIE_FILE, _yt_last_block
    with open(COOKIE_FILE, "w", encoding="utf-8", newline="\n") as f:
        f.write(content.replace("\r\n", "\n").strip() + "\n")
    ACTIVE_COOKIE_FILE = COOKIE_FILE
    _yt_last_block = 0.0
    _cache.clear()


def set_block_callback(cb) -> None:
    global _block_callback
    _block_callback = cb


def youtube_is_blocked() -> bool:
    return _yt_last_block > _yt_last_ok


# ---------------- POT server ko jagaye rakhna (Render free sleep) ----------------

def _ping_pot() -> None:
    url = config.POT_URL.rstrip("/") + "/ping"
    with urllib.request.urlopen(url, timeout=20) as r:
        r.read()


async def keep_pot_alive():
    if not config.POT_URL:
        return
    while True:
        try:
            await asyncio.to_thread(_ping_pot)
        except Exception as e:
            print(f"POT ping failed: {str(e)[:100]}", flush=True)
        await asyncio.sleep(480)  # har 8 min


# ---------------- cache ----------------

def _cache_get(key: str) -> dict | None:
    hit = _cache.get(key)
    if not hit:
        return None
    if time.time() - hit[0] > config.CACHE_TTL:
        _cache.pop(key, None)
        return None
    _cache.move_to_end(key)
    return hit[1]


def _cache_put(key: str, value: dict) -> None:
    _cache[key] = (time.time(), value)
    _cache.move_to_end(key)
    while len(_cache) > config.CACHE_MAX:
        _cache.popitem(last=False)


# ---------------- stream url ----------------

def _pick_format(info: dict) -> tuple[str | None, dict | None]:
    """(url, http_headers) wapas deta hai."""
    top_headers = info.get("http_headers")

    if info.get("url") and info.get("acodec") not in (None, "none"):
        return info["url"], top_headers

    formats = info.get("formats") or []

    # 1) direct http(s) audio-only
    for fmt in reversed(formats):
        if (
            fmt.get("acodec") not in (None, "none")
            and fmt.get("vcodec") in (None, "none")
            and fmt.get("url")
            and str(fmt.get("protocol", "")) in ("http", "https")
        ):
            return fmt["url"], fmt.get("http_headers") or top_headers

    # 2) koi bhi audio (HLS bhi)
    for fmt in reversed(formats):
        if fmt.get("acodec") not in (None, "none") and fmt.get("url"):
            return fmt["url"], fmt.get("http_headers") or top_headers

    return None, None


# ---------------- debug: ffmpeg se URL padhke dekho ----------------

async def check_stream(url: str, headers: dict | None = None) -> str:
    """ffmpeg se 3 second ka audio padhta hai. Logs me 'check=OK' ya 'check=FAIL ...' aata hai."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
    if headers:
        hdr = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        cmd += ["-headers", hdr]
    cmd += ["-i", url, "-t", "3", "-vn", "-f", "null", "-"]

    started = time.time()
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout=25)
        except asyncio.TimeoutError:
            proc.kill()
            return "TIMEOUT (25s)"
        took = time.time() - started
        text = err.decode("utf-8", errors="ignore").strip().replace("\n", " | ")
        if proc.returncode == 0:
            return f"OK ({took:.1f}s)"
        return f"FAIL rc={proc.returncode}: {text[-300:]}"
    except Exception as e:
        return f"CHECK ERROR: {str(e)[:100]}"


# ---------------- YouTube ----------------

def _yt_sync(query: str) -> dict | None:
    debug = os.environ.get("YT_DEBUG") == "1"

    opts = {
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "noplaylist": True,
        "quiet": not debug,
        "verbose": debug,
        "no_warnings": True,
        "socket_timeout": 8,
        "source_address": "0.0.0.0",
        "skip_download": True,
    }
    if ACTIVE_COOKIE_FILE:
        opts["cookiefile"] = ACTIVE_COOKIE_FILE
    if config.YT_PROXY:
        opts["proxy"] = config.YT_PROXY

    ea = {}
    if config.POT_URL:
        ea["youtubepot-bgutilhttp"] = {"base_url": [config.POT_URL]}
    clients = os.environ.get("YT_CLIENTS", "").strip()
    if clients:
        ea["youtube"] = {
            "player_client": [c.strip() for c in clients.split(",") if c.strip()]
        }
    if ea:
        opts["extractor_args"] = ea

    target = query if query.startswith(("http://", "https://")) else f"ytsearch1:{query}"

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(target, download=False)

    if info and info.get("entries"):
        info = next((e for e in info["entries"] if e), None)
    if not info:
        return None

    stream_url, headers = _pick_format(info)
    if not stream_url:
        return None

    return {
        "title": info.get("title", "Unknown Track"),
        "link": info.get("webpage_url", target),
        "duration": info.get("duration", 0) or 0,
        "stream_url": stream_url,
        "headers": headers,
    }


async def _source_youtube(query: str) -> dict | None:
    global _yt_last_block, _yt_last_ok, _last_alert
    try:
        result = await asyncio.to_thread(_yt_sync, query)
        if result:
            _yt_last_ok = time.time()
        return result
    except Exception as e:
        msg = str(e).lower()
        if "sign in" in msg or "not a bot" in msg:
            _yt_last_block = time.time()
            print("⚠️ YouTube bot-check aaya", flush=True)
            if _block_callback and time.time() - _last_alert > 3600:
                _last_alert = time.time()
                asyncio.create_task(_block_callback())
        else:
            print(f"YouTube error: {str(e)[:150]}", flush=True)
        return None


# ---------------- public API ----------------

async def get_audio_stream(query: str) -> dict | None:
    key = query.strip().lower()
    cached = _cache_get(key)
    if cached:
        return dict(cached)

    try:
        result = await asyncio.wait_for(_source_youtube(query), timeout=SOURCE_TIMEOUT)
    except asyncio.TimeoutError:
        print("youtube: timeout", flush=True)
        return None

    if result and result.get("stream_url"):
        _cache_put(key, result)
        return dict(result)
    return None
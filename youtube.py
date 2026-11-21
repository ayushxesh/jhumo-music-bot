import os
import time
import asyncio
import yt_dlp

COOKIE_FILE = "/tmp/cookies.txt"
CACHE_TTL = 3600          # 1 ghanta
YT_COOLDOWN = 900         # YouTube block hone par 15 min skip
YT_ENABLED = os.environ.get("YT_ENABLED", "1") == "1"
YT_PROXY = os.environ.get("YT_PROXY", "").strip() or None

_cache: dict[str, tuple[float, dict]] = {}
_yt_blocked_until = 0.0


def _init_cookies() -> str | None:
    cookies_content = os.environ.get("YOUTUBE_COOKIES", "").strip()
    if cookies_content:
        clean = cookies_content.replace("\r\n", "\n")
        with open(COOKIE_FILE, "w", encoding="utf-8", newline="\n") as f:
            f.write(clean + "\n")
        return COOKIE_FILE
    if os.path.exists("cookies.txt"):
        return "cookies.txt"
    return None


ACTIVE_COOKIE_FILE = _init_cookies()


def _is_preview(fmt: dict) -> bool:
    text = f"{fmt.get('format_id', '')} {fmt.get('url', '')}".lower()
    return "preview" in text


def _pick_stream_url(info: dict) -> str | None:
    if (
        info.get("url")
        and info.get("acodec") not in (None, "none")
        and not _is_preview(info)
    ):
        return info["url"]

    formats = info.get("formats") or []

    # 1) direct http(s) audio
    for fmt in reversed(formats):
        if (
            fmt.get("acodec") not in (None, "none")
            and fmt.get("vcodec") in (None, "none")
            and fmt.get("url")
            and str(fmt.get("protocol", "")) in ("http", "https")
            and not _is_preview(fmt)
        ):
            return fmt["url"]

    # 2) koi bhi audio (HLS bhi chalega)
    for fmt in reversed(formats):
        if (
            fmt.get("acodec") not in (None, "none")
            and fmt.get("url")
            and not _is_preview(fmt)
        ):
            return fmt["url"]

    return None


def _result(info: dict, fallback_link: str, stream_url: str) -> dict:
    return {
        "title": info.get("title", "Unknown Track"),
        "link": info.get("webpage_url", fallback_link),
        "duration": info.get("duration", 0),
        "stream_url": stream_url,
    }


def _try_youtube(query: str) -> dict | None:
    opts = {
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 8,
        "source_address": "0.0.0.0",
        "skip_download": True,
    }
    if ACTIVE_COOKIE_FILE:
        opts["cookiefile"] = ACTIVE_COOKIE_FILE
    if YT_PROXY:
        opts["proxy"] = YT_PROXY

    target = query if query.startswith(("http://", "https://")) else f"ytsearch1:{query}"

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(target, download=False)

    if info and info.get("entries"):
        info = next((e for e in info["entries"] if e), None)
    if not info:
        return None

    stream_url = _pick_stream_url(info)
    return _result(info, target, stream_url) if stream_url else None


def _try_soundcloud(query: str) -> dict | None:
    opts = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 10,
        "source_address": "0.0.0.0",
        "skip_download": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"scsearch2:{query}", download=False)

    for entry in (info.get("entries") or []) if info else []:
        if not entry:
            continue
        stream_url = _pick_stream_url(entry)
        if stream_url:
            return _result(entry, "https://soundcloud.com", stream_url)
    return None


def _extract(query: str) -> dict | None:
    global _yt_blocked_until

    key = query.strip().lower()
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]

    is_url = query.startswith(("http://", "https://"))
    result = None

    if YT_ENABLED and time.time() >= _yt_blocked_until:
        try:
            result = _try_youtube(query)
        except Exception as e:
            msg = str(e).lower()
            if "sign in" in msg or "not a bot" in msg:
                _yt_blocked_until = time.time() + YT_COOLDOWN
                print("⚠️ YouTube bot-check: 15 min ke liye SoundCloud use hoga", flush=True)
            else:
                print(f"YouTube error: {e}", flush=True)

    if not result and not is_url:
        try:
            result = _try_soundcloud(query)
        except Exception as e:
            print(f"SoundCloud error: {e}", flush=True)

    if result:
        _cache[key] = (time.time(), result)
    return result


async def get_audio_stream(query: str) -> dict | None:
    return await asyncio.to_thread(_extract, query)
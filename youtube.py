import os
import asyncio
import yt_dlp

COOKIE_FILE = "/tmp/cookies.txt"


def _init_cookies() -> str | None:
    cookies_content = os.environ.get("YOUTUBE_COOKIES", "").strip()
    if cookies_content:
        clean_cookies = cookies_content.replace("\r\n", "\n")
        with open(COOKIE_FILE, "w", encoding="utf-8", newline="\n") as f:
            f.write(clean_cookies + "\n")
        return COOKIE_FILE
    if os.path.exists("cookies.txt"):
        return "cookies.txt"
    return None


ACTIVE_COOKIE_FILE = _init_cookies()


def _priority_score(entry: dict) -> int:
    title = (entry.get("title") or "").lower()
    channel = (entry.get("channel") or entry.get("uploader") or "").lower()
    if "- topic" in channel:
        return 0
    if any(word in title for word in ("audio", "lyrical", "lyrics")):
        return 1
    return 2


def _pick_stream_url(info: dict) -> str | None:
    if info.get("url") and info.get("acodec") != "none":
        return info["url"]

    formats = info.get("formats") or []
    # Prefer direct HTTP/HTTPS audio streams first for smooth PyTgCalls playback
    for fmt in reversed(formats):
        if (
            fmt.get("acodec") not in (None, "none")
            and fmt.get("vcodec") in (None, "none")
            and fmt.get("url")
            and str(fmt.get("protocol", "")).startswith("http")
            and "m3u8" not in str(fmt.get("protocol", ""))
        ):
            return fmt["url"]

    for fmt in reversed(formats):
        if fmt.get("acodec") not in (None, "none") and fmt.get("url"):
            return fmt["url"]

    return info.get("url")


def _try_youtube(query: str) -> dict | None:
    base_opts = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 8,
        "source_address": "0.0.0.0",
    }
    if ACTIVE_COOKIE_FILE:
        base_opts["cookiefile"] = ACTIVE_COOKIE_FILE

    if query.startswith(("http://", "https://")):
        candidates = [query]
    else:
        search_opts = {**base_opts, "extract_flat": "in_playlist"}
        with yt_dlp.YoutubeDL(search_opts) as ydl:
            search_info = ydl.extract_info(f"ytsearch3:{query}", download=False)
            entries = [e for e in (search_info.get("entries") or []) if e]
            entries.sort(key=_priority_score)
            candidates = [
                entry.get("url") or f"https://www.youtube.com/watch?v={entry['id']}"
                for entry in entries
                if entry.get("url") or entry.get("id")
            ]

    with yt_dlp.YoutubeDL(base_opts) as ydl:
        for url in candidates:
            try:
                info = ydl.extract_info(url, download=False)
                if not info:
                    continue
                stream_url = _pick_stream_url(info)
                if stream_url:
                    return {
                        "title": info.get("title", "Unknown Track"),
                        "link": info.get("webpage_url", url),
                        "duration": info.get("duration", 0),
                        "stream_url": stream_url,
                    }
            except Exception:
                continue
    return None


def _try_soundcloud(query: str) -> dict | None:
    sc_opts = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 10,
        "source_address": "0.0.0.0",
    }
    with yt_dlp.YoutubeDL(sc_opts) as ydl:
        info = ydl.extract_info(f"scsearch3:{query}", download=False)
        entries = [e for e in (info.get("entries") or []) if e] if info else []
        for entry in entries:
            stream_url = _pick_stream_url(entry)
            if stream_url:
                return {
                    "title": entry.get("title", "Unknown Track"),
                    "link": entry.get("webpage_url", "https://soundcloud.com"),
                    "duration": entry.get("duration", 0),
                    "stream_url": stream_url,
                }
    return None


def _extract(query: str) -> dict | None:
    # 1. Try YouTube first
    try:
        yt_result = _try_youtube(query)
        if yt_result:
            return yt_result
    except Exception:
        pass

    # 2. Automatic fallback to SoundCloud (works 24/7 on Render without cookies)
    if not query.startswith(("http://", "https://")):
        sc_result = _try_soundcloud(query)
        if sc_result:
            return sc_result

    return None


async def get_audio_stream(query: str) -> dict | None:
    return await asyncio.to_thread(_extract, query)
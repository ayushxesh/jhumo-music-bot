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


def _extract(query: str) -> dict | None:
    base_opts = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
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

            candidates = []
            for entry in entries:
                url = entry.get("url") or (
                    f"https://www.youtube.com/watch?v={entry['id']}"
                    if entry.get("id")
                    else None
                )
                if url:
                    candidates.append(url)

    if not candidates:
        return None

    last_error = None
    with yt_dlp.YoutubeDL(base_opts) as ydl:
        for url in candidates:
            try:
                info = ydl.extract_info(url, download=False)
                if not info:
                    continue

                stream_url = info.get("url")
                if not stream_url:
                    for fmt in reversed(info.get("formats", [])):
                        if fmt.get("acodec") != "none" and fmt.get("url"):
                            stream_url = fmt["url"]
                            break

                if stream_url:
                    return {
                        "title": info.get("title", "Unknown Track"),
                        "link": info.get("webpage_url", url),
                        "duration": info.get("duration", 0),
                        "stream_url": stream_url,
                    }
            except Exception as e:
                last_error = e
                continue

    if last_error:
        raise last_error
    return None


async def get_audio_stream(query: str) -> dict | None:
    return await asyncio.to_thread(_extract, query)
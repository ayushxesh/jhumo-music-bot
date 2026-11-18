import asyncio
import threading
import yt_dlp

_YDL_LOCK = threading.Lock()

YDL_OPTS = {
    "format": "bestaudio[abr>=128]/bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch1",
    "source_address": "0.0.0.0",
    "nocheckcertificate": True,
    "geo_bypass": True,
    "cachedir": False,
}

_YDL = yt_dlp.YoutubeDL(YDL_OPTS)


def _extract_sync(query: str) -> dict | None:
    with _YDL_LOCK:
        info = _YDL.extract_info(query, download=False)

    if not info:
        return None
    if "entries" in info:
        entries = list(info["entries"])
        if not entries:
            return None
        info = entries[0]

    stream_url = info.get("url")
    if not stream_url and info.get("requested_formats"):
        stream_url = info["requested_formats"][0].get("url")

    if not stream_url:
        return None

    return {
        "title": info.get("title", "Unknown Song"),
        "stream_url": stream_url,
        "duration": info.get("duration", 0),
        "link": info.get("webpage_url", ""),
    }


async def get_audio_stream(query: str) -> dict | None:
    return await asyncio.to_thread(_extract_sync, query)
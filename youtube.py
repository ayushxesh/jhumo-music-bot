import asyncio
import yt_dlp

def _extract(query: str) -> dict | None:
    ydl_opts = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "source_address": "0.0.0.0",
        "extractor_args": {
            "youtube": {
                "player_client": ["ios", "android"],
            }
        },
    }

    search_query = query if query.startswith(("http://", "https://")) else f"ytsearch1:{query}"
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(search_query, download=False)
        if not info:
            return None
        if "entries" in info:
            entries = [e for e in info["entries"] if e]
            if not entries:
                return None
            info = entries[0]

        stream_url = info.get("url")
        if not stream_url:
            for fmt in reversed(info.get("formats", [])):
                if fmt.get("acodec") != "none" and fmt.get("url"):
                    stream_url = fmt["url"]
                    break

        if not stream_url:
            return None

        return {
            "title": info.get("title", "Unknown Track"),
            "link": info.get("webpage_url", f"https://www.youtube.com/watch?v={info.get('id', '')}"),
            "duration": info.get("duration", 0),
            "stream_url": stream_url,
        }

async def get_audio_stream(query: str) -> dict | None:
    return await asyncio.to_thread(_extract, query)
import os
import asyncio
import threading
from collections import deque, defaultdict
from http.server import HTTPServer, BaseHTTPRequestHandler

from pyrogram import Client, filters
from pyrogram.enums import ChatMemberStatus
from pyrogram.errors import UserNotParticipant
from pyrogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)
from pytgcalls import PyTgCalls, filters as call_filters
from pytgcalls.types import MediaStream, AudioQuality, Update

try:
    from pytgcalls.types import ChatUpdate
except ImportError:
    ChatUpdate = None

import config
from sources import (
    get_audio_stream,
    set_cookies,
    set_block_callback,
    keep_pot_alive,
    youtube_is_blocked,
    check_stream,
)

queues: dict[int, deque] = {}
locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
verified_chats: set[int] = set()

DEBUG = os.environ.get("YT_DEBUG") == "1"
FFMPEG_PARAMS = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
AUDIO_QUALITY = getattr(
    AudioQuality,
    os.environ.get("AUDIO_QUALITY", "HIGH").strip().upper(),
    AudioQuality.HIGH,
)


# ---------------- health server (Render ke liye) ----------------

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Jhumo Music Bot is Live!")

    def log_message(self, format, *args):
        pass


def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    print(f"🌐 Health server listening on port {port}", flush=True)
    server.serve_forever()


# ---------------- utils ----------------

def format_duration(seconds: int) -> str:
    if not seconds:
        return "Live / Unknown"
    mins, secs = divmod(int(seconds), 60)
    hours, mins = divmod(mins, 60)
    if hours:
        return f"{hours:02d}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


def control_buttons():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⏸ Pause", callback_data="pause"),
                InlineKeyboardButton("▶️ Resume", callback_data="resume"),
            ],
            [
                InlineKeyboardButton("⏭ Skip", callback_data="skip"),
                InlineKeyboardButton("⏹ Stop", callback_data="stop"),
            ],
        ]
    )


def build_stream(song: dict) -> MediaStream:
    kwargs = dict(
        audio_parameters=AUDIO_QUALITY,
        video_flags=MediaStream.Flags.IGNORE,
        ffmpeg_parameters=FFMPEG_PARAMS,
    )
    headers = song.get("headers")
    if headers:
        try:
            return MediaStream(song["stream_url"], headers=headers, **kwargs)
        except TypeError:
            print("⚠️ Is pytgcalls version me headers option nahi hai", flush=True)
    return MediaStream(song["stream_url"], **kwargs)


async def main():
    threading.Thread(target=start_health_server, daemon=True).start()

    bot = Client(
        "JhumoBot",
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        bot_token=config.BOT_TOKEN,
        in_memory=True,
    )

    assistant = Client(
        "JhumoAssistant",
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        session_string=config.STRING_SESSION,
        in_memory=True,
    )

    call_py = PyTgCalls(assistant)

    # ---------- helpers ----------

    async def ensure_assistant(chat_id: int):
        if chat_id in verified_chats:
            return
        try:
            await assistant.get_chat_member(chat_id, "me")
        except UserNotParticipant:
            invite_link = await bot.export_chat_invite_link(chat_id)
            await assistant.join_chat(invite_link)
        verified_chats.add(chat_id)

    async def can_control(chat_id: int, user_id: int | None) -> bool:
        # anonymous admin (from_user None) ko allow karo
        if user_id is None:
            return True
        if user_id in config.OWNER_IDS:
            return True
        try:
            member = await bot.get_chat_member(chat_id, user_id)
            if member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
                return True
        except Exception:
            pass
        queue = queues.get(chat_id)
        return bool(queue and queue[0].get("requester_id") == user_id)

    async def send_now_playing(
        chat_id: int, song: dict, status_task: asyncio.Task | None = None
    ):
        duration_str = format_duration(song.get("duration", 0))
        text = (
            f"🎵 **Now Playing**\n\n"
            f"**Title:** [{song['title']}]({song['link']})\n"
            f"**Duration:** `{duration_str}`\n"
            f"**Requested by:** {song['requester']}"
        )
        try:
            if status_task:
                status = await status_task
                await status.edit_text(
                    text,
                    reply_markup=control_buttons(),
                    disable_web_page_preview=True,
                )
            else:
                await bot.send_message(
                    chat_id,
                    text,
                    reply_markup=control_buttons(),
                    disable_web_page_preview=True,
                )
        except Exception as e:
            print(f"Now-playing message error: {e}", flush=True)

    async def start_stream(
        chat_id: int, song: dict, status_task: asyncio.Task | None = None
    ):
        check = "skipped"
        if DEBUG:
            check = await check_stream(song["stream_url"], song.get("headers"))
        print(
            f"▶️ STREAM: {song['title']} | check={check} | {song['stream_url'][:80]}",
            flush=True,
        )
        await call_py.play(chat_id, build_stream(song))
        asyncio.create_task(send_now_playing(chat_id, song, status_task))

    async def play_next(chat_id: int):
        async with locks[chat_id]:
            queue = queues.get(chat_id)
            if queue:
                queue.popleft()

            while queue:
                try:
                    await start_stream(chat_id, queue[0])
                    return
                except Exception as e:
                    print(f"Next track failed, skipping: {e}", flush=True)
                    skipped = queue.popleft()
                    try:
                        await bot.send_message(
                            chat_id,
                            f"⚠️ Could not play **{skipped['title']}**, skipping.",
                        )
                    except Exception:
                        pass

            queues.pop(chat_id, None)
            try:
                await call_py.leave_call(chat_id)
            except Exception:
                pass

    # ---------- call events ----------

    @call_py.on_update(call_filters.stream_end())
    async def on_stream_end(_, update: Update):
        await play_next(update.chat_id)

    if ChatUpdate is not None:
        try:

            @call_py.on_update(
                call_filters.chat_update(
                    ChatUpdate.Status.CLOSED_VOICE_CHAT
                    | ChatUpdate.Status.KICKED
                    | ChatUpdate.Status.LEFT_CALL
                )
            )
            async def on_chat_update(_, update: Update):
                queues.pop(update.chat_id, None)

        except Exception as e:
            print(f"⚠️ chat_update handler register nahi hua: {e}", flush=True)

    # ---------- commands ----------

    @bot.on_message(filters.command(["start", "help"]))
    async def start_cmd(_, message: Message):
        await message.reply_text(
            "🎶 **Welcome to Jhumo Music Bot**\n\n"
            "Stream music into your group's Voice Chat.\n\n"
            "**Setup:** Add me to your group, make me **Admin** "
            "(with *Invite Users via Link*), start a **Voice Chat**, then use /play.\n\n"
            "**Commands:**\n"
            "• `/play <song name>` — Play or queue a track\n"
            "• `/pause` / `/resume` — Pause or resume\n"
            "• `/skip` — Next track\n"
            "• `/stop` — Stop and leave Voice Chat\n"
            "• `/queue` — Show the queue\n"
            "• `/ping` — Check if I'm alive\n\n"
            "_Pause, skip and stop can be used by group admins or the person who requested the current track._"
        )

    @bot.on_message(filters.command("ping"))
    async def ping_cmd(_, message: Message):
        await message.reply_text("🏓 Pong! Bot is alive.")

    @bot.on_message(filters.command("play") & filters.group)
    async def play_cmd(_, message: Message):
        if len(message.command) < 2:
            return await message.reply_text(
                "⚠️ **Usage:** `/play <song name>`\n**Example:** `/play Kesariya`"
            )

        query = message.text.split(None, 1)[1].strip()
        if len(query) > 200:
            return await message.reply_text("⚠️ Query too long (max 200 characters).")

        chat_id = message.chat.id
        user_id = message.from_user.id if message.from_user else None
        requester = message.from_user.mention if message.from_user else "Anonymous"

        status_task = asyncio.create_task(message.reply_text("🔎 **Searching...**"))
        assistant_task = asyncio.create_task(ensure_assistant(chat_id))
        song_task = asyncio.create_task(get_audio_stream(query))

        try:
            await assistant_task
        except Exception as e:
            song_task.cancel()
            print(f"Assistant join failed in {chat_id}: {e}", flush=True)
            status = await status_task
            return await status.edit_text(
                "❌ **Assistant Join Failed**\n"
                "Please make me **Admin** with the *Invite Users via Link* permission, "
                "then try again."
            )

        try:
            song = await asyncio.wait_for(song_task, timeout=60)
        except asyncio.TimeoutError:
            status = await status_task
            return await status.edit_text(
                "⏳ **Search Timed Out**\nPlease try again in a moment."
            )
        except Exception as e:
            print(f"Search error: {e}", flush=True)
            status = await status_task
            return await status.edit_text(
                "❌ **Search failed.** Please try again later."
            )

        if not song or not song.get("stream_url"):
            status = await status_task
            if youtube_is_blocked():
                return await status.edit_text(
                    "⚠️ **YouTube is temporarily blocking requests.**\n"
                    "The owner has been notified. Please try again later."
                )
            return await status.edit_text(
                "❌ **No Results Found**\nTry a different title or add the artist name."
            )

        max_sec = config.MAX_DURATION_MIN * 60
        if max_sec and song.get("duration") and song["duration"] > max_sec:
            status = await status_task
            return await status.edit_text(
                f"⚠️ Track is too long. Max allowed: {config.MAX_DURATION_MIN} minutes."
            )

        song["requester"] = requester
        song["requester_id"] = user_id

        async with locks[chat_id]:
            queue = queues.setdefault(chat_id, deque())

            if len(queue) >= config.MAX_QUEUE:
                status = await status_task
                return await status.edit_text(
                    f"⚠️ Queue is full (max {config.MAX_QUEUE} tracks)."
                )

            queue.append(song)
            position = len(queue) - 1

            if position == 0:
                try:
                    await start_stream(chat_id, song, status_task=status_task)
                except Exception as e:
                    print(f"Playback error in {chat_id}: {e}", flush=True)
                    queues.pop(chat_id, None)
                    status = await status_task
                    await status.edit_text(
                        "❌ **Playback Error**\n"
                        "Please make sure a **Voice Chat** is active in this group, "
                        "then try again."
                    )
            else:
                status = await status_task
                await status.edit_text(
                    f"➕ **Added to Queue (#{position})**\n"
                    f"**Track:** {song['title']}\n"
                    f"**Requested by:** {requester}"
                )

    DENIED = "🚫 Only group admins or the person who requested the current track can do this."

    @bot.on_message(filters.command("pause") & filters.group)
    async def pause_cmd(_, message: Message):
        uid = message.from_user.id if message.from_user else None
        if not await can_control(message.chat.id, uid):
            return await message.reply_text(DENIED)
        try:
            await call_py.pause(message.chat.id)
            await message.reply_text("⏸ **Playback paused.**")
        except Exception:
            await message.reply_text("❌ Nothing is playing right now.")

    @bot.on_message(filters.command("resume") & filters.group)
    async def resume_cmd(_, message: Message):
        uid = message.from_user.id if message.from_user else None
        if not await can_control(message.chat.id, uid):
            return await message.reply_text(DENIED)
        try:
            await call_py.resume(message.chat.id)
            await message.reply_text("▶️ **Playback resumed.**")
        except Exception:
            await message.reply_text("❌ Nothing to resume.")

    @bot.on_message(filters.command("skip") & filters.group)
    async def skip_cmd(_, message: Message):
        chat_id = message.chat.id
        uid = message.from_user.id if message.from_user else None
        if not queues.get(chat_id):
            return await message.reply_text("📭 Nothing is playing.")
        if not await can_control(chat_id, uid):
            return await message.reply_text(DENIED)
        await message.reply_text("⏭ **Skipped to the next track.**")
        await play_next(chat_id)

    @bot.on_message(filters.command("stop") & filters.group)
    async def stop_cmd(_, message: Message):
        chat_id = message.chat.id
        uid = message.from_user.id if message.from_user else None
        if not await can_control(chat_id, uid):
            return await message.reply_text(DENIED)
        async with locks[chat_id]:
            queues.pop(chat_id, None)
        try:
            await call_py.leave_call(chat_id)
        except Exception:
            pass
        await message.reply_text("⏹ **Playback stopped and disconnected from Voice Chat.**")

    @bot.on_message(filters.command("queue") & filters.group)
    async def queue_cmd(_, message: Message):
        queue = queues.get(message.chat.id)
        if not queue:
            return await message.reply_text("📭 **The queue is empty.**")
        text = "📜 **Current Queue**\n\n"
        for i, item in enumerate(queue):
            prefix = "▶️ **Now:**" if i == 0 else f"**{i}.**"
            text += f"{prefix} {item['title']}\n"
        await message.reply_text(text)

    # ---------- buttons ----------

    @bot.on_callback_query()
    async def button_handler(_, query: CallbackQuery):
        chat_id = query.message.chat.id
        data = query.data
        user_id = query.from_user.id if query.from_user else None

        if not await can_control(chat_id, user_id):
            return await query.answer(
                "Only admins or the requester can use these buttons.", show_alert=True
            )
        try:
            if data == "pause":
                await call_py.pause(chat_id)
                await query.answer("⏸ Playback paused")
            elif data == "resume":
                await call_py.resume(chat_id)
                await query.answer("▶️ Playback resumed")
            elif data == "skip":
                await query.answer("⏭ Skipping track...")
                await play_next(chat_id)
            elif data == "stop":
                async with locks[chat_id]:
                    queues.pop(chat_id, None)
                try:
                    await call_py.leave_call(chat_id)
                except Exception:
                    pass
                await query.answer("⏹ Playback stopped")
                await query.message.edit_text("⏹ **Playback stopped.**")
        except Exception as e:
            print(f"Button error: {e}", flush=True)
            await query.answer("Something went wrong. Is a track playing?", show_alert=True)

    # ---------- owner: alerts + cookies upload ----------

    async def notify_owner_block():
        for oid in config.OWNER_IDS:
            try:
                await bot.send_message(
                    oid,
                    "⚠️ YouTube is blocking the bot (bot-check).\n"
                    "Send me a fresh `cookies.txt` file here, or check the POT provider.",
                )
            except Exception as e:
                print(f"Owner alert failed: {e}", flush=True)

    set_block_callback(notify_owner_block)

    @bot.on_message(filters.private & filters.command("cookies"))
    async def cookies_help(_, message: Message):
        if not message.from_user or message.from_user.id not in config.OWNER_IDS:
            return
        await message.reply_text(
            "Send your YouTube `cookies.txt` as a **file** in this chat. "
            "I'll load it immediately, no redeploy needed."
        )

    @bot.on_message(filters.private & filters.document)
    async def cookies_upload(_, message: Message):
        if not message.from_user or message.from_user.id not in config.OWNER_IDS:
            return
        doc = message.document
        if not (doc.file_name or "").lower().endswith(".txt"):
            return await message.reply_text("⚠️ Please send a `.txt` cookies file.")
        if doc.file_size and doc.file_size > 2_000_000:
            return await message.reply_text("⚠️ File too large.")
        buf = await message.download(in_memory=True)
        text = buf.getvalue().decode("utf-8", errors="ignore")
        if "youtube.com" not in text:
            return await message.reply_text(
                "⚠️ This doesn't look like a YouTube cookies file."
            )
        set_cookies(text)
        await message.reply_text("✅ Cookies updated. Try /play now.")
        try:
            await message.delete()  # cookies chat me na rahein
        except Exception:
            pass

    # ---------- start ----------

    await bot.start()
    me = await bot.get_me()
    await assistant.start()
    await call_py.start()
    print(
        f"✅ @{me.username} is LIVE! | debug={DEBUG} | audio={AUDIO_QUALITY}",
        flush=True,
    )
    asyncio.create_task(keep_pot_alive())
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
import os
import asyncio
import threading
from collections import deque
from http.server import HTTPServer, BaseHTTPRequestHandler

from pyrogram import Client, filters
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
except ImportError:  # v3 me naam alag ho to bot crash na ho
    ChatUpdate = None

import config
from youtube import get_audio_stream

queues: dict[int, deque] = {}
verified_chats: set[int] = set()

FFMPEG_PARAMS = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"


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
        print(
            f"▶️ STREAM: {song['title']} | {song['stream_url'][:100]}",
            flush=True,
        )
        await call_py.play(
            chat_id,
            MediaStream(
                song["stream_url"],
                audio_parameters=AudioQuality.HIGH,
                video_flags=MediaStream.Flags.IGNORE,
                ffmpeg_parameters=FFMPEG_PARAMS,
            ),
        )
        asyncio.create_task(send_now_playing(chat_id, song, status_task))

    async def play_next(chat_id: int):
        queue = queues.get(chat_id)
        if queue:
            queue.popleft()

        # Agla song fail ho jaye to usse skip karke uske baad wala try karo
        while queue and len(queue) > 0:
            try:
                await start_stream(chat_id, queue[0])
                return
            except Exception as e:
                print(f"Next track failed, skipping: {e}", flush=True)
                queue.popleft()

        queues.pop(chat_id, None)
        try:
            await call_py.leave_call(chat_id)
        except Exception:
            pass

    # ---------- call events ----------

    @call_py.on_update(call_filters.stream_end())
    async def on_stream_end(_, update: Update):
        await play_next(update.chat_id)

    # VC band/kick hone par purani queue saaf karo
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
            "Stream high-quality audio directly into your group's Voice Chat "
            "with minimal latency.\n\n"
            "**Available Commands:**\n"
            "• `/play <song name or lyrics>` — Play a track or add it to the queue\n"
            "• `/pause` — Pause the current stream\n"
            "• `/resume` — Resume playback\n"
            "• `/skip` — Skip to the next track\n"
            "• `/stop` — Stop playback and leave Voice Chat\n"
            "• `/queue` — View the upcoming playlist"
        )

    @bot.on_message(filters.command("play") & filters.group)
    async def play_cmd(_, message: Message):
        if len(message.command) < 2:
            return await message.reply_text(
                "⚠️ **Usage:** Please provide a song name or lyrics.\n"
                "**Example:** `/play Kesariya`"
            )

        query = message.text.split(None, 1)[1]
        chat_id = message.chat.id
        requester = message.from_user.mention if message.from_user else "User"

        status_task = asyncio.create_task(message.reply_text("🔎 **Searching...**"))
        assistant_task = asyncio.create_task(ensure_assistant(chat_id))
        song_task = asyncio.create_task(get_audio_stream(query))

        try:
            await assistant_task
        except Exception as e:
            song_task.cancel()
            status = await status_task
            return await status.edit_text(
                "❌ **Assistant Join Failed**\n"
                "Please promote the bot to **Admin** with the "
                "*Invite Users via Link* permission.\n\n"
                f"**Details:** `{e}`"
            )

        try:
            song = await asyncio.wait_for(song_task, timeout=45)
        except asyncio.TimeoutError:
            status = await status_task
            return await status.edit_text(
                "⏳ **Search Timed Out**\n"
                "The music source took too long to respond. Please try again."
            )
        except Exception as e:
            status = await status_task
            return await status.edit_text(
                "❌ **Search Error**\n" f"**Details:** `{e}`"
            )

        if not song or not song.get("stream_url"):
            status = await status_task
            return await status.edit_text(
                "❌ **No Results Found**\n"
                "Could not find a matching track. Please try a different title or lyric."
            )

        song["requester"] = requester

        if chat_id not in queues:
            queues[chat_id] = deque()

        queues[chat_id].append(song)

        if len(queues[chat_id]) == 1:
            try:
                await start_stream(chat_id, song, status_task=status_task)
            except Exception as e:
                queues.pop(chat_id, None)
                status = await status_task
                await status.edit_text(
                    "❌ **Playback Error**\n"
                    "Please ensure a **Voice Chat** is active in the group first.\n\n"
                    f"**Details:** `{e}`"
                )
        else:
            status = await status_task
            await status.edit_text(
                f"➕ **Added to Queue (#{len(queues[chat_id]) - 1})**\n"
                f"**Track:** {song['title']}\n"
                f"**Requested by:** {requester}"
            )

    @bot.on_message(filters.command("pause") & filters.group)
    async def pause_cmd(_, message: Message):
        try:
            await call_py.pause(message.chat.id)
            await message.reply_text("⏸ **Playback paused.**")
        except Exception as e:
            await message.reply_text(f"❌ **Nothing to pause.**\n`{e}`")

    @bot.on_message(filters.command("resume") & filters.group)
    async def resume_cmd(_, message: Message):
        try:
            await call_py.resume(message.chat.id)
            await message.reply_text("▶️ **Playback resumed.**")
        except Exception as e:
            await message.reply_text(f"❌ **Nothing to resume.**\n`{e}`")

    @bot.on_message(filters.command("skip") & filters.group)
    async def skip_cmd(_, message: Message):
        if not queues.get(message.chat.id):
            return await message.reply_text("📭 **Nothing is playing.**")
        await message.reply_text("⏭ **Skipped to the next track.**")
        await play_next(message.chat.id)

    @bot.on_message(filters.command("stop") & filters.group)
    async def stop_cmd(_, message: Message):
        chat_id = message.chat.id
        queues.pop(chat_id, None)
        try:
            await call_py.leave_call(chat_id)
        except Exception:
            pass
        await message.reply_text(
            "⏹ **Playback stopped and disconnected from Voice Chat.**"
        )

    @bot.on_message(filters.command("queue") & filters.group)
    async def queue_cmd(_, message: Message):
        queue = queues.get(message.chat.id)
        if not queue:
            return await message.reply_text(
                "📭 **The playback queue is currently empty.**"
            )
        text = "📜 **Current Playback Queue**\n\n"
        for i, item in enumerate(queue):
            prefix = "▶️ **Now:**" if i == 0 else f"**{i}.**"
            text += f"{prefix} {item['title']}\n"
        await message.reply_text(text)

    # ---------- buttons ----------

    @bot.on_callback_query()
    async def button_handler(_, query: CallbackQuery):
        chat_id = query.message.chat.id
        data = query.data
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
                queues.pop(chat_id, None)
                try:
                    await call_py.leave_call(chat_id)
                except Exception:
                    pass
                await query.answer("⏹ Playback stopped")
                await query.message.edit_text("⏹ **Playback stopped by user.**")
        except Exception as e:
            await query.answer(f"Error: {e}", show_alert=True)

    # ---------- start ----------

    await bot.start()
    me = await bot.get_me()
    await assistant.start()
    await call_py.start()
    print(f"✅ @{me.username} is LIVE!", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
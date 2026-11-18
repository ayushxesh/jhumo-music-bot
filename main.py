import asyncio
from collections import deque
from pyrogram import Client, filters, idle
from pyrogram.errors import UserNotParticipant
from pyrogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)
from pytgcalls import PyTgCalls, filters as call_filters
from pytgcalls.types import MediaStream, AudioQuality, Update

import config
from youtube import get_audio_stream

queues: dict[int, deque] = {}
verified_chats: set[int] = set()


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
    bot = Client(
        "JhumoBot",
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        bot_token=config.BOT_TOKEN,
    )

    assistant = Client(
        "JhumoAssistant",
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        session_string=config.STRING_SESSION,
    )

    call_py = PyTgCalls(assistant)

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

    async def start_stream(
        chat_id: int, song: dict, status_task: asyncio.Task | None = None
    ):
        await call_py.play(
            chat_id,
            MediaStream(
                song["stream_url"],
                audio_parameters=AudioQuality.HIGH,
                video_flags=MediaStream.Flags.IGNORE,
                ffmpeg_parameters="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
            ),
        )
        asyncio.create_task(send_now_playing(chat_id, song, status_task))

    async def play_next(chat_id: int):
        queue = queues.get(chat_id)
        if queue:
            queue.popleft()
        if queue and len(queue) > 0:
            await start_stream(chat_id, queue[0])
        else:
            queues.pop(chat_id, None)
            try:
                await call_py.leave_call(chat_id)
            except Exception:
                pass

    @call_py.on_update(call_filters.stream_end())
    async def on_stream_end(_, update: Update):
        await play_next(update.chat_id)

    @bot.on_message(filters.command(["start", "help"]))
    async def start_cmd(_, message: Message):
        await message.reply_text(
            "🎶 **Welcome to Jhumo Music Bot**\n\n"
            "Stream high-quality audio directly from YouTube into your group's Voice Chat.\n\n"
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
                "Please promote the bot to **Admin** with the *Invite Users via Link* permission.\n\n"
                f"**Details:** `{e}`"
            )

        song = await song_task
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
        await call_py.pause(message.chat.id)
        await message.reply_text("⏸ **Playback paused.**")

    @bot.on_message(filters.command("resume") & filters.group)
    async def resume_cmd(_, message: Message):
        await call_py.resume(message.chat.id)
        await message.reply_text("▶️ **Playback resumed.**")

    @bot.on_message(filters.command("skip") & filters.group)
    async def skip_cmd(_, message: Message):
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
        await message.reply_text("⏹ **Playback stopped and disconnected from Voice Chat.**")

    @bot.on_message(filters.command("queue") & filters.group)
    async def queue_cmd(_, message: Message):
        queue = queues.get(message.chat.id)
        if not queue:
            return await message.reply_text("📭 **The playback queue is currently empty.**")
        text = "📜 **Current Playback Queue**\n\n"
        for i, item in enumerate(queue):
            prefix = "▶️ **Now:**" if i == 0 else f"**{i}.**"
            text += f"{prefix} {item['title']}\n"
        await message.reply_text(text)

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
                await call_py.leave_call(chat_id)
                await query.answer("⏹ Playback stopped")
                await query.message.edit_text("⏹ **Playback stopped by user.**")
        except Exception as e:
            await query.answer(f"Error: {e}", show_alert=True)

    await bot.start()
    me = await bot.get_me()
    await assistant.start()
    await call_py.start()
    print(f"✅ @{me.username} is LIVE!")
    await idle()


if __name__ == "__main__":
    asyncio.run(main())
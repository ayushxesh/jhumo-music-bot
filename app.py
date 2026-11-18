import asyncio
import threading
import gradio as gr
from main import main

_bot_started = False


def start_bot_once():
    global _bot_started
    if not _bot_started:
        _bot_started = True
        threading.Thread(target=lambda: asyncio.run(main()), daemon=True).start()


start_bot_once()

with gr.Blocks(title="Jhumo Music Bot") as demo:
    gr.Markdown("## 🎶 Jhumo Music Bot is Live!\nStreaming audio in Telegram Group Voice Chats.")

if __name__ == "__main__":
    demo.launch(server_name="0.0.0.0", server_port=7860)
import asyncio
from pyrogram import Client

async def main():
    api_id = int(input("Enter API_ID: "))
    api_hash = input("Enter API_HASH: ")
    async with Client("assistant", api_id=api_id, api_hash=api_hash, in_memory=True) as app:
        session = await app.export_session_string()
        print("\n=== COPY THIS STRING_SESSION ===\n")
        print(session)
        print("\n================================\n")
        await app.send_message("me", f"**Your STRING_SESSION:**\n\n`{session}`")

asyncio.run(main())
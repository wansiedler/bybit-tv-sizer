"""Print the chat ids your bot can post to.

Send /start (or any message) to @your_bot first, then run this.
"""

import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    sys.exit("Missing BOT_TOKEN in .env")

response = httpx.get(
    f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates", timeout=15
).json()

if not response.get("ok"):
    sys.exit(f"Telegram API error: {response}")

seen = {}
for update in response["result"]:
    message = update.get("message") or update.get("channel_post")
    if not message:
        continue
    chat = message["chat"]
    title = chat.get("title") or " ".join(
        filter(None, [chat.get("first_name"), chat.get("last_name")])
    )
    seen[chat["id"]] = f"{chat['type']}: {title or chat.get('username', '')}"

if not seen:
    print("No updates yet. Send /start to your bot, then run this again.")
    print("(A channel needs the bot as admin and one post after that.)")
else:
    print("TARGET_CHAT_ID candidates:")
    for chat_id, label in seen.items():
        print(f"  {chat_id}\t{label}")

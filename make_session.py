from telethon.sync import TelegramClient
from telethon.sessions import StringSession
import os
from dotenv import load_dotenv

load_dotenv()

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")

with TelegramClient(StringSession(), API_ID, API_HASH) as client:
    print("\n===== СКОПИРУЙ ЭТУ СТРОКУ =====\n")
    print(client.session.save())
    print("\n===== КОНЕЦ =====\n")
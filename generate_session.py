"""
User account အတွက် SESSION_STRING ထုတ်တဲ့ script.
Run:  python generate_session.py

API_ID / API_HASH ကို env var, .env file, ဒါမှမဟုတ် prompt ကနေ ယူမယ်.
SESSION_STRING ကို .env ထဲ (SESSION_STRING=...) သိမ်းမယ်,
session_string.txt မှာလည်း backup အဖြစ် သိမ်းမယ်.
"""
import asyncio
import os

from dotenv import load_dotenv, set_key

load_dotenv()

from pyrogram import Client  # noqa: E402


def get_cred(name):
    val = os.environ.get(name, "").strip()
    if val:
        return val
    return input(f"{name}: ").strip()


async def main():
    api_id = int(get_cred("API_ID"))
    api_hash = get_cred("API_HASH")

    async with Client("tmp_session", api_id=api_id, api_hash=api_hash) as app:
        s = await app.export_session_string()

    # .env ထဲ သိမ်း (မရှိရင် အသစ်ဆောက်)
    env_path = ".env"
    if not os.path.exists(env_path):
        with open(env_path, "w") as f:
            f.write(f"API_ID={api_id}\nAPI_HASH={api_hash}\nBOT_TOKEN=\n"
                    f"SESSION_STRING=\nALLOWED_USER_IDS=\n")
    set_key(env_path, "SESSION_STRING", s)

    # backup
    with open("session_string.txt", "w") as f:
        f.write(s)

    print("\n✅ SESSION_STRING ကို .env ထဲ သိမ်းပြီးပါပြီ")
    print("⚠️ .env / session_string.txt ကို ဘယ်သူနဲ့မှ မမျှပါနဲ့ — သင့် account ရဲ့ key ပါ။")


asyncio.run(main())

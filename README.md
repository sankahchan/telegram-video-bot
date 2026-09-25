# 📥 Telegram Restricted Media Downloader Bot

![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)
![VPS 24/7](https://img.shields.io/badge/VPS-24%2F7-orange)

Download restricted Telegram videos/photos/files through **your own account session** and receive them via your bot.

သင့် Telegram account session ကနေ restricted media တွေကို download လုပ်ပြီး bot ကနေ ပြန်ပို့ပေးမယ်။

## ✨ Features

- ⚡ **Parallel download** — multi-connection chunk download (much faster on high-latency links)
- 📥 **Batch download** — up to 10 links at once / တစ်ခါတည်း 10 ခုအထိ
- 📝 Original caption preserved / မူရင်း caption အတိုင်း
- 🖼️ video / photo / video_note / document / animation / audio / voice
- 📦 `/mode` — send as streamable video or as file
- 🔗 Comment-thread links supported (`t.me/c/.../.../...`)
- 🔒 Only allowed user IDs can use the bot

## 🚀 Run on VPS 24/7 (one command)

On a fresh **Ubuntu 22.04+** VPS as root:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/sankahchan/telegram-video-bot/main/install.sh) https://github.com/sankahchan/telegram-video-bot.git
```

The script will:
1. Install Python3, git, dependencies
2. Clone the repo to `/opt/tg-video-bot`
3. Ask for `API_ID` / `API_HASH` / `BOT_TOKEN` / `SESSION_STRING` → saves to `.env`
4. Install a **systemd service** → bot runs 24/7 with auto-restart

Useful commands:

```bash
systemctl status tg-video-bot      # status
journalctl -u tg-video-bot -f      # live logs
systemctl restart tg-video-bot     # restart
bash /opt/tg-video-bot/update.sh  # update from GitHub
```

## 💻 Run locally (Mac/Windows)

```bash
cp .env.example .env   # fill in your values
pip install -r requirements.txt
./start.sh             # or: python bot.py
```

Get `SESSION_STRING`:

```bash
python generate_session.py   # login with your phone number + Telegram code
```

Get `API_ID` / `API_HASH`: https://my.telegram.org → API development tools
Get `BOT_TOKEN`: [@BotFather](https://t.me/BotFather) → /newbot

## ⚙️ Configuration (`.env`)

| Variable | Description |
|---|---|
| `API_ID` / `API_HASH` | From my.telegram.org |
| `BOT_TOKEN` | From @BotFather |
| `SESSION_STRING` | Your user session (via `generate_session.py`) |
| `ALLOWED_USER_IDS` | Comma-separated Telegram user IDs allowed to use the bot |
| `DOWNLOAD_WORKERS` | Parallel download connections (default 8) |

## 🚀 Speed tips

- **Parallel download** is built in (8 connections by default, adjustable via `DOWNLOAD_WORKERS`).
  It helps most when the network latency to Telegram's servers is high.
- **VPS location matters most**: Telegram's data centers are in Europe —
  a VPS in the EU (e.g. Hetzner Germany) downloads noticeably faster than one in Asia.

## 🔐 Security

- **Never commit `.env` or `session_string.txt`** — they are in `.gitignore`.
- If your bot token was ever exposed, regenerate it via @BotFather → /revoke.
- If your session string was exposed, terminate it via Telegram → Settings → Devices.

## 📁 Files

| File | Purpose |
|---|---|
| `bot.py` | Main bot |
| `generate_session.py` | Create user session string |
| `install.sh` | VPS one-command installer |
| `update.sh` | Update bot from GitHub on VPS |
| `start.sh` | Local run script |
| `requirements.txt` | Python dependencies |

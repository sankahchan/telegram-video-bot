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
- 🌐 **Web video download** — YouTube / TikTok / Facebook (video·reel·story) / Instagram (video·reel·story) / X + hundreds of sites (yt-dlp). **PDF & direct file links** supported too
- 📊 `/stats` — download stats (day / 30 days / all-time)
- 🎵 `/mp3` — extract audio from video
- 👥 `/adduser` `/deluser` `/users` — multi-user (owner only)
- 👁️ `/watch` — auto-download new posts from channels (checks every 5 min)
- 📶 Live download progress %
- 🗜️ `/quality high|low` — low = compressed 720p (saves bandwidth). The bot also asks **Low/High per download** for video links (Telegram video / YouTube / direct .mp4); PDFs and other non-video files skip the prompt
- 📖 `/help <command>` — detailed usage + examples for each command (e.g. `/help quality`)
- 💾 `/save` — also save a copy to your Saved Messages
- 🌙 `/nightmode` — queue big files (>100MB) for night download (KST hour)
- 📦 `/zip` — send a batch as one ZIP archive
- ✂️ `/trim <start> <end>` — cut a video segment (e.g. `/trim 0:10 0:45`)
- 🔍 `/find <channel> <keyword>` — search media in a channel, tap number to download
- 🐦 **X/Twitter no-login cascade** — FxTwitter → VxTwitter → syndication CDN before yt-dlp (works for age-restricted tweets yt-dlp can't see); `/xtimeline @user [n]` downloads latest videos from a public profile
- 🧩 **YouTube PO-token provider** (optional) — `bgutil-ytdlp-pot-provider` server to beat YouTube "not a bot" blocks on VPS IPs (`POT_PROVIDER_URL`)
- ⚡ **file_id cache** — repeat links are re-sent instantly from Telegram's servers, no re-download; `/clearcache` (owner only), entries auto-expire after 30 days
- 🍪 **Per-site cookies** — `cookies_youtube.txt` / `cookies_instagram.txt` / `cookies_twitter.txt` (fallback: shared `cookies.txt`); **proxy** support via `YTDLP_PROXY` (e.g. `socks5://user:pass@host:port`)
- 🛡️ **Download integrity check** — every web video is ffprobe-verified after download (truncated/corrupt files are auto-retried, never silently sent); soundless videos get a silent audio track so Telegram shows them as video instead of GIF

> 📌 Instagram/Facebook **private** content (stories etc.) needs login cookies:
> export `cookies.txt` (browser extension "Get cookies.txt") and place it next to `bot.py`.
> Per-site cookies are also supported: `cookies_youtube.txt`, `cookies_instagram.txt`, `cookies_twitter.txt`
> (used for that site when present, otherwise the shared `cookies.txt`).

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
| `POT_PROVIDER_URL` | YouTube PO-token provider server (optional, default `http://127.0.0.1:4416`) |
| `YTDLP_PROXY` | Proxy for yt-dlp downloads (optional, e.g. `socks5://user:pass@host:port`) |

### YouTube PO-token provider (optional)

YouTube often blocks VPS IPs ("confirm you're not a bot"). A local
[bgutil-ytdlp-pot-provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider)
server can generate PO tokens that lift the block. Bind it to **localhost only**
— it has no auth:

```bash
docker run --name bgutil-provider -d --init \
  -p 127.0.0.1:4416:4416 \
  brainicism/bgutil-ytdlp-pot-provider
```

The bot auto-detects the server at `POT_PROVIDER_URL` (default `http://127.0.0.1:4416`);
if it's not running, the bot silently falls back to the normal flow.
If YouTube still blocks after this, a residential proxy (`YTDLP_PROXY`) is the
durable fallback — typically a paid service (~$3–10/month). Neither option
guarantees success; YouTube may still block aggressively-flagged IPs.

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
| `web_download.py` | yt-dlp web downloads, PO-token, proxy, per-site cookies |
| `x_media.py` | X/Twitter no-login cascade + timeline scraping |
| `filecache.py` | URL → Telegram file_id cache |
| `generate_session.py` | Create user session string |
| `install.sh` | VPS one-command installer |
| `update.sh` | Update bot from GitHub on VPS |
| `start.sh` | Local run script |
| `requirements.txt` | Python dependencies |

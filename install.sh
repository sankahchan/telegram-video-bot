#!/bin/bash
# =============================================================================
# Telegram Media Downloader Bot — VPS one-command installer (Ubuntu/Debian)
#
# Usage (VPS ပေါ်မှာ root/sudo နဲ့):
#   bash <(curl -fsSL https://raw.githubusercontent.com/sankahchan/telegram-video-bot/main/install.sh) \
#        https://github.com/sankahchan/telegram-video-bot.git
#
# လုပ်ပေးမှာတွေ:
#   1. Python3 + git install
#   2. Repo clone → /opt/tg-video-bot
#   3. venv + pip install
#   4. API_ID / API_HASH / BOT_TOKEN / SESSION_STRING မေး → .env ထဲသိမ်း
#   5. systemd service သွင်း → 24/7 auto-run + auto-restart
# =============================================================================
set -euo pipefail

REPO_URL="${1:-}"
INSTALL_DIR="/opt/tg-video-bot"
SERVICE_NAME="tg-video-bot"

if [ -z "$REPO_URL" ]; then
  echo "❌ Usage:"
  echo "   bash <(curl -fsSL https://raw.githubusercontent.com/sankahchan/telegram-video-bot/main/install.sh) https://github.com/sankahchan/telegram-video-bot.git"
  exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
  echo "❌ root နဲ့ run ပါ:  sudo bash <(curl -fsSL ...) <repo-url>"
  exit 1
fi

if ! command -v apt-get >/dev/null 2>&1; then
  echo "❌ ဒီ script က Ubuntu/Debian (apt) အတွက်ပါ."
  exit 1
fi

echo "📦 System packages install လုပ်နေပါတယ်..."
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git curl ffmpeg aria2

PYVER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "🐍 Python $PYVER"
python3 -c 'import sys; assert sys.version_info >= (3, 9), "Python 3.9+ လိုပါတယ်"' \
  || { echo "❌ Python 3.9+ လိုပါတယ် — Ubuntu 22.04+ သုံးပါ."; exit 1; }

echo "📥 Code download: $REPO_URL"
rm -rf "$INSTALL_DIR"
git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
cd "$INSTALL_DIR"

echo "📚 Dependencies install..."
python3 -m venv venv
./venv/bin/pip install -q --upgrade pip
./venv/bin/pip install -q -r requirements.txt

echo ""
echo "🔑 Bot settings ဖြည့်ပါ"
read -rp "API_ID (my.telegram.org): " API_ID
# ဂဏန်းသက်သက် ဆွဲထုတ် (quote/smart-quote/space မှန်သမျှ ခံနိုင်ရည်ရှိ)
API_ID=$(python3 -c "import re,sys; m=re.findall(r'\d+', sys.argv[1]); print(m[0] if m else '')" "$API_ID")
if [ -z "$API_ID" ]; then
  echo "❌ API_ID ဂဏန်းဖြစ်ရမယ် — my.telegram.org က App api_id ကို ပြန်ထည့်ပါ."
  exit 1
fi
read -rp "API_HASH: " API_HASH
read -rp "BOT_TOKEN (@BotFather): " BOT_TOKEN
# paste လုပ်ရင် ပါလာတတ်တဲ့ space/newline တွေ ဖယ် (quote မပါဘဲ .env ထဲ ရေးမှာ)
API_HASH=$(printf '%s' "$API_HASH" | tr -d '[:space:]')
BOT_TOKEN=$(printf '%s' "$BOT_TOKEN" | tr -d '[:space:]')
if [ -z "$API_HASH" ] || [ -z "$BOT_TOKEN" ]; then
  echo "❌ API_HASH / BOT_TOKEN အလွတ် မရပါ — ပြန်ဖြည့်ပါ."
  exit 1
fi
# ALLOWED_USER_IDS: ဂဏန်း ID တွေပဲ လက်ခံ — username မှားထည့်မိရင် တန်းသတိပေး
while true; do
  read -rp "ALLOWED_USER_IDS (ဂဏန်း Telegram ID — @userinfobot မှာ ကြည့်နိုင်, Enter=default): " ALLOWED_IDS_RAW
  ALLOWED_IDS=$(python3 -c "import re,sys; print(','.join(re.findall(r'\d+', sys.argv[1])))" "$ALLOWED_IDS_RAW")
  if [ -n "$ALLOWED_IDS" ] || [ -z "$ALLOWED_IDS_RAW" ]; then
    break
  fi
  echo "⚠️ '$ALLOWED_IDS_RAW' ထဲမှာ ဂဏန်း ID မတွေ့ပါ."
  echo "   @userinfobot ကို Telegram မှာ စာပို့ပြီး ရတဲ့ ဂဏန်းသက်သက် ထည့်ပါ (ဥပမာ 1180438393)."
  echo "   Username (@...) နဲ့ မရပါ."
done

# .env အရင်ရေး (generate_session.py က API_ID/API_HASH ကို .env ကနေ ဖတ်မယ်)
# NOTE: quote မပါဘဲ ရေး — systemd EnvironmentFile က quote ကို မဖယ်ဘူး
cat > .env <<EOF
API_ID=$API_ID
API_HASH=$API_HASH
BOT_TOKEN=$BOT_TOKEN
SESSION_STRING=
ALLOWED_USER_IDS=$ALLOWED_IDS
EOF
chmod 600 .env

echo ""
echo "SESSION_STRING (သင့် Telegram account session):"
echo "  1) ရှိပြီးသား session string ကို paste လုပ်"
echo "  2) အသစ်ထုတ် (ဖုန်းနံပါတ် + Telegram login code လိုမယ်)"
read -rp "ရွေးပါ [1/2]: " SCHOICE
if [ "$SCHOICE" = "2" ]; then
  ./venv/bin/python generate_session.py
else
  read -rp "SESSION_STRING paste: " SESSION_STRING
  printf '%s' "$SESSION_STRING" | ./venv/bin/python -c "
import sys
from dotenv import set_key
set_key('.env', 'SESSION_STRING', sys.stdin.read().strip())
print('✅ .env ထဲ သိမ်းပြီးပါပြီ')
"
fi
rm -f session_string.txt tmp_session.session*

echo ""
echo "🍪 Cookie files (optional — login လိုတဲ့ site တွေအတွက်)"
echo "   ရှိရင် file path ထည့်ပါ, မရှိရင် Enter နှိပ်"
echo "   (နောက်မှ scp နဲ့ $INSTALL_DIR/ ကို တင်လို့ရပါတယ်)"
for f in cookies.txt cookies_youtube.txt cookies_instagram.txt cookies_twitter.txt cookies_tiktok.txt; do
  read -rp "  $f path (Enter=skip): " CPATH
  if [ -n "$CPATH" ] && [ -f "$CPATH" ]; then
    cp "$CPATH" "$INSTALL_DIR/$f"
    chmod 600 "$INSTALL_DIR/$f"
    echo "  ✅ $f ထည့်ပြီးပါပြီ"
  elif [ -n "$CPATH" ]; then
    echo "  ⚠️ file မတွေ့ပါ: $CPATH — skip"
  fi
done

echo ""
echo "🧩 YouTube PO-token provider (optional — YouTube 'not a bot' block ဖြေရှင်းဖို့)"
echo "   bgutil server run ထားရင် URL ထည့်ပါ, မရှိရင် Enter နှိပ် (နောက်မှ ပြင်လို့ရ)"
echo "   server run ရန် (Docker):"
echo "     docker run --name bgutil-provider -d --init -p 127.0.0.1:4416:4416 \\"
echo "       brainicism/bgutil-ytdlp-pot-provider"
read -rp "  POT_PROVIDER_URL [Enter=skip]: " POT_URL
if [ -n "$POT_URL" ]; then
  ./venv/bin/python -c "
from dotenv import set_key
set_key('.env', 'POT_PROVIDER_URL', '''$POT_URL'''.strip())
print('  ✅ POT_PROVIDER_URL သိမ်းပြီးပါပြီ')
"
fi

echo ""
echo "⚙️ systemd service သွင်းနေပါတယ်..."
cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=Telegram Media Downloader Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$INSTALL_DIR/.env
ExecStart=$INSTALL_DIR/venv/bin/python $INSTALL_DIR/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable -q "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"
sleep 3

echo ""
if systemctl is-active -q "$SERVICE_NAME"; then
  echo "✅ Bot run နေပါပြီ! Telegram မှာ /start ပို့ပြီး စမ်းကြည့်ပါ."
else
  echo "⚠️ Service စမရပါ — log ကြည့်ပါ:"
fi
echo ""
echo "📋 Commands:"
echo "  Status : systemctl status $SERVICE_NAME"
echo "  Logs   : journalctl -u $SERVICE_NAME -f"
echo "  Restart: systemctl restart $SERVICE_NAME"
echo "  Stop   : systemctl stop $SERVICE_NAME"
echo "  Update : bash $INSTALL_DIR/update.sh"

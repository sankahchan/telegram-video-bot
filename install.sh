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
apt-get install -y -qq python3 python3-venv python3-pip git curl

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
read -rp "API_HASH: " API_HASH
read -rp "BOT_TOKEN (@BotFather): " BOT_TOKEN
read -rp "ALLOWED_USER_IDS (သင့် Telegram ID — @userinfobot မှာ ကြည့်နိုင်, Enter=default): " ALLOWED_IDS
ALLOWED_IDS="${ALLOWED_IDS:-}"

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

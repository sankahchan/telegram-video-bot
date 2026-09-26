#!/bin/bash
# Bot update: GitHub က code အသစ် pull → deps update → restart
# Usage (VPS ပေါ် root နဲ့): bash /opt/tg-video-bot/update.sh
set -euo pipefail

INSTALL_DIR="/opt/tg-video-bot"
SERVICE_NAME="tg-video-bot"

if [ "$(id -u)" -ne 0 ]; then
  echo "❌ root နဲ့ run ပါ: sudo bash $INSTALL_DIR/update.sh"
  exit 1
fi

cd "$INSTALL_DIR"
echo "📥 GitHub ကနေ အသစ်ဆွဲနေပါတယ်..."
git pull --ff-only
echo "🔍 Code စစ်နေပါတယ်..."
./venv/bin/python -m py_compile bot.py store.py web_download.py media_tools.py fast_download.py generate_session.py \
  x_media.py tiktok_media.py torrent_download.py filecache.py \
  || { echo "❌ Code error တွေ့လို့ restart မလုပ်ပါ — အဟောင်း ဆက်� run နေမယ်."; exit 1; }
echo "📚 Dependencies update..."
./venv/bin/pip install -q -r requirements.txt
# YouTube က ခဏခဏ ပြောင်းလို့ yt-dlp ကို latest ထားမှ ရမယ်
./venv/bin/pip install -q -U yt-dlp
echo "🎬 ffmpeg စစ်နေပါတယ်..."
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "📥 ffmpeg မဆိသေးလို့ install လုပ်နေပါတယ်..."
  apt-get update -qq && apt-get install -y -qq ffmpeg
fi
echo "🧲 aria2c (torrent) စစ်နေပါတယ်..."
if ! command -v aria2c >/dev/null 2>&1; then
  echo "📥 aria2c မရှိသေးလို့ install လုပ်နေပါတယ်..."
  apt-get update -qq && apt-get install -y -qq aria2
fi
echo "🔄 Restart..."
# v6.3.2: run python unbuffered (-u) so print() reaches the journal
# immediately — without it, stdout stays block-buffered and debug lines
# (e.g. the YouTube fallback chain) never appear in journalctl.
SVC_FILE="/etc/systemd/system/$SERVICE_NAME.service"
if [ -f "$SVC_FILE" ] && ! grep -q "python -u" "$SVC_FILE"; then
  sed -i 's|/venv/bin/python |/venv/bin/python -u |' "$SVC_FILE"
  systemctl daemon-reload
  echo "🔧 service: python -u (unbuffered logs) ထည့်ပြီးပြီ"
fi
systemctl restart "$SERVICE_NAME"
sleep 2
systemctl is-active -q "$SERVICE_NAME" && echo "✅ Bot run နေပါပြီ." || echo "⚠️ Service စမရပါ: journalctl -u $SERVICE_NAME -f"

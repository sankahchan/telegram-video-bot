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
./venv/bin/python -m py_compile bot.py fast_download.py generate_session.py \
  || { echo "❌ Code error တွေ့လို့ restart မလုပ်ပါ — အဟောင်း ဆက်� run နေမယ်."; exit 1; }
echo "📚 Dependencies update..."
./venv/bin/pip install -q -r requirements.txt
echo "🎬 ffmpeg စစ်နေပါတယ်..."
command -v ffmpeg >/dev/null 2>&1 || apt-get install -y -qq ffmpeg
echo "🔄 Restart..."
systemctl restart "$SERVICE_NAME"
sleep 2
systemctl is-active -q "$SERVICE_NAME" && echo "✅ Bot run နေပါပြီ." || echo "⚠️ Service စမရပါ: journalctl -u $SERVICE_NAME -f"

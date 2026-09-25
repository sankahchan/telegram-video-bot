#!/bin/bash
# Local run: .env file ကနေ settings ဖတ်မယ်
# .env မရှိရင်: cp .env.example .env  ပြီးတော့ တန်ဖိုးတွေဖြည့်
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo "❌ .env file မရှိပါ."
  echo "   cp .env.example .env   ပြီးတော့ တန်ဖိုးတွေဖြည့်ပါ."
  exit 1
fi

# venv ရှိရင် သုံး, မရှိရင် system python
if [ -x "venv/bin/python" ]; then
  PYTHON="venv/bin/python"
else
  PYTHON="python3"
fi

exec "$PYTHON" bot.py

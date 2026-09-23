"""
Gold (XAU/USD) Volatility Monitor
-----------------------------------
এই স্ক্রিপ্টটি তিনটা জিনিস চেক করে এবং প্রয়োজনে Telegram-এ নোটিফিকেশন পাঠায়:

  ১. Breakout Alert   — বর্তমান ভোলাটিলিটি থ্রেশহোল্ড পার হলে (বড় মুভ হচ্ছে)
  ২. Squeeze Alert    — বাজার অস্বাভাবিক রকম শান্ত হয়ে গেলে (বড় মুভের পূর্বাভাস —
                         দিক বলা যায় না, শুধু বলা যায় "প্রস্তুত থাকুন")
  ৩. Calendar Alert   — বড় অর্থনৈতিক খবর (Fed rate decision, NFP, CPI) আসার
                         আগে সতর্কতা (এই ইভেন্টগুলো সাধারণত গোল্ডে বড় মুভ ঘটায়)

গুরুত্বপূর্ণ: এই সিস্টেম ভবিষ্যদ্বাণী করে না, দাম কোন দিকে যাবে তা বলে না —
শুধু পরিসংখ্যানগত ও সময়সূচি-ভিত্তিক ইঙ্গিত দেয় যে সতর্ক থাকা উচিত কিনা।

চালানোর আগে এই environment variable গুলো সেট করতে হবে:
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
  VOLATILITY_THRESHOLD_PERCENT (ঐচ্ছিক, ডিফল্ট 0.5)
  WINDOW_HOURS                 (ঐচ্ছিক, ডিফল্ট 2)
  SQUEEZE_RATIO_THRESHOLD      (ঐচ্ছিক, ডিফল্ট 0.4)
  CALENDAR_ALERT_ENABLED       (ঐচ্ছিক, ডিফল্ট true)
"""

import os
from datetime import datetime, timezone

import requests

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
WINDOW_HOURS = int(os.environ.get("WINDOW_HOURS", "2"))

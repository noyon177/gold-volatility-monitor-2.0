# গোল্ড মার্কেট ভোলাটিলিটি মনিটর

গোল্ড (XAU/USD)-এর ভোলাটিলিটি মাপে এবং থ্রেশহোল্ড ক্রস করলে Telegram-এ ফোনে নোটিফিকেশন পাঠায়। GitHub Actions-এর মাধ্যমে ফ্রিতে ২৪/৭ (প্রতি ১৫ মিনিটে) চলবে — নিজের কম্পিউটার বা কোনো সার্ভার চালু রাখার দরকার নেই।

## কীভাবে কাজ করে
- প্রতি ১৫ মিনিটে GitHub Actions অটোমেটিক স্ক্রিপ্ট চালায়
- Alpha Vantage থেকে গোল্ডের ৫-মিনিট ক্যান্ডেল ডেটা আনে
- ATR (Average True Range) দিয়ে ভোলাটিলিটি ক্যালকুলেট করে
- ATR% থ্রেশহোল্ডের বেশি হলে Telegram-এ মেসেজ পাঠায়

## সেটআপ ধাপ

### ১. Alpha Vantage API Key নিন (ফ্রি)
1. https://www.alphavantage.co/support/#api-key -এ যান
2. ইমেইল দিয়ে ফ্রি API key নিন (সাথে সাথে পাবেন)
3. ফ্রি টায়ারে দিনে ২৫ রিকোয়েস্ট পাওয়া যায় — প্রতি ১৫ মিনিটে চেক করলে দিনে ৯৬ বার লাগবে যা ফ্রি লিমিটের বেশি। তাই হয় ক্রন সময় বাড়িয়ে দিন (যেমন প্রতি ৩০-৬০ মিনিটে), অথবা তাদের ফ্রি প্রিমিয়াম কী রিকোয়েস্ট করুন (কিছু প্ল্যানে বেশি লিমিট থাকে)। `.github/workflows/gold_volatility.yml` ফাইলে cron সময় পরিবর্তন করা যাবে।

### ২. Telegram Bot বানান
1. Telegram-এ **@BotFather**-কে মেসেজ দিন
2. `/newbot` কমান্ড দিন, নাম ও ইউজারনেম দিন
3. একটা **Bot Token** পাবেন (এটা সেভ রাখুন) — যেমন `123456:ABC-DEF...`
4. এবার নিজের **Chat ID** বের করতে হবে:
   - আপনার নতুন বটকে Telegram-এ খুঁজে বের করে একটা মেসেজ পাঠান (যেমন "hi")
   - ব্রাউজারে যান: `https://api.telegram.org/bot<আপনার-টোকেন>/getUpdates`
   - রেসপন্সে `"chat":{"id": ...}` এর মধ্যে যে নাম্বারটা আছে সেটাই আপনার Chat ID

### ৩. GitHub Repository বানান
1. GitHub-এ একটা নতুন (private) repository বানান
2. এই ফোল্ডারের সব ফাইল (`main.py`, `requirements.txt`, `.github/workflows/gold_volatility.yml`) repo-তে push করুন

### ৪. GitHub Secrets যোগ করুন
Repo-র **Settings → Secrets and variables → Actions → New repository secret**-এ গিয়ে এই তিনটা যোগ করুন:
- `ALPHA_VANTAGE_API_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

### ৫. চালু করুন
- এমনিতেই cron শিডিউল অনুযায়ী চলা শুরু হয়ে যাবে
- ম্যানুয়ালি টেস্ট করতে চাইলে repo-র **Actions** ট্যাবে গিয়ে "Gold Volatility Monitor" workflow সিলেক্ট করে **Run workflow** বাটনে ক্লিক করুন

## কাস্টমাইজ করার জায়গা

| কী পরিবর্তন করবেন | কোথায় |
|---|---|
| চেক করার ফ্রিকোয়েন্সি | `.github/workflows/gold_volatility.yml`-এর `cron` লাইন |
| ভোলাটিলিটি থ্রেশহোল্ড (%) | ওই ফাইলের `ATR_THRESHOLD_PERCENT` |
| ATR ক্যালকুলেশনের period | `main.py`-তে `ATR_PERIOD` env var |

## স্থানীয়ভাবে টেস্ট করতে চাইলে
```bash
pip install -r requirements.txt
export ALPHA_VANTAGE_API_KEY="আপনার-কী"
export TELEGRAM_BOT_TOKEN="আপনার-টোকেন"
export TELEGRAM_CHAT_ID="আপনার-চ্যাট-আইডি"
python main.py
```

## গুরুত্বপূর্ণ নোট
এই টুলটি একটি টেকনিক্যাল মনিটরিং সিস্টেম, ট্রেডিং অ্যাডভাইস নয়। ATR% থ্রেশহোল্ড নিজের ঝুঁকি সহনশীলতা অনুযায়ী ঠিক করুন এবং যেকোনো ট্রেডিং সিদ্ধান্তের আগে নিজের গবেষণা করুন।

# ML Meta-Label Filter (v8) — চালানোর ধাপ

নিয়ম: v8 রুল-ইঞ্জিন সিগন্যাল দেয় → ML মডেল জেতার সম্ভাবনা বলে → কম হলে ট্রেড বাদ।

## প্রস্তুতি (নিজের কম্পিউটারে)
    pip install requests numpy pandas scikit-learn joblib

## ধাপ ১: ডেটা (৬ মাস+ নিন, যত বেশি তত ভালো)
    export TWELVE_DATA_API_KEY=xxxx
    python download_data.py --symbol XAU/USD --out xauusd_5m.csv --days 180
    python download_data.py --symbol BTC/USD --out btcusd_5m.csv --days 180
(নিজের MT5 CSV ব্যবহার করলে কলাম: datetime,open,high,low,close — UTC)

## ধাপ ২: ব্যাকটেস্ট + ট্রেড ডেটাসেট (সময় লাগে, --workers দিন)
    python collect_trades.py --symbol XAUUSD --csv xauusd_5m.csv --out trades_xau.csv --workers 4
    python collect_trades.py --symbol BTCUSD --csv btcusd_5m.csv --out trades_btc.csv --workers 4
এখানেই দেখবেন v8 রুলের নিজস্ব এজ আছে কিনা (avg R)। avg R নেগেটিভ হলে ML দিয়ে ঠিক হবে না।

## ধাপ ৩: ট্রেনিং ও যাচাই
    python train_model.py --trades trades_xau.csv trades_btc.csv --out ml_model.joblib
মডেল সেভ হবে শুধু hold-out-এ পাস করলে। FAIL হলে ডিপ্লয় করবেন না।

## ধাপ ৪: বটে চালু
GitHub repo-তে দিন: ict_smc_market_reader_v8.py, ml_features.py, ml_filter.py, ml_model.joblib
Workflow-তে: `pip install requests numpy scikit-learn joblib` (একই scikit-learn ভার্সন)
Variables: ML_ON=1 (ঐচ্ছিক ML_THRESHOLD, ML_MODEL_PATH)
মডেল না পেলে বট v7-এর মতোই চলে (fail-open)।

## সতর্কতা
- প্রথমে কয়েক সপ্তাহ পেপার ট্রেড (টেলিগ্রাম সিগন্যাল) করুন।
- প্রতি ১-২ মাসে নতুন ডেটা দিয়ে ধাপ ২-৩ আবার চালান।
- ব্যাকটেস্টে নিউজ ব্ল্যাকআউট, ডেইলি লস ক্যাপ, SL-streak পজ নেই; লাইভে আছে।

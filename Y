ICT SMC SMART SCALP READER v7.0

FILES
- ict_smc_market_reader_v7.py : Python bot
- ict_smc_market_reader.yml   : GitHub Actions workflow

INSTALL
1) Put ict_smc_market_reader_v7.py in the repository root.
2) Replace the old workflow at .github/workflows/ict_smc_market_reader.yml with the supplied ict_smc_market_reader.yml.
3) Keep GitHub Actions secrets:
   TWELVE_DATA_API_KEY
   TELEGRAM_BOT_TOKEN
   TELEGRAM_CHAT_ID
4) Ensure Actions is enabled. The workflow commits ict_smc_state.json back to the repository after each run (and also keeps an Actions-cache backup). This state file is essential: it contains closed-trade outcomes and the learning history. The workflow therefore needs Actions permission to write repository contents.
5) Run workflow_dispatch with test_telegram=true once to test Telegram. Then run normally.
6) Do not run the old v4/v6 workflow at the same time. Disable/delete the old duplicate workflow, or it may double-scan and consume API credits.

WHAT IT LEARNS
- UTC session and Bangladesh-time session
- weekday, BUY/SELL direction
- score band, market/limit entry type
- low/normal/high volatility
- 15M/1H trend alignment
- premium/discount location
- freshness of liquidity sweep
It records each completed simulated trade's net R result. A losing condition is blocked only after at least 20 matching closed trades and sufficiently poor average R + win rate. The weekly report lists weakest and strongest observed conditions.

RISK GUARDS
- At most 1 open tracked trade per symbol
- At most 2 tracked trades total
- Stop new signals for a symbol after -3R realised that UTC day
- Existing loss-streak pause and adaptive score/session filters remain active

IMPORTANT LIMITATIONS
- This is a rule-based scanner with statistical feedback, not a self-training AI model.
- It sends Telegram trade plans and tracks hypothetical fills from Twelve Data candles. It does NOT place orders in Exness, MT5, Binance, or any broker.
- Tracked results are estimates. Real spreads, slippage, execution, broker candle differences, and manual actions can make actual results different.
- The learning system is not allowed to loosen risk rules or increase lot size after wins.
- Do not use live money until the bot has produced at least 50-100 completed paper trades and the net expectancy, profit factor, drawdown, and session breakdown have been independently reviewed.

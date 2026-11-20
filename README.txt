# Bitget Order-Book WebSocket upgrade

1. Stop the bot with Ctrl+C.
2. Copy `market_stream.py` and `install_ws.py` into:
   `C:\Users\sukku\Desktop\bitget-orderbook-bot-live`
3. Confirm `.env` still contains `TRADING_MODE=paper`.
4. In PowerShell, from that folder, run:
   ```powershell
   .\.venv\Scripts\python.exe .\install_ws.py
   .\.venv\Scripts\python.exe -m py_compile .\market_stream.py .\bot.py
   .\.venv\Scripts\python.exe -m compileall -q .
   ```
5. If compilation succeeds, start the bot:
   ```powershell
   .\.venv\Scripts\python.exe .\bot.py
   ```

The installer creates `bot.py.before_ws.bak` and refuses to overwrite an existing
backup. It only edits `bot.py`; it does not touch `.env`, SQLite state, credentials,
or the virtual environment.

The stream uses Bitget Classic Spot `books15`, with 45 symbols per connection.
It reconnects with backoff and clears a shard's cache on disconnect. Each cached
snapshot is processed at most once by the strategy. The held-position exit path
continues to use the existing single-symbol REST request and its existing
exchange-timestamp freshness check.

Keep paper mode enabled. Do not switch to live trading based only on successful
compilation or a successful WebSocket connection.

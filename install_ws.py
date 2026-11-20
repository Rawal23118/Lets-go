from pathlib import Path
import re
import shutil
import sys

BOT = Path("bot.py")
IMPORT_LINE = (
    "from bitget import Bitget, BitgetError, eligible_usdt_crypto_symbols, "
    "select_volume_tier_symbols"
)

def fail(message):
    print("ERROR:", message)
    raise SystemExit(1)

if not BOT.is_file():
    fail("Run this installer from the bitget-orderbook-bot-live folder.")

source = BOT.read_text(encoding="utf-8")

if "from market_stream import SpotBookStream" in source:
    print("WebSocket integration already appears to be installed. No changes made.")
    raise SystemExit(0)

if source.count(IMPORT_LINE) != 1:
    fail("Could not uniquely identify the Bitget import line. bot.py was not changed.")

backup = BOT.with_name("bot.py.before_ws.bak")
if backup.exists():
    fail(f"{backup.name} already exists. Keep it safe; rename it before retrying.")
shutil.copy2(BOT, backup)

updated = source.replace(
    IMPORT_LINE,
    IMPORT_LINE + "\nfrom market_stream import SpotBookStream",
    1,
)

# Create the stream after Bitget has created its aiohttp session.
needle = "        async with Bitget(cfg.api_key, cfg.api_secret, cfg.passphrase) as api:\n"
if updated.count(needle) != 1:
    fail("Could not uniquely identify Bitget client initialization.")
updated = updated.replace(
    needle,
    needle + "            stream = SpotBookStream(api.session, log, shard_size=45)\n",
    1,
)

# Keep a version cursor so one WebSocket snapshot is processed only once.
needle = "            last_scan_log = 0.0\n"
if updated.count(needle) != 1:
    fail("Could not uniquely identify scanner initialization.")
updated = updated.replace(
    needle,
    needle + "            last_processed_versions = {}\n",
    1,
)

# Stop market-data subscriptions while a position is managed through the
# existing single-symbol REST exit path.
needle = "                    if position:\n                        book = await api.orderbook(\n"
if updated.count(needle) != 1:
    fail("Could not uniquely identify the position-management branch.")
updated = updated.replace(
    needle,
    "                    if position:\n"
    "                        await stream.set_symbols([])\n"
    "                        book = await api.orderbook(\n",
    1,
)

# Synchronize subscriptions with the selected symbol universe.
needle = "                        if time.monotonic() - last_scan_log >= 15:\n"
if updated.count(needle) != 1:
    fail("Could not uniquely identify the scanner logging section.")
updated = updated.replace(
    needle,
    "                        await stream.set_symbols(monitored_symbols)\n\n" + needle,
    1,
)

# Replace 406 serial/concurrency-limited REST book requests with cache reads.
pattern = re.compile(
    r"                        book_semaphore = asyncio\.Semaphore\(5\)\n"
    r".*?"
    r"                        book_results = await asyncio\.gather\(\n"
    r".*?"
    r"                        \)\n",
    re.DOTALL,
)
matches = list(pattern.finditer(updated))
if len(matches) != 1:
    fail("Could not uniquely identify the REST scanner block.")
replacement = (
    "                        # Read the latest books15 snapshots received by WebSocket.\n"
    "                        book_results = [\n"
    "                            stream.get(symbol) for symbol in monitored_symbols\n"
    "                        ]\n"
)
updated = pattern.sub(replacement, updated, count=1)

# Consume only a newly received snapshot; never count a cached snapshot twice.
old = (
    "                                if isinstance(book_result, BaseException):\n"
    "                                    raise book_result\n\n"
    "                                book = book_result\n"
    "                                ts = num(book, \"ts\", default=0)\n"
)
new = (
    "                                if book_result is None:\n"
    "                                    scan_stats[\"stale\"] += 1\n"
    "                                    continue\n\n"
    "                                book, received_mono, version = book_result\n"
    "                                if version <= last_processed_versions.get(symbol, 0):\n"
    "                                    continue\n"
    "                                last_processed_versions[symbol] = version\n"
    "                                ts = num(book, \"ts\", default=0)\n"
)
if updated.count(old) != 1:
    fail("Could not uniquely identify the scanner's book-result handling.")
updated = updated.replace(old, new, 1)

# Close WebSocket tasks before releasing the process lock / database.
old = "    finally:\n        state.close()\n"
new = (
    "    finally:\n"
    "        if 'stream' in locals():\n"
    "            await stream.close()\n"
    "        state.close()\n"
)
if updated.count(old) != 1:
    fail("Could not uniquely identify bot shutdown handling.")
updated = updated.replace(old, new, 1)

# Sanity checks before writing.
required = [
    "from market_stream import SpotBookStream",
    "await stream.set_symbols(monitored_symbols)",
    "stream.get(symbol)",
    "last_processed_versions",
    "await stream.close()",
]
if not all(item in updated for item in required):
    fail("Post-edit verification failed. Original bot.py remains available in backup.")

BOT.write_text(updated, encoding="utf-8", newline="\n")
print("WebSocket integration installed.")
print("Backup created:", backup.name)
print("New helper required: market_stream.py")
print("No changes were made to .env, bot_state.sqlite3, or .venv.")

# Bitget Classic Spot Order-Book Bot

Python spot-market scanner using sustained bid-side order-book imbalance with adaptive profit exits.

## Strategy defaults

- Scans eligible USDT Spot crypto pairs selected across low-, mid-, and high-volume tiers (default top 20; configurable up to 1,000).
- Bid-side share = bid notional / (bid notional + ask notional) × 100, using price × quantity over the configured depth.
- Entry signal requires strictly more than 90% bid-side share for 3 continuous minutes. A reading of exactly 90% or lower, a wide spread, a missing/stale/malformed book, an API/data error, a gap of more than 15 seconds between valid observations, or a symbol leaving the monitored scan set resets its timer.
- Rejects order books older than 3 seconds (configurable).
- One bot-managed position at a time; this order-book bot has its own configurable allocation, default 20 USDT and hard-capped at 20 USDT.
- Adaptive profit-taking operates in the +20% to +40% estimated net-return band. The strategy considers pressure weakening, reversal and momentum stall; +40% is the hard upper exit.
- Emergency stop and maximum holding time are configurable. A separate experimental defensive exit requires bid-side share at or below 10% for 15 continuous seconds while estimated net P&L is at or below -0.35% (all three thresholds configurable).

Order-book imbalance is only displayed depth. Orders can be cancelled or spoofed; it is not a measure of executed buying or a guarantee of future price movement.

## Safety work in this revision

- Added durable SQLite order-intent journal using unique client order IDs.
- A submission timeout is treated as an unknown outcome. The bot looks up the existing order by clientOid and never blindly POST-retries.
- On restart, persisted non-reconciled order intents are looked up at Bitget. Existing live/partial orders are cancellation-requested and polled until terminal; confirmed fills are replayed into local position state. Missing or ambiguous exchange records halt startup for manual review.
- Confirmed buy/sell fill accounting and the corresponding journal transition are committed in one SQLite transaction, preventing a crash from persisting only one side of the local accounting update.
- Fill reconciliation verifies that the order side and symbol match the saved intent and that a sell cannot exceed the locally tracked position.
- Added order-detail lookup by clientOid and order cancellation support.
- Added exchange symbol metadata checks (online status, min/max market-buy notional, quote precision and base quantity precision).
- Market sell quantities are rounded down to the exchange's quantity precision and checked against the symbol's minimum notional.
- Added stale order-book rejection, confirmation reset on API/data failures, a 15-second maximum gap between valid samples, and a local SQLite process lock to prevent two copies of this bot using the same state database.
- Added automated unit tests for order-book parsing, strategy confirmation/exits, market eligibility, volume selection, precision, market-order validation and journal uniqueness. GitHub Actions runs the test suite and byte-compilation on pushes and pull requests.

## Setup (Windows PowerShell)

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python -m unittest discover -s tests -v
python bot.py
```

Paper mode is the default and does not submit exchange orders. Keep it in paper mode while validating the scanner and exit logic.

## Live deployment safeguards

Live mode is available only when the operator explicitly selects the subaccount role, confirms that the subaccount is dedicated to this bot, supplies all three subaccount API credentials, and enters the required confirmation phrase. On first live start, the bot refuses to run if non-USDT spot balances are present. On restart with a tracked position, it checks the exchange's available base balance; it checks again before each sell. The listing bot is separate and remains on the main account. The order-book bot no longer shares a USDT reservation ledger with it.

This is not a certification of live execution. The first live order still requires close supervision and a small test allocation. REST polling can miss short-lived signals and is subject to rate limits. Verify actual Bitget response fields, partial-fill/cancel races, fees, and subaccount permissions before increasing allocation. Never manually trade or transfer assets in the dedicated subaccount while this bot is running.

## API key hygiene

Use a dedicated Bitget API key with Spot read/trade permissions only, no withdrawal permission, and IP allowlisting. Keep credentials in the local `.env` file; never commit secrets or paste them into chat.

## Configuration

See `.env.example`. Defaults: paper mode, 20 USDT cap, strictly above 90% ratio, fixed 3-minute confirmation, top 20 USDT pairs, 15 book levels, 35 bps maximum spread, 3-second book freshness, -8% emergency stop, and adaptive +20% to +40% profit-taking.

## Bitget API references

- [Classic Spot order book](https://www.bitget.com/legacy-docs/classic/spot/market/Get-Orderbook)
- [Classic Spot symbol metadata](https://www.bitget.com/legacy-docs/classic/spot/market/Get-Symbols)
- [Classic Spot place order](https://www.bitget.com/legacy-docs/classic/spot/trade/Place-Order)
- [Classic Spot order info](https://www.bitget.com/legacy-docs/classic/spot/trade/Get-Order-Info)
- [Classic Spot cancel order](https://www.bitget.com/legacy-docs/classic/spot/trade/Cancel-Order)

## Market eligibility hardening

The scanner now uses Bitget Spot instrument metadata and only monitors instruments
that are online, quoted in USDT, identified as crypto, and explicitly marked
`isReality=no`. Tokenized Reality stock instruments and records with missing
metadata are excluded. Instrument metadata is cached for five minutes and a
failed refresh resets confirmation and prevents an unverified scan. Scan
progress is logged periodically.

Restart recovery replays confirmed fills only after exchange terminal status is verified and commits position/journal changes atomically.

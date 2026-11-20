
import asyncio
import ctypes
import logging
import os
import sqlite3
import time
import uuid
from decimal import Decimal, InvalidOperation

from config import Config
from bitget import Bitget, BitgetError, eligible_usdt_crypto_symbols, select_volume_tier_symbols
from market_stream import SpotBookStream
from strategy import analyse_book, Confirmation, ExitAnalyser
from execution_safety import OrderJournal, SafetyError, validate_market_buy, floor_to_precision, prepare_market_sell

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("orderbook-bot")


class State:
    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=10)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS position (
          id INTEGER PRIMARY KEY CHECK(id=1), symbol TEXT, qty REAL, cost REAL,
          entry_price REAL, opened REAL, mode TEXT)""")
        self.db.execute("CREATE TABLE IF NOT EXISTS process_lock (id INTEGER PRIMARY KEY CHECK(id=1), pid INTEGER NOT NULL, acquired REAL NOT NULL)")
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                qty REAL NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL NOT NULL,
                entry_cost REAL NOT NULL,
                entry_fee REAL NOT NULL,
                exit_gross REAL NOT NULL,
                exit_fee REAL NOT NULL,
                pnl_usdt REAL NOT NULL,
                pnl_pct REAL NOT NULL,
                opened REAL NOT NULL,
                closed REAL NOT NULL,
                reason TEXT NOT NULL
            )
        """)
        self.db.commit()
        self.journal = OrderJournal(self.db)
        self.locked = False

    def acquire(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute(
                "SELECT pid FROM process_lock WHERE id=1"
            ).fetchone()

            if row:
                pid = int(row[0])

                if os.name == "nt":
                    # Windows-compatible process existence check.
                    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                    kernel32.OpenProcess.restype = ctypes.c_void_p
                    kernel32.OpenProcess.argtypes = [
                        ctypes.c_uint32,
                        ctypes.c_int,
                        ctypes.c_uint32,
                    ]
                    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

                    # PROCESS_QUERY_LIMITED_INFORMATION
                    handle = kernel32.OpenProcess(0x1000, False, pid)

                    if handle:
                        kernel32.CloseHandle(handle)
                        alive = True
                    else:
                        error = ctypes.get_last_error()
                        # ERROR_INVALID_PARAMETER (87): PID does not exist.
                        # Other errors are treated conservatively as alive.
                        alive = error != 87

                else:
                    alive = True
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        alive = False
                    except PermissionError:
                        alive = True

                if alive:
                    raise RuntimeError(
                        f"Another bot process appears active (PID {pid}); "
                        "refusing concurrent execution"
                    )

                self.db.execute("DELETE FROM process_lock WHERE id=1")

            self.db.execute(
                "INSERT INTO process_lock VALUES (1,?,?)",
                (os.getpid(), time.time()),
            )
            self.db.commit()
            self.locked = True

        except Exception:
            self.db.rollback()
            raise

    def release(self):
        if self.locked:
            self.db.execute(
                "DELETE FROM process_lock WHERE id=1 AND pid=?",
                (os.getpid(),),
            )
            self.db.commit()
            self.locked = False

    def close(self):
        self.release()
        self.db.close()

    def get(self):
        row = self.db.execute(
            "SELECT symbol,qty,cost,entry_price,opened,mode FROM position WHERE id=1"
        ).fetchone()
        return dict(zip(
            ("symbol", "qty", "cost", "entry_price", "opened", "mode"), row
        )) if row else None

    def save(self, symbol, qty, cost, entry, mode, opened=None):
        self.db.execute(
            "INSERT OR REPLACE INTO position VALUES (1,?,?,?,?,?,?)",
            (symbol, qty, cost, entry, opened or time.time(), mode),
        )
        self.db.commit()

    def clear(self):
        self.db.execute("DELETE FROM position WHERE id=1")
        self.db.commit()

    def paper_buy(self, symbol, qty, cost, entry, opened=None):
        """Save a simulated buy. Cost excludes the modeled entry fee."""
        from decimal import Decimal, InvalidOperation

        try:
            q, c, price = map(Decimal, (str(qty), str(cost), str(entry)))
        except (InvalidOperation, ValueError):
            raise SafetyError("Invalid paper BUY values")

        if not all(x.is_finite() and x > 0 for x in (q, c, price)):
            raise SafetyError("Paper BUY values must be finite and positive")

        opened = time.time() if opened is None else float(opened)
        if not symbol or not opened > 0:
            raise SafetyError("Invalid paper BUY symbol or timestamp")

        self.db.execute("BEGIN IMMEDIATE")
        try:
            if self.db.execute("SELECT 1 FROM position WHERE id=1").fetchone():
                raise SafetyError("A position already exists; refusing paper BUY")

            self.db.execute(
                "INSERT INTO position VALUES (1,?,?,?,?,?,?)",
                (symbol, float(q), float(c), float(price), opened, "paper"),
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def paper_sell(self, exit_price, fee_rate, reason):
        """Close a paper position and persist realized P&L atomically."""
        from decimal import Decimal, InvalidOperation

        try:
            px = Decimal(str(exit_price))
            fee = Decimal(str(fee_rate))
        except (InvalidOperation, ValueError):
            raise SafetyError("Invalid paper SELL values")

        if (not px.is_finite() or px <= 0 or
                not fee.is_finite() or fee < 0 or fee >= 1):
            raise SafetyError("Paper SELL price or fee is invalid")

        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute(
                "SELECT symbol,qty,cost,entry_price,opened,mode "
                "FROM position WHERE id=1"
            ).fetchone()

            if not row:
                raise SafetyError("No position exists to close")

            symbol, qty_raw, cost_raw, entry_raw, opened, mode = row
            if mode != "paper":
                raise SafetyError("Refusing to paper-close a non-paper position")

            qty = Decimal(str(qty_raw))
            cost = Decimal(str(cost_raw))
            entry = Decimal(str(entry_raw))

            entry_fee = cost * fee
            exit_gross = px * qty
            exit_fee = exit_gross * fee
            total_entry = cost + entry_fee
            pnl = exit_gross - exit_fee - total_entry
            pnl_pct = pnl / total_entry * Decimal("100")
            closed = time.time()

            self.db.execute("""
                INSERT INTO paper_trades
                (symbol,qty,entry_price,exit_price,entry_cost,entry_fee,
                 exit_gross,exit_fee,pnl_usdt,pnl_pct,opened,closed,reason)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                symbol, float(qty), float(entry), float(px), float(cost),
                float(entry_fee), float(exit_gross), float(exit_fee),
                float(pnl), float(pnl_pct), float(opened), closed, str(reason),
            ))

            self.db.execute("DELETE FROM position WHERE id=1")
            self.db.commit()

            return {
                "symbol": symbol,
                "qty": float(qty),
                "exit_price": float(px),
                "pnl_usdt": float(pnl),
                "pnl_pct": float(pnl_pct),
                "reason": str(reason),
            }
        except Exception:
            self.db.rollback()
            raise

    def paper_summary(self):
        row = self.db.execute("""
            SELECT COUNT(*),
                   COALESCE(SUM(CASE WHEN pnl_usdt > 0 THEN 1 ELSE 0 END),0),
                   COALESCE(SUM(pnl_usdt),0),
                   COALESCE(SUM(entry_fee + exit_fee),0)
            FROM paper_trades
        """).fetchone()

        count, wins, pnl, fees = row
        return {
            "trades": int(count),
            "wins": int(wins),
            "losses": int(count - wins),
            "win_rate": 100.0 * wins / count if count else 0.0,
            "net_pnl": float(pnl),
            "fees": float(fees),
        }

    def apply_buy_fill(self, client_oid, symbol, qty, cost, entry, mode):
        if qty <= 0 or cost <= 0 or entry <= 0:
            raise SafetyError("Refusing to persist an invalid buy fill")
        intent = self.journal.get(client_oid)
        if not intent or intent[1] != symbol or intent[2] != "buy":
            raise SafetyError("Buy fill does not match its persisted order intent")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if self.db.execute("SELECT 1 FROM position WHERE id=1").fetchone():
                raise SafetyError("A position already exists; refusing to merge an unexpected buy fill")
            self.db.execute(
                "INSERT INTO position VALUES (1,?,?,?,?,?,?)",
                (symbol, qty, cost, entry, time.time(), mode),
            )
            self.journal.update(client_oid, "reconciled", commit=False)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def apply_sell_fill(self, client_oid, qty_sold):
        if qty_sold <= 0:
            raise SafetyError("Refusing to persist a zero/negative sell fill")
        intent = self.journal.get(client_oid)
        if not intent or intent[2] != "sell":
            raise SafetyError("Sell fill does not match its persisted order intent")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute(
                "SELECT symbol,qty,cost,entry_price,opened,mode FROM position WHERE id=1"
            ).fetchone()
            if not row:
                raise SafetyError("Sell fill exists but local position is missing")
            symbol, qty, cost, entry, opened, mode = row
            if intent[1] != symbol:
                raise SafetyError("Sell intent symbol does not match the tracked position")
            if Decimal(str(qty_sold)) > Decimal(str(qty)):
                raise SafetyError("Exchange sell fill exceeds locally tracked position")
            remaining = max(0.0, qty - qty_sold)
            if remaining <= max(qty * 1e-10, 1e-14):
                self.db.execute("DELETE FROM position WHERE id=1")
            else:
                remaining_cost = cost * remaining / qty
                self.db.execute(
                    "UPDATE position SET qty=?,cost=? WHERE id=1",
                    (remaining, remaining_cost),
                )
            self.journal.update(client_oid, "reconciled", commit=False)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise


def num(d, *keys, default=0):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return float(d[k])
    return default


def available_asset(assets, coin):
    coin = str(coin).upper()
    for asset in assets or []:
        if str(asset.get("coin", "")).upper() == coin:
            try:
                amount = Decimal(str(asset.get("available", "0") or "0"))
            except (InvalidOperation, TypeError) as exc:
                raise SafetyError(f"Invalid available balance for {coin}") from exc
            if not amount.is_finite() or amount < 0:
                raise SafetyError(f"Invalid available balance for {coin}")
            return amount
    return Decimal("0")


def order_fill(detail):
    if not detail:
        return 0.0, 0.0, 0.0
    return (
        num(detail, "baseVolume"),
        num(detail, "quoteVolume"),
        num(detail, "priceAvg"),
    )


async def resolve_market_order(api, journal, symbol, side, amount, client_oid, result=None):
    """Submit once, then resolve only by clientOid/orderId. Never POST-retry."""
    journal.create(client_oid, symbol, side, amount)
    order_id = (result or {}).get("orderId") if result else None
    try:
        if result is None:
            if side == "buy":
                result = await api.place_market_buy(symbol, amount, client_oid)
            else:
                result = await api.place_market_sell(symbol, amount, client_oid)
            order_id = (result or {}).get("orderId")
    except BitgetError as exc:
        journal.update(client_oid, "submission_unknown", error=str(exc))
        log.error(
            "Submission outcome unknown for %s. Resolving by clientOid; will not resubmit.",
            client_oid,
        )

    detail = None
    for attempt in range(8):
        try:
            detail = (
                await api.order_detail(order_id=order_id, client_oid=None)
                if order_id else await api.order_detail(client_oid=client_oid)
            )
        except BitgetError:
            detail = None
        if detail:
            break
        await asyncio.sleep(min(0.5 * (attempt + 1), 2.0))

    if not detail:
        journal.update(
            client_oid,
            "submission_unknown",
            order_id=order_id,
            error="No order detail found; manual reconciliation required",
        )
        raise SafetyError(
            f"Order {client_oid} has an unknown outcome. Do not restart/resubmit; reconcile in Bitget."
        )

    order_id = detail.get("orderId") or order_id
    status = detail.get("status", "")
    journal.update(client_oid, "exchange_" + status, order_id=order_id)

    if status in ("live", "partially_filled"):
        if not order_id:
            raise SafetyError(
                f"Order {client_oid} is open without an orderId; manual reconciliation required"
            )
        try:
            await api.cancel_order(symbol, order_id)
        except BitgetError as exc:
            log.warning(
                "Cancel request response for %s was uncertain; checking exchange state: %s",
                client_oid, exc,
            )
        for _ in range(8):
            await asyncio.sleep(0.5)
            try:
                detail = await api.order_detail(order_id=order_id)
            except BitgetError as exc:
                journal.update(
                    client_oid,
                    "submission_unknown",
                    order_id=order_id,
                    error=f"Could not verify order after cancel: {exc}",
                )
                raise SafetyError(
                    f"Cannot verify final status for order {client_oid}; manual reconciliation required"
                ) from exc
            if detail and detail.get("status") in ("filled", "cancelled"):
                break
        if not detail or detail.get("status") not in ("filled", "cancelled"):
            journal.update(
                client_oid,
                "submission_unknown",
                order_id=order_id,
                error="Cancellation not confirmed",
            )
            raise SafetyError(f"Could not confirm cancellation/final fill for {client_oid}")

    filled, quote, average = order_fill(detail)
    if detail.get("status") not in ("filled", "cancelled") or (
        filled <= 0 and detail.get("status") != "cancelled"
    ):
        journal.update(
            client_oid,
            "submission_unknown",
            order_id=order_id,
            error="Non-terminal or invalid fill response",
        )
        raise SafetyError(f"Order {client_oid} has no safely confirmed terminal status")
    return detail, filled, quote, average


async def recover_order_intents(api, state):
    """Resolve every persisted intent against Bitget before strategy execution resumes."""
    for intent in state.journal.recoverable():
        client_oid, symbol, side, requested, order_id, old_status = intent
        try:
            detail = (
                await api.order_detail(order_id=order_id)
                if order_id else await api.order_detail(client_oid=client_oid)
            )
        except BitgetError as exc:
            raise SafetyError(
                f"Cannot reconcile persisted order {client_oid}: {exc}"
            ) from exc
        if not detail:
            raise SafetyError(
                f"Order {client_oid} ({old_status}) is not yet visible at Bitget. "
                "Trading is halted; verify it in the exchange UI before manual resolution."
            )
        exchange_status = str(detail.get("status", "")).lower()
        exchange_order_id = detail.get("orderId") or order_id
        if exchange_status in ("live", "partially_filled"):
            if not exchange_order_id:
                raise SafetyError(
                    f"Open order {client_oid} has no order ID; manual reconciliation required"
                )
            try:
                await api.cancel_order(symbol, exchange_order_id)
            except BitgetError:
                pass
            detail = None
            for _ in range(10):
                await asyncio.sleep(0.75)
                try:
                    detail = await api.order_detail(order_id=exchange_order_id)
                except BitgetError:
                    continue
                if detail and str(detail.get("status", "")).lower() in ("filled", "cancelled"):
                    break
            if not detail or str(detail.get("status", "")).lower() not in ("filled", "cancelled"):
                raise SafetyError(
                    f"Could not confirm terminal state for recovered order {client_oid}"
                )
            exchange_status = str(detail.get("status", "")).lower()
        if exchange_status not in ("filled", "cancelled"):
            raise SafetyError(
                f"Order {client_oid} has unsupported/nonterminal exchange status {exchange_status!r}; "
                "manual reconciliation required."
            )
        filled, quote, average = order_fill(detail)
        state.journal.update(
            client_oid,
            "exchange_" + exchange_status,
            order_id=exchange_order_id,
        )
        if filled > 0:
            if side == "buy":
                if quote <= 0:
                    raise SafetyError(
                        f"Recovered buy {client_oid} has no valid quote fill amount"
                    )
                state.apply_buy_fill(
                    client_oid, symbol, filled, quote,
                    average or quote / filled, "live",
                )
            else:
                state.apply_sell_fill(client_oid, filled)
        else:
            state.journal.update(client_oid, "reconciled")
        log.warning(
            "Recovered order %s side=%s status=%s base_fill=%s quote_fill=%s",
            client_oid, side, exchange_status, filled, quote,
        )


async def run():
    cfg = Config()
    cfg.validate()
    log.warning(
        "MODE=%s | allocation cap=%.2f USDT | sustained ratio %.1f%% for %ss",
        cfg.mode.upper(), cfg.allocation, cfg.buy_ratio, cfg.confirm_seconds,
    )

    state = State(cfg.state_db)
    state.acquire()
    try:
        confirmation = Confirmation(cfg.confirm_seconds)
        exits = ExitAnalyser(
            bearish_ratio=cfg.defensive_exit_ratio,
            bearish_seconds=cfg.defensive_exit_seconds,
            bearish_loss_pct=cfg.defensive_exit_loss_pct,
        )
        async with Bitget(cfg.api_key, cfg.api_secret, cfg.passphrase) as api:
            stream = SpotBookStream(api.session, log, shard_size=45)
            if cfg.mode == "live":
                await recover_order_intents(api, state)
                unresolved = state.journal.unresolved()
                if unresolved:
                    raise SafetyError(
                        f"Order intents remain unresolved after recovery: {unresolved}"
                    )

                tracked = state.get()
                open_orders = await api.open_orders()
                if open_orders:
                    order_ids = [
                        str(order.get("orderId", "unknown"))
                        for order in open_orders[:10]
                    ]
                    raise SafetyError(
                        "Subaccount has pre-existing open Spot orders "
                        + ", ".join(order_ids)
                        + ". Cancel/reconcile them before starting this bot."
                    )

                assets = await api.assets()
                if not isinstance(assets, list):
                    raise SafetyError(
                        "Invalid Spot assets response; refusing live startup"
                    )

                if tracked is None:
                    unrelated = []
                    restricted_usdt = False
                    for asset in assets:
                        coin = str(asset.get("coin", "")).upper()
                        try:
                            available_amount = Decimal(
                                str(asset.get("available", "0") or "0")
                            )
                            frozen_amount = Decimal(
                                str(asset.get("frozen", "0") or "0")
                            )
                            locked_amount = Decimal(
                                str(asset.get("locked", "0") or "0")
                            )
                        except (InvalidOperation, TypeError):
                            raise SafetyError(
                                f"Invalid balance returned for {coin or 'unknown asset'}"
                            )
                        amounts = (
                            available_amount, frozen_amount, locked_amount
                        )
                        if any(
                            not amount.is_finite() or amount < 0
                            for amount in amounts
                        ):
                            raise SafetyError(
                                f"Invalid negative/non-finite balance returned for {coin or 'unknown asset'}"
                            )
                        if coin and coin != "USDT" and sum(amounts) > 0:
                            unrelated.append(coin)
                        if coin == "USDT" and (
                            frozen_amount > 0 or locked_amount > 0
                        ):
                            restricted_usdt = True
                    if unrelated:
                        raise SafetyError(
                            "Subaccount is not clean: non-USDT Spot balances exist ("
                            + ", ".join(sorted(set(unrelated)))
                            + "). Move them out before first live start."
                        )
                    if restricted_usdt:
                        raise SafetyError(
                            "Subaccount has frozen or locked USDT. Resolve it before live startup."
                        )
                else:
                    rows = await api.symbols(tracked["symbol"])
                    meta = next(
                        (x for x in (rows or []) if x.get("symbol") == tracked["symbol"]),
                        None,
                    )
                    base_coin = str((meta or {}).get("baseCoin", "")).upper()
                    if not base_coin:
                        raise SafetyError(
                            "Cannot identify tracked position base coin; refusing live startup"
                        )
                    available_base = available_asset(assets, base_coin)
                    if available_base < Decimal(str(tracked["qty"])):
                        raise SafetyError(
                            f"Tracked {base_coin} quantity exceeds exchange available balance; "
                            "manual reconciliation required"
                        )
            else:
                unresolved = state.journal.unresolved()
                if unresolved:
                    raise SafetyError(f"Unresolved order intents exist: {unresolved}")

            existing_position = state.get()
            if existing_position and existing_position["mode"] != cfg.mode:
                raise SafetyError(
                    "Stored position mode differs from configured mode; reconcile before switching modes."
                )

            symbol_meta = {}
            eligible_symbols = None
            last_instruments_refresh = 0.0
            instruments_cache_seconds = 300
            monitored_symbols = []
            last_symbol_selection_refresh = 0.0
            symbol_selection_cache_seconds = 300
            last_scan_log = 0.0
            last_processed_versions = {}

            while True:
                started = time.monotonic()
                try:
                    position = state.get()

                    if position:
                        await stream.set_symbols([])
                        book = await api.orderbook(
                            position["symbol"], cfg.book_levels
                        )
                        ts = num(book, "ts", default=0)
                        if not ts or abs(time.time() * 1000 - ts) > cfg.max_book_age_ms:
                            raise SafetyError(
                                "Position order book is missing/stale; refusing to calculate an exit from stale data"
                            )

                        sig = analyse_book(book)
                        bids = book.get("bids") or []
                        liquidation_price = float(bids[0][0])
                        gross = liquidation_price * position["qty"]
                        fee = cfg.fee_rate
                        net_pct = (
                            (gross * (1-fee) - position["cost"] * (1+fee))
                            / (position["cost"] * (1+fee))
                        ) * 100 if position["cost"] else 0
                        age_min = (time.time() - position["opened"]) / 60

                        should_sell, reason = exits.evaluate(
                            position["symbol"], sig.ratio, sig.mid, net_pct,
                            cfg.min_take_profit, cfg.max_take_profit,
                        )
                        if net_pct <= cfg.emergency_stop_pct:
                            should_sell, reason = True, "emergency_stop"
                        if age_min >= cfg.max_age_minutes:
                            should_sell, reason = True, "max_holding_time"

                        if should_sell:
                            if cfg.mode == "paper":
                                trade = state.paper_sell(
                                    liquidation_price, cfg.fee_rate, reason
                                )
                                summary = state.paper_summary()
                                log.info(
                                    "[PAPER] SELL %s qty=%.8f exit=%.8f "
                                    "pnl=%+.4f USDT pnl_pct=%+.2f%% reason=%s",
                                    trade["symbol"], trade["qty"], trade["exit_price"],
                                    trade["pnl_usdt"], trade["pnl_pct"], trade["reason"],
                                )
                                log.info(
                                    "[PAPER] PERFORMANCE trades=%d wins=%d losses=%d "
                                    "win_rate=%.1f%% cumulative_net_pnl=%+.4f USDT "
                                    "fees=%.4f USDT",
                                    summary["trades"], summary["wins"], summary["losses"],
                                    summary["win_rate"], summary["net_pnl"], summary["fees"],
                                )
                            else:
                                if position["symbol"] not in symbol_meta:
                                    rows = await api.symbols(position["symbol"])
                                    symbol_meta[position["symbol"]] = next(
                                        (x for x in (rows or [])
                                         if x.get("symbol") == position["symbol"]),
                                        None,
                                    )
                                meta = symbol_meta[position["symbol"]]
                                if not meta or not meta.get("baseCoin"):
                                    raise SafetyError(
                                        "Missing base-coin metadata; refusing market sell"
                                    )

                                assets = await api.assets()
                                available_base = available_asset(
                                    assets, meta["baseCoin"]
                                )
                                sell_qty = prepare_market_sell(
                                    meta, position["qty"], liquidation_price
                                )
                                if available_base < Decimal(str(sell_qty)):
                                    raise SafetyError(
                                        "Exchange available base balance is below the bot-tracked sell quantity; refusing sell"
                                    )

                                client_oid = "ob-s-" + uuid.uuid4().hex[:20]
                                detail, sold, quote, avg = await resolve_market_order(
                                    api, state.journal, position["symbol"], "sell",
                                    str(sell_qty), client_oid,
                                )
                                remaining = max(0.0, position["qty"] - sold)
                                if remaining <= max(position["qty"] * 1e-10, 1e-14):
                                    state.apply_sell_fill(client_oid, sold)
                                    log.warning(
                                        "SELL reconciled %s sold=%s avg=%s reason=%s",
                                        position["symbol"], sold, avg, reason,
                                    )
                                elif sold > 0:
                                    state.apply_sell_fill(client_oid, sold)
                                    log.warning(
                                        "PARTIAL SELL reconciled; remaining=%s",
                                        remaining,
                                    )
                                else:
                                    state.journal.update(
                                        client_oid, "reconciled",
                                        order_id=detail.get("orderId"),
                                    )
                                    log.error(
                                        "Sell completed with zero fill; position retained"
                                    )
                        else:
                            log.info(
                                "HOLD %s ratio=%.2f%% est_net=%.2f%% bid=%.8f",
                                position["symbol"], sig.ratio, net_pct,
                                liquidation_price,
                            )

                    else:
                        now_mono = time.monotonic()

                        # Refresh instrument metadata only when its cache expires.
                        if (
                            eligible_symbols is None
                            or now_mono - last_instruments_refresh
                            >= instruments_cache_seconds
                        ):
                            try:
                                instruments = await api.instruments()
                                eligible_symbols = eligible_usdt_crypto_symbols(
                                    instruments
                                )
                                last_instruments_refresh = time.monotonic()
                                monitored_symbols = []
                                last_symbol_selection_refresh = 0.0
                                log.info(
                                    "Eligible ordinary USDT crypto instruments: %s",
                                    len(eligible_symbols),
                                )
                            except Exception:
                                eligible_symbols = None
                                confirmation.reset_all()
                                raise

                        # Fetch the full ticker list only when symbol selection
                        # is due, not on every main-loop iteration.
                        now_mono = time.monotonic()
                        if (
                            not monitored_symbols
                            or now_mono - last_symbol_selection_refresh
                            >= symbol_selection_cache_seconds
                        ):
                            try:
                                tickers = await api.tickers()
                            except Exception:
                                confirmation.reset_all()
                                raise

                            selected = select_volume_tier_symbols(
                                tickers, eligible_symbols,
                                cfg.min_quote_volume, cfg.top_symbols,
                            )
                            if not selected:
                                confirmation.reset_all()
                                raise SafetyError(
                                    "No eligible symbols meet the configured quote-volume threshold"
                                )

                            monitored_symbols = selected
                            last_symbol_selection_refresh = time.monotonic()
                            confirmation.reset_unseen(monitored_symbols)
                            log.info(
                                "Refreshed monitored symbol selection (%ss interval)",
                                symbol_selection_cache_seconds,
                            )

                        await stream.set_symbols(monitored_symbols)

                        if time.monotonic() - last_scan_log >= 15:
                            log.info(
                                "Monitoring %s selected symbols: %s",
                                len(monitored_symbols), ", ".join(monitored_symbols),
                            )
                            last_scan_log = time.monotonic()

                        # Read the latest books15 snapshots received by WebSocket.
                        book_results = [
                            stream.get(symbol) for symbol in monitored_symbols
                        ]

                        scan_stats = {
                            "fresh": 0,
                            "stale": 0,
                            "spread_rejected": 0,
                            "ratio_rejected": 0,
                            "confirming": 0,
                            "confirmed": 0,
                            "errors": 0,
                        }
                        book_ages_ms = []

                        for symbol, book_result in zip(
                            monitored_symbols, book_results
                        ):
                            try:
                                if book_result is None:
                                    scan_stats["stale"] += 1
                                    # Missing data is a hard break in signal continuity.
                                    confirmation.reset(symbol)
                                    continue

                                book, received_mono, version = book_result

                                receive_age_ms = max(
                                    0.0,
                                    (time.monotonic() - received_mono) * 1000
                                )

                                ts = num(book, "ts", default=0)
                                exchange_age_ms = (
                                    abs(time.time() * 1000 - ts)
                                    if ts else None
                                )

                                if (
                                    receive_age_ms > cfg.max_book_age_ms
                                    or exchange_age_ms is None
                                    or exchange_age_ms > cfg.max_book_age_ms
                                ):
                                    scan_stats["stale"] += 1

                                    if version > last_processed_versions.get(symbol, 0):
                                        last_processed_versions[symbol] = version

                                    # A stale cached snapshot breaks signal continuity
                                    # even when no newer WebSocket version arrived.
                                    confirmation.reset(symbol)
                                    continue

                                # Count fresh cached books on every scan.
                                scan_stats["fresh"] += 1
                                book_ages_ms.append(receive_age_ms)

                                # Analyse each snapshot version only once.
                                if version <= last_processed_versions.get(symbol, 0):
                                    continue

                                last_processed_versions[symbol] = version

                                sig = analyse_book(book)

                                if sig.spread_bps > cfg.max_spread_bps:
                                    scan_stats["spread_rejected"] += 1
                                    confirmation.update(
                                        symbol, 0, cfg.buy_ratio
                                    )
                                    continue

                                if sig.ratio <= cfg.buy_ratio:
                                    scan_stats["ratio_rejected"] += 1
                                    confirmation.update(
                                        symbol, sig.ratio, cfg.buy_ratio
                                    )
                                    continue

                                if not confirmation.update(
                                    symbol, sig.ratio, cfg.buy_ratio
                                ):
                                    scan_stats["confirming"] += 1
                                    continue

                                scan_stats["confirmed"] += 1

                                if cfg.mode == "paper":
                                    asks = book.get("asks") or []
                                    if not asks:
                                        raise SafetyError(
                                            "Paper BUY refused: order book has no asks"
                                        )
                                    best_ask = float(asks[0][0])

                                    quote_cost = cfg.allocation / (1.0 + cfg.fee_rate)
                                    qty = quote_cost / best_ask

                                    state.paper_buy(
                                        symbol, qty, quote_cost, best_ask
                                    )
                                    log.info(
                                        "[PAPER] BUY %s ratio=%.2f%% ask=%.8f "
                                        "qty=%.8f notional=%.4f entry_fee=%.4f "
                                        "total_cap=%.2f USDT",
                                        symbol, sig.ratio, best_ask, qty,
                                        quote_cost, quote_cost * cfg.fee_rate,
                                        cfg.allocation,
                                    )
                                else:
                                    if symbol not in symbol_meta:
                                        rows = await api.symbols(symbol)
                                        symbol_meta[symbol] = next(
                                            (x for x in (rows or [])
                                             if x.get("symbol") == symbol),
                                            None,
                                        )

                                    meta = symbol_meta[symbol]
                                    if not meta:
                                        raise SafetyError(
                                            f"Missing symbol metadata for {symbol}"
                                        )

                                    assets = await api.assets()
                                    usdt = next(
                                        (a for a in (assets or [])
                                         if a.get("coin", "").upper() == "USDT"),
                                        {},
                                    )
                                    available = num(
                                        usdt, "available", default=0
                                    )
                                    spend = min(
                                        cfg.allocation,
                                        max(0, available - 0.10),
                                    )
                                    spend = floor_to_precision(
                                        spend, meta.get("quotePrecision", 8)
                                    )
                                    validate_market_buy(meta, spend)

                                    client_oid = "ob-b-" + uuid.uuid4().hex[:20]
                                    detail, filled, quote, average = (
                                        await resolve_market_order(
                                            api, state.journal, symbol, "buy",
                                            spend, client_oid,
                                        )
                                    )

                                    if str(detail.get("status", "")).lower() not in (
                                        "filled", "cancelled"
                                    ):
                                        raise SafetyError(
                                            "Buy order is not terminal; refusing to continue"
                                        )

                                    if filled > 0 and quote > 0:
                                        state.apply_buy_fill(
                                            client_oid, symbol, filled, quote,
                                            average or quote / filled, "live",
                                        )
                                        log.warning(
                                            "BUY reconciled %s qty=%s quote=%s status=%s",
                                            symbol, filled, quote,
                                            detail.get("status"),
                                        )
                                    else:
                                        state.journal.update(
                                            client_oid, "reconciled",
                                            order_id=detail.get("orderId"),
                                        )
                                        log.warning(
                                            "Buy order cancelled without a fill: %s",
                                            client_oid,
                                        )

                                break

                            except SafetyError:
                                raise
                            except Exception as exc:
                                scan_stats["errors"] += 1
                                confirmation.reset(symbol)
                                log.warning(
                                    "Symbol %s skipped; confirmation reset: %s",
                                    symbol, exc,
                                )

                        age_summary = (
                            f"min={min(book_ages_ms):.0f}ms max={max(book_ages_ms):.0f}ms"
                            if book_ages_ms else "no fresh books"
                        )

                        log.info(
                            "SCAN DIAGNOSTICS fresh=%s stale=%s spread_rejected=%s "
                            "ratio_rejected=%s confirming=%s confirmed=%s errors=%s ages=(%s)",
                            scan_stats["fresh"],
                            scan_stats["stale"],
                            scan_stats["spread_rejected"],
                            scan_stats["ratio_rejected"],
                            scan_stats["confirming"],
                            scan_stats["confirmed"],
                            scan_stats["errors"],
                            age_summary,
                        )

                    elapsed = time.monotonic() - started
                    await asyncio.sleep(
                        max(0.05, cfg.scan_interval - elapsed)
                    )

                except asyncio.CancelledError:
                    raise
                except SafetyError:
                    log.exception(
                        "Safety stop: bot will not continue trading"
                    )
                    raise
                except Exception:
                    log.exception("Main loop error; cooling down")
                    await asyncio.sleep(3)

    finally:
        if 'stream' in locals():
            await stream.close()
        state.close()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.info("Stopped by user")

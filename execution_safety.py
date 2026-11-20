"""Fail-closed execution helpers and durable Bitget order journal."""
import sqlite3
import time
from decimal import Decimal, ROUND_DOWN, InvalidOperation


class SafetyError(RuntimeError):
    pass


def floor_to_precision(value, precision):
    try:
        precision = int(precision)
        if precision < 0 or precision > 18:
            raise ValueError("precision out of range")
        quantum = Decimal(1).scaleb(-precision)
        amount = Decimal(str(value))
        if not amount.is_finite():
            raise ValueError("non-finite amount")
        return amount.quantize(quantum, rounding=ROUND_DOWN)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise SafetyError("Invalid amount or exchange precision") from exc


def validate_market_buy(symbol_info, quote_amount):
    if not symbol_info or symbol_info.get("status") != "online":
        raise SafetyError("Symbol metadata is missing or symbol is not online")
    try:
        amount = Decimal(str(quote_amount))
        minimum = Decimal(str(symbol_info.get("minTradeUSDT") or "0"))
        maximum = Decimal(str(symbol_info.get("maxMarketOrderValue") or "0"))
    except InvalidOperation as exc:
        raise SafetyError("Invalid exchange notional metadata") from exc
    if not amount.is_finite() or amount <= 0 or (minimum > 0 and amount < minimum):
        raise SafetyError("Market buy is invalid or below Bitget minimum")
    if maximum > 0 and amount > maximum:
        raise SafetyError("Market buy exceeds Bitget maximum")
    return amount


class OrderJournal:
    """Persist order intent before POST; unresolved intents are never blindly retried."""
    TERMINAL = ("filled", "cancelled", "reconciled")

    def __init__(self, connection):
        self.db = connection
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS order_intents (
                client_oid TEXT PRIMARY KEY, symbol TEXT NOT NULL, side TEXT NOT NULL,
                requested_amount TEXT NOT NULL, order_id TEXT, status TEXT NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL, last_error TEXT
            )
        """)
        self.db.commit()

    def create(self, client_oid, symbol, side, amount):
        if side not in ("buy", "sell") or not client_oid or not symbol:
            raise SafetyError("Invalid order intent")
        now = time.time()
        try:
            self.db.execute(
                "INSERT INTO order_intents VALUES (?,?,?,?,NULL,'prepared',?,?,NULL)",
                (client_oid, symbol, side, str(amount), now, now),
            )
            self.db.commit()
        except sqlite3.IntegrityError as exc:
            raise SafetyError("Duplicate client order ID blocked") from exc

    def update(self, client_oid, status, order_id=None, error=None, commit=True):
        cur = self.db.execute(
            """UPDATE order_intents SET status=?, order_id=COALESCE(?,order_id),
               updated=?, last_error=? WHERE client_oid=?""",
            (status, order_id, time.time(), error, client_oid),
        )
        if cur.rowcount != 1:
            raise SafetyError("Order intent disappeared during update")
        if commit:
            self.db.commit()

    def get(self, client_oid):
        return self.db.execute(
            """SELECT client_oid,symbol,side,requested_amount,order_id,status
               FROM order_intents WHERE client_oid=?""", (client_oid,)
        ).fetchone()

    def recoverable(self):
        return self.db.execute(
            """SELECT client_oid,symbol,side,requested_amount,order_id,status
               FROM order_intents WHERE status NOT IN ('reconciled') ORDER BY created"""
        ).fetchall()

    def unresolved(self):
        return self.db.execute(
            """SELECT client_oid,symbol,side,requested_amount,order_id,status
               FROM order_intents WHERE status NOT IN ('filled','cancelled','reconciled')"""
        ).fetchall()


def prepare_market_sell(symbol_info, base_amount, best_bid):
    if not symbol_info or symbol_info.get("status") != "online":
        raise SafetyError("Symbol is not online for selling")
    qty = floor_to_precision(base_amount, symbol_info.get("quantityPrecision", 0))
    minimum = Decimal(str(symbol_info.get("minTradeUSDT") or "0"))
    value = qty * Decimal(str(best_bid))
    if qty <= 0 or not value.is_finite():
        raise SafetyError("Sell quantity or notional is invalid")
    if minimum > 0 and value < minimum:
        raise SafetyError("Remaining position value is below Bitget minimum")
    return qty

"""Cross-process USDT reservation ledger shared by the two Bitget bots.

Both repositories must use the same BITGET_SHARED_DB_PATH. On Windows the
default is %LOCALAPPDATA%\\BitgetBots\\shared_account.sqlite3.
Reservations are durable and fail closed: only explicit terminal reconciliation
may settle a reservation. This module never reads or modifies exchange holdings.
"""
import os
import sqlite3
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path


class ReservationError(RuntimeError):
    pass


def _decimal(value, field):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ReservationError(f"Invalid {field}") from exc
    if not result.is_finite() or result < 0:
        raise ReservationError(f"{field} must be finite and non-negative")
    return result


def default_database_path():
    configured = os.getenv("BITGET_SHARED_DB_PATH")
    if configured:
        return Path(configured).expanduser()
    root = os.getenv("LOCALAPPDATA") or os.getenv("USERPROFILE")
    if root:
        return Path(root) / "BitgetBots" / "shared_account.sqlite3"
    # Linux/macOS test and development fallback; both repos resolve to the
    # same per-user path. Windows deployments use LOCALAPPDATA above.
    return Path.home() / ".local" / "share" / "BitgetBots" / "shared_account.sqlite3"


class SharedAccountCoordinator:
    ACTIVE = ("RESERVED", "SUBMISSION_UNKNOWN")

    def __init__(self, path=None):
        self.path = Path(path) if path else default_database_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS usdt_reservations (
                client_oid TEXT PRIMARY KEY,
                bot_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                budget TEXT NOT NULL,
                status TEXT NOT NULL,
                created REAL NOT NULL,
                updated REAL NOT NULL,
                detail TEXT
            )
        """)
        self.db.execute("""
            CREATE INDEX IF NOT EXISTS idx_reservations_active
            ON usdt_reservations(status)
        """)

    def close(self):
        self.db.close()

    def reserved_total(self):
        rows = self.db.execute(
            "SELECT budget FROM usdt_reservations WHERE status IN (?,?)",
            self.ACTIVE,
        ).fetchall()
        return sum((Decimal(row[0]) for row in rows), Decimal("0"))

    def reserve(self, bot_id, client_oid, symbol, budget, available_usdt):
        bot_id = str(bot_id).strip()
        client_oid = str(client_oid).strip()
        symbol = str(symbol).upper().strip()
        amount = _decimal(budget, "reservation budget")
        available = _decimal(available_usdt, "available USDT")
        if not bot_id or not client_oid or not symbol or amount <= 0:
            raise ReservationError("Bot, clientOid, symbol and positive budget are required")
        now = time.time()
        try:
            self.db.execute("BEGIN IMMEDIATE")
            existing = self.db.execute(
                "SELECT bot_id,symbol,budget,status FROM usdt_reservations WHERE client_oid=?",
                (client_oid,),
            ).fetchone()
            if existing:
                raise ReservationError("clientOid already has a shared reservation")
            rows = self.db.execute(
                "SELECT budget FROM usdt_reservations WHERE status IN (?,?)",
                self.ACTIVE,
            ).fetchall()
            already_reserved = sum(
                (Decimal(row[0]) for row in rows), Decimal("0")
            )
            if amount + already_reserved > available:
                raise ReservationError(
                    f"Insufficient unreserved USDT: available={available}, "
                    f"reserved={already_reserved}, requested={amount}"
                )
            self.db.execute(
                """INSERT INTO usdt_reservations
                   (client_oid,bot_id,symbol,budget,status,created,updated,detail)
                   VALUES (?,?,?,?,?,?,?,NULL)""",
                (client_oid, bot_id, symbol, str(amount), "RESERVED", now, now),
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def mark_unknown(self, client_oid, detail):
        self._transition(client_oid, "SUBMISSION_UNKNOWN", str(detail))

    def settle_terminal(self, client_oid, terminal_status, detail=None):
        status = str(terminal_status).upper()
        if status not in {"FILLED", "CANCELLED", "REJECTED", "NO_FILL"}:
            raise ReservationError("Only a verified terminal order status can settle funds")
        self._transition(client_oid, "SETTLED", f"{status}: {detail or ''}")

    def _transition(self, client_oid, status, detail):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            cur = self.db.execute(
                """UPDATE usdt_reservations SET status=?,updated=?,detail=?
                   WHERE client_oid=? AND status IN (?,?)""",
                (status, time.time(), detail, str(client_oid), *self.ACTIVE),
            )
            if cur.rowcount != 1:
                row = self.db.execute(
                    "SELECT status FROM usdt_reservations WHERE client_oid=?",
                    (str(client_oid),),
                ).fetchone()
                if not row:
                    raise ReservationError("Reservation not found; refusing implicit release")
                if row[0] != status:
                    raise ReservationError(f"Cannot transition reservation from {row[0]} to {status}")
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def get(self, client_oid):
        row = self.db.execute(
            """SELECT client_oid,bot_id,symbol,budget,status,created,updated,detail
               FROM usdt_reservations WHERE client_oid=?""",
            (str(client_oid),),
        ).fetchone()
        if not row:
            return None
        keys = ("client_oid","bot_id","symbol","budget","status","created","updated","detail")
        return dict(zip(keys, row))

    def active(self):
        rows = self.db.execute(
            """SELECT client_oid,bot_id,symbol,budget,status,created,updated,detail
               FROM usdt_reservations WHERE status IN (?,?) ORDER BY created""",
            self.ACTIVE,
        ).fetchall()
        keys = ("client_oid","bot_id","symbol","budget","status","created","updated","detail")
        return [dict(zip(keys, row)) for row in rows]

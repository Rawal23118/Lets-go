from pathlib import Path
import ast
import shutil
import time

p = Path("bot.py")
s = p.read_text(encoding="utf-8")

backup = Path("bot.py.before_paper_upgrade.bak")

if not p.exists() or not backup.exists():
    raise SystemExit("STOP: bot.py or its verified backup is missing.")

if "def paper_buy(" in s or "CREATE TABLE IF NOT EXISTS paper_trades" in s:
    raise SystemExit("STOP: Upgrade markers already exist. No changes made.")

# 1. Add persistent paper-trade history.
init_anchor = '        self.db.commit()\n        self.journal = OrderJournal(self.db)'
schema = '''        self.db.execute("""
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
'''
if s.count(init_anchor) != 1:
    raise SystemExit("STOP: Database initialization anchor did not match exactly.")
s = s.replace(init_anchor, schema + init_anchor, 1)

# 2. Add paper accounting methods without changing live fill methods.
method_anchor = '    def apply_buy_fill(self, client_oid, symbol, qty, cost, entry, mode):'
methods = '''    def paper_buy(self, symbol, qty, cost, entry, opened=None):
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

'''
if s.count(method_anchor) != 1:
    raise SystemExit("STOP: State method insertion point did not match exactly.")
s = s.replace(method_anchor, methods + method_anchor, 1)

# 3. Replace only the paper SELL branch.
old_sell = '''                            if cfg.mode == "paper":
                                log.info(
                                    "[PAPER] SELL %s qty=%s net_est=%.2f%% reason=%s",
                                    position["symbol"], position["qty"], net_pct, reason,
                                )
                                state.clear()
'''
new_sell = '''                            if cfg.mode == "paper":
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
'''
if s.count(old_sell) != 1:
    raise SystemExit("STOP: Paper SELL block did not match exactly.")
s = s.replace(old_sell, new_sell, 1)

# 4. Replace only the paper BUY branch. Use best ask, not midpoint.
old_buy = '''                                if cfg.mode == "paper":
                                    log.info(
                                        "[PAPER] BUY signal %s ratio=%.2f%% mid=%.8f allocation=%.2f USDT",
                                        symbol, sig.ratio, sig.mid, cfg.allocation,
                                    )
                                    qty = cfg.allocation / sig.mid
                                    state.save(
                                        symbol, qty, cfg.allocation,
                                        sig.mid, "paper",
                                    )
'''
new_buy = '''                                if cfg.mode == "paper":
                                    asks = book.get("asks") or []
                                    if not asks:
                                        raise SafetyError(
                                            "Paper BUY refused: order book has no asks"
                                        )
                                    best_ask = float(asks[0][0])

                                    # Keep modeled entry fee inside the allocation cap.
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
'''
if s.count(old_buy) != 1:
    raise SystemExit("STOP: Paper BUY block did not match exactly.")
s = s.replace(old_buy, new_buy, 1)

# Validate the complete modified file before writing it.
ast.parse(s)

# Keep the original backup; make an additional timestamped backup too.
extra = Path(f"bot.py.pre_patch_{time.strftime('%Y%m%d_%H%M%S')}.bak")
shutil.copy2(p, extra)

temp = Path("bot.py.upgrading")
temp.write_text(s, encoding="utf-8")
temp.replace(p)

print("SUCCESS: Paper-trading upgrade applied.")
print("Original backup: bot.py.before_paper_upgrade.bak")
print("Additional backup:", extra.name)

from dataclasses import dataclass
from collections import deque
import math
import time


@dataclass
class BookSignal:
    ratio: float
    spread_bps: float
    mid: float
    bid_value: float
    ask_value: float


def analyse_book(book):
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    if not bids or not asks:
        raise ValueError("Empty order book")

    def parse_levels(levels, side):
        parsed = []
        total_value = 0.0
        for level in levels:
            if not isinstance(level, (list, tuple)) or len(level) < 2:
                raise ValueError(f"Malformed {side} level")
            try:
                price = float(level[0])
                quantity = float(level[1])
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(f"Invalid {side} price or quantity") from exc
            if (
                not math.isfinite(price)
                or not math.isfinite(quantity)
                or price <= 0
                or quantity <= 0
            ):
                raise ValueError(f"Non-positive or non-finite {side} level")
            total_value += price * quantity
            if not math.isfinite(total_value):
                raise ValueError(f"Non-finite {side} depth")
            parsed.append((price, quantity))
        return parsed, total_value

    parsed_bids, bid_value = parse_levels(bids, "bid")
    parsed_asks, ask_value = parse_levels(asks, "ask")
    total = bid_value + ask_value

    if not math.isfinite(total) or total <= 0:
        raise ValueError("Empty or invalid order-book depth")

    best_bid = parsed_bids[0][0]
    best_ask = parsed_asks[0][0]

    if best_ask <= best_bid:
        raise ValueError("Invalid/crossed order book")

    mid = (best_bid + best_ask) / 2
    ratio = 100 * bid_value / total
    spread_bps = 10000 * (best_ask - best_bid) / mid
    if not math.isfinite(ratio) or not math.isfinite(spread_bps):
        raise ValueError("Non-finite order-book signal")

    return BookSignal(
        ratio,
        spread_bps,
        mid,
        bid_value,
        ask_value,
    )

class Confirmation:
    """Require a continuously valid signal; gaps break continuity."""

    def __init__(self, seconds, max_gap_seconds=15):
        if seconds <= 0 or max_gap_seconds <= 0:
            raise ValueError("Confirmation durations must be positive")

        self.seconds = seconds
        self.max_gap_seconds = max_gap_seconds
        self.above_since = {}
        self.last_seen = {}

    def reset(self, symbol):
        self.above_since.pop(symbol, None)
        self.last_seen.pop(symbol, None)

    def reset_all(self):
        self.above_since.clear()
        self.last_seen.clear()

    def reset_unseen(self, active_symbols):
        active = set(active_symbols)
        for symbol in list(self.above_since):
            if symbol not in active:
                self.reset(symbol)

    def update(self, symbol, ratio, threshold, now=None):
        now = time.monotonic() if now is None else now

        if ratio <= threshold:
            self.reset(symbol)
            return False

        previous = self.last_seen.get(symbol)

        if previous is not None and (
            now <= previous or now - previous > self.max_gap_seconds
        ):
            self.reset(symbol)

        started = self.above_since.setdefault(symbol, now)
        self.last_seen[symbol] = now

        return now - started >= self.seconds


class ExitAnalyser:
    """Profit exits plus a separately confirmed defensive bearish exit."""

    def __init__(
        self,
        bearish_ratio=10,
        bearish_seconds=15,
        bearish_loss_pct=-0.35,
        bearish_max_gap_seconds=5,
    ):
        if not 0 < bearish_ratio < 50:
            raise ValueError("Bearish ratio must be between 0 and 50")

        if bearish_seconds < 1 or bearish_max_gap_seconds <= 0:
            raise ValueError("Invalid bearish confirmation duration")

        if bearish_loss_pct >= 0:
            raise ValueError("Bearish loss threshold must be negative")

        self.bearish_ratio = bearish_ratio
        self.bearish_seconds = bearish_seconds
        self.bearish_loss_pct = bearish_loss_pct
        self.bearish_max_gap_seconds = bearish_max_gap_seconds

        self.history = {}
        self.bearish_since = {}
        self.bearish_last_seen = {}

    def evaluate(
        self, symbol, ratio, mid, net_pct,
        min_profit=20, max_profit=40, now=None
    ):
        now = time.monotonic() if now is None else now

        hist = self.history.setdefault(symbol, deque(maxlen=12))
        hist.append((now, mid, ratio))

        # Reset bearish confirmation if observations are discontinuous.
        previous = self.bearish_last_seen.get(symbol)

        if previous is not None and (
            now <= previous
            or now - previous > self.bearish_max_gap_seconds
        ):
            self.bearish_since.pop(symbol, None)
            self.bearish_last_seen.pop(symbol, None)

        # Defensive exit: sustained weak bid depth AND sufficient net loss.
        if ratio <= self.bearish_ratio:
            started = self.bearish_since.setdefault(symbol, now)
            self.bearish_last_seen[symbol] = now

            bearish_duration = now - started

            if (
                bearish_duration >= self.bearish_seconds
                and net_pct <= self.bearish_loss_pct
            ):
                return True, "sustained_bearish_pressure"
        else:
            self.bearish_since.pop(symbol, None)
            self.bearish_last_seen.pop(symbol, None)

        # Existing profit-taking logic remains unchanged.
        if net_pct < min_profit:
            return False, "below_minimum_profit"

        prices = [x[1] for x in hist]
        ratios = [x[2] for x in hist]

        momentum = (
            (prices[-1] / prices[0] - 1) * 100
            if len(prices) > 1 and prices[0]
            else 0
        )

        weakening = (
            len(ratios) >= 3
            and ratios[-1] < ratios[0] - 3
        )

        if net_pct >= max_profit:
            return True, "upper_profit_limit"

        if weakening and net_pct >= min_profit:
            return True, "buy_pressure_weakening"

        if (
            len(ratios) >= 4
            and max(ratios[-4:]) - min(ratios[-4:]) <= 1.0
            and net_pct >= min_profit
            and momentum <= 0.15
        ):
            return True, "momentum_stalling"

        if (
            len(ratios) >= 2
            and ratios[-1] < 50
            and net_pct >= min_profit
        ):
            return True, "orderbook_reversal"

        return False, "hold_for_continuation"

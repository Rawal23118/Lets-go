
import math
import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    api_key: str = os.getenv("BITGET_API_KEY", "")
    api_secret: str = os.getenv("BITGET_API_SECRET", "")
    passphrase: str = os.getenv("BITGET_API_PASSPHRASE", "")

    mode: str = os.getenv("TRADING_MODE", "paper").lower()
    live_confirmation: str = os.getenv("LIVE_CONFIRMATION", "")
    required_confirmation: str = os.getenv(
        "LIVE_CONFIRMATION_REQUIRED",
        "I_CONFIRM_SUBACCOUNT_SPOT_LIVE_TRADING"
    )

    account_role: str = os.getenv("BITGET_ACCOUNT_ROLE", "").lower()
    subaccount_isolated: str = os.getenv(
        "BITGET_SUBACCOUNT_ISOLATED", ""
    ).upper()

    allocation: float = float(os.getenv("MAX_ALLOCATION_USDT", "20"))
    buy_ratio: float = float(os.getenv("MIN_BUY_RATIO", "90"))
    confirm_seconds: int = 180

    book_levels: int = int(os.getenv("BOOK_LEVELS", "15"))
    top_symbols: int = int(os.getenv("TOP_SYMBOLS", "30"))

    scan_interval: float = float(os.getenv("SCAN_INTERVAL_SECONDS", "1"))
    max_book_age_ms: int = int(os.getenv("MAX_BOOK_AGE_MS", "3000"))
    max_spread_bps: float = float(os.getenv("MAX_SPREAD_BPS", "35"))

    min_quote_volume: float = float(
        os.getenv("MIN_24H_QUOTE_VOLUME_USDT", "10000")
    )

    max_age_minutes: int = int(
        os.getenv("MAX_POSITION_AGE_MINUTES", "240")
    )
    emergency_stop_pct: float = float(
        os.getenv("EMERGENCY_STOP_NET_PCT", "-8")
    )

    defensive_exit_ratio: float = float(
        os.getenv("DEFENSIVE_EXIT_RATIO", "10")
    )
    defensive_exit_seconds: int = int(
        os.getenv("DEFENSIVE_EXIT_SECONDS", "15")
    )
    defensive_exit_loss_pct: float = float(
        os.getenv("DEFENSIVE_EXIT_LOSS_PCT", "-0.35")
    )

    min_take_profit: float = float(
        os.getenv("MIN_NET_PROFIT_TO_TAKE", "10")
    )
    max_take_profit: float = float(
        os.getenv("MAX_NET_PROFIT_TARGET", "40")
    )

    fee_rate: float = float(os.getenv("FEE_RATE_ESTIMATE", "0.001"))
    state_db: str = os.getenv("STATE_DB", "bot_state.sqlite3")

    def validate(self):
        if self.mode not in ("paper", "live"):
            raise ValueError("TRADING_MODE must be paper or live")

        finite_values = {
            "MAX_ALLOCATION_USDT": self.allocation,
            "MIN_BUY_RATIO": self.buy_ratio,
            "SCAN_INTERVAL_SECONDS": self.scan_interval,
            "MAX_SPREAD_BPS": self.max_spread_bps,
            "MIN_24H_QUOTE_VOLUME_USDT": self.min_quote_volume,
            "EMERGENCY_STOP_NET_PCT": self.emergency_stop_pct,
            "DEFENSIVE_EXIT_RATIO": self.defensive_exit_ratio,
            "DEFENSIVE_EXIT_LOSS_PCT": self.defensive_exit_loss_pct,
            "MIN_NET_PROFIT_TO_TAKE": self.min_take_profit,
            "MAX_NET_PROFIT_TARGET": self.max_take_profit,
            "FEE_RATE_ESTIMATE": self.fee_rate,
        }
        invalid = [
            name for name, value in finite_values.items()
            if not math.isfinite(value)
        ]
        if invalid:
            raise ValueError(
                "Configuration contains non-finite numeric values: "
                + ", ".join(invalid)
            )

        if not 0 < self.allocation <= 20:
            raise ValueError(
                "MAX_ALLOCATION_USDT must be greater than 0 "
                "and no more than the hard 20 USDT cap"
            )

        if not 0 < self.buy_ratio <= 100:
            raise ValueError("MIN_BUY_RATIO must be between 0 and 100")

        if self.confirm_seconds < 1:
            raise ValueError("CONFIRM_SECONDS must be at least 1")

        if not 1 <= self.top_symbols <= 1000:
            raise ValueError("TOP_SYMBOLS must be between 1 and 1000")

        if not 1 <= self.book_levels <= 50:
            raise ValueError("BOOK_LEVELS must be between 1 and 50")

        if (
            self.scan_interval <= 0
            or self.max_book_age_ms < 250
            or self.max_spread_bps <= 0
            or self.min_quote_volume <= 0
            or self.max_age_minutes < 1
        ):
            raise ValueError(
                "Invalid scan interval, freshness, spread, volume, or holding-time limit"
            )

        if not 0 < self.min_take_profit < self.max_take_profit <= 40:
            raise ValueError(
                "Profit band must be positive, increasing, "
                "and capped at 40%"
            )

        if self.fee_rate < 0 or self.fee_rate >= 0.1:
            raise ValueError("Invalid fee estimate")

        if not 0 < self.defensive_exit_ratio < 50:
            raise ValueError(
                "DEFENSIVE_EXIT_RATIO must be greater than 0 and below 50"
            )

        if self.defensive_exit_seconds < 1:
            raise ValueError("DEFENSIVE_EXIT_SECONDS must be at least 1")

        if not self.emergency_stop_pct < self.defensive_exit_loss_pct < 0:
            raise ValueError(
                "Defensive exit loss must be negative and less severe "
                "than the emergency stop"
            )

        if self.mode == "live":
            if not all((self.api_key, self.api_secret, self.passphrase)):
                raise ValueError(
                    "Live mode requires all three Bitget subaccount API credentials"
                )

            if self.account_role != "subaccount":
                raise ValueError(
                    "Live mode requires BITGET_ACCOUNT_ROLE=subaccount"
                )

            if self.subaccount_isolated != "YES":
                raise ValueError(
                    "Live mode requires BITGET_SUBACCOUNT_ISOLATED=YES; "
                    "do not manually trade this account"
                )

            if self.live_confirmation != self.required_confirmation:
                raise ValueError(
                    "Live mode confirmation phrase does not match "
                    "LIVE_CONFIRMATION_REQUIRED"
                )

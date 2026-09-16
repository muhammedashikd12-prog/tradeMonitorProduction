"""
Central, mutable settings. Loaded from .env at boot, but every threshold is
also re-writable at runtime via the /settings endpoint (persisted to SQLite)
so the "make thresholds configurable" requirement is real, not just env vars.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    # --- Fyers ---
    fyers_app_id: str = ""
    fyers_secret_id: str = ""
    fyers_redirect_uri: str = "https://127.0.0.1:8000/fyers/callback"
    fyers_access_token: str = ""

    # --- Capital / risk ---
    total_capital: float = 80_000
    max_risk_per_trade_pct: float = 1.0
    max_daily_loss_pct: float = 1.5
    drawdown_stop_pct: float = 15.0

    # --- Strategy ---
    default_wing_widths: str = "100,150,200,250"
    preferred_premium_min: float = 8.0
    preferred_premium_max: float = 10.0

    # NIFTY lot size changes periodically via NSE circulars (was 50, then 25,
    # then 75 as of 2025) — this is the single source of truth used
    # throughout the app. get_lot_size() in fyers_client.py will try to
    # confirm/override this from Fyers' Symbol Master feed at runtime, but
    # VERIFY this default against https://public.fyers.in/sym_details/NSE_FO.csv
    # (or nseindia.com) before trading live.
    nifty_lot_size: int = 75

    # --- IV regime thresholds (%), configurable, NOT hardcoded truth ---
    iv_low_max: float = 13.0
    iv_preferred_max: float = 18.0
    iv_high_max: float = 22.0

    # --- Stop loss / profit booking ---
    sl_multiple_of_credit: float = 3.0   # advance SL loss = max profit x this multiplier
    profit_booking_pct: float = 75.0     # evaluate exit once this % of credit is captured

    # --- Time-of-day windows (IST, 24h "HH:MM") ---
    no_entry_before: str = "09:30"
    preferred_entry_end: str = "10:30"
    good_entry_end: str = "11:30"
    selective_entry_end: str = "13:30"
    very_selective_entry_end: str = "14:00"

    # --- Score weighting (must sum to 100; validated at runtime) ---
    weight_market_regime: int = 20
    weight_iv_environment: int = 15
    weight_expected_move: int = 15
    weight_strike_quality: int = 15
    weight_price_action: int = 10
    weight_oi: int = 10
    weight_liquidity: int = 5
    weight_time: int = 5
    weight_risk_reward: int = 5

    # --- Score thresholds ---
    score_high_quality: int = 80
    score_acceptable: int = 65
    score_wait: int = 50

    # --- Operating mode: analysis | alert | execution ---
    operating_mode: str = "analysis"
    execution_mode: str = "off"

    @property
    def wing_widths(self) -> List[int]:
        return [int(x) for x in self.default_wing_widths.split(",") if x]

    def weight_sum_valid(self) -> bool:
        total = (
            self.weight_market_regime + self.weight_iv_environment +
            self.weight_expected_move + self.weight_strike_quality +
            self.weight_price_action + self.weight_oi + self.weight_liquidity +
            self.weight_time + self.weight_risk_reward
        )
        return total == 100


settings = Settings()

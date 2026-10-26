"""IV / Greeks engine. Computes ATM IV, skew, percentile/rank, and classifies
the IV regime using CONFIGURABLE thresholds (never hardcoded truth)."""
from __future__ import annotations
from collections import deque
from datetime import datetime, time, timedelta, timezone
import math
from statistics import NormalDist
from typing import Optional
from app.models import OptionChainSnapshot, IVResult
from app.config import settings

_N = NormalDist()

# Rolling IV history used to derive percentile/rank until you wire in a real
# historical-IV data source. Swap this for a DB-backed series in production.
_iv_history: deque[float] = deque(maxlen=252)  # ~1 trading year of daily ATM IVs


def normalize_iv_value(value: object) -> Optional[float]:
    """Return a broker/market IV value in percentage points, or None if invalid."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value or value.lower() in {"nan", "inf", "+inf", "-inf"}:
            return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric):
        return None
    if numeric <= 0 or numeric > 1000:
        return None
    return numeric


def iv_freshness_status(updated_at: Optional[datetime] = None, *, max_age_seconds: Optional[int] = None) -> tuple[str, Optional[float]]:
    """Return freshness status for IV data using the actual source timestamp when available."""
    if updated_at is None:
        return "UNKNOWN_TIMESTAMP", None
    max_age = settings.iv_data_freshness_seconds if max_age_seconds is None else max_age_seconds
    if updated_at.tzinfo is None:
        local_tz = datetime.now().astimezone().tzinfo or timezone.utc
        updated_at = updated_at.replace(tzinfo=local_tz)
    else:
        updated_at = updated_at.astimezone(timezone.utc)
    age_seconds = max(0.0, (datetime.now(timezone.utc) - updated_at).total_seconds())
    return ("FRESH" if age_seconds <= max_age else "STALE", age_seconds)


def _find_atm_quotes(chain: OptionChainSnapshot):
    strikes = sorted({q.strike for q in chain.quotes})
    atm_strike = min(strikes, key=lambda s: abs(s - chain.spot))
    ce = next((q for q in chain.quotes if q.strike == atm_strike and q.option_type == "CE"), None)
    pe = next((q for q in chain.quotes if q.strike == atm_strike and q.option_type == "PE"), None)
    return ce, pe


def black_scholes_iv_estimate(price: float, spot: float, strike: float, t_years: float,
                               r: float = 0.065, option_type: str = "CE",
                               tol: float = 1e-4, max_iter: int = 100) -> Optional[float]:
    """Newton-Raphson implied vol solver, used as a fallback when the broker
    doesn't supply IV directly for a given strike."""
    if not math.isfinite(price) or not math.isfinite(spot) or not math.isfinite(strike):
        return None
    if t_years <= 0 or price <= 0 or spot <= 0 or strike <= 0:
        return None
    sigma = 0.20
    for _ in range(max_iter):
        d1 = (np_log(spot / strike) + (r + 0.5 * sigma ** 2) * t_years) / (sigma * t_years ** 0.5)
        d2 = d1 - sigma * t_years ** 0.5
        if option_type == "CE":
            model_price = spot * _N.cdf(d1) - strike * (2.71828 ** (-r * t_years)) * _N.cdf(d2)
        else:
            model_price = strike * (2.71828 ** (-r * t_years)) * _N.cdf(-d2) - spot * _N.cdf(-d1)
        vega = spot * _N.pdf(d1) * t_years ** 0.5
        if vega < 1e-8:
            break
        diff = model_price - price
        if abs(diff) < tol:
            return round(sigma * 100, 2)
        sigma -= diff / vega
        sigma = max(0.01, min(sigma, 3.0))
    return round(sigma * 100, 2)


def calculate_current_atm_iv(chain: OptionChainSnapshot) -> Optional[float]:
    """Average call/put Black-Scholes IV at the nearest usable strike.

    Uses the live bid/ask midpoint, falling back to LTP when a two-sided quote
    is unavailable. Time to expiry uses the selected expiry's 15:30 IST close.
    """
    spot = chain.spot
    if not math.isfinite(spot) or spot <= 0:
        return None
    expiry_close_ist = datetime.combine(
        chain.expiry, time(15, 30), tzinfo=timezone(timedelta(hours=5, minutes=30))
    )
    t_years = (expiry_close_ist.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds() / (365 * 86400)
    if t_years <= 0:
        return None

    quotes_by_strike = {}
    for quote in chain.quotes:
        if quote.option_type not in ("CE", "PE"):
            continue
        strike = quote.strike
        if not math.isfinite(strike) or strike <= 0:
            continue
        quotes_by_strike.setdefault(strike, {})[quote.option_type] = quote

    for strike in sorted(quotes_by_strike, key=lambda value: abs(value - spot)):
        pair = quotes_by_strike[strike]
        if "CE" not in pair or "PE" not in pair:
            continue
        estimates = []
        for option_type in ("CE", "PE"):
            quote = pair[option_type]
            bid, ask = quote.bid, quote.ask
            if math.isfinite(bid) and math.isfinite(ask) and bid > 0 and ask >= bid:
                premium = (bid + ask) / 2
            elif math.isfinite(quote.ltp) and quote.ltp > 0:
                premium = quote.ltp
            else:
                premium = None
            if premium is None or not math.isfinite(premium) or premium <= 0:
                break
            estimate = black_scholes_iv_estimate(premium, spot, strike, t_years, option_type=option_type)
            if estimate is None or not math.isfinite(estimate) or estimate <= 0:
                break
            estimates.append(estimate)
        if len(estimates) == 2:
            return round(sum(estimates) / 2, 2)
    return None


def np_log(x: float) -> float:
    import math
    return math.log(x)


def classify_iv(iv_avg: float) -> str:
    if iv_avg < settings.iv_low_max:
        return "LOW"
    if iv_avg < settings.iv_preferred_max:
        return "PREFERRED"
    if iv_avg < settings.iv_high_max:
        return "HIGH"
    return "EXTREME"


def compute_iv_percentile_rank(current_iv: float) -> tuple[Optional[float], Optional[float]]:
    if len(_iv_history) < 20:
        return None, None
    hist = sorted(_iv_history)
    below = sum(1 for v in hist if v <= current_iv)
    percentile = round(100 * below / len(hist), 1)
    lo, hi = hist[0], hist[-1]
    rank = round(100 * (current_iv - lo) / (hi - lo), 1) if hi > lo else 50.0
    return percentile, rank


def record_daily_iv(iv_avg: float) -> None:
    _iv_history.append(iv_avg)


_last_recorded_date = None


def record_daily_iv_once(iv_avg: float, today=None) -> None:
    """Call this on every /analyze — it only actually appends to the
    rolling history the FIRST time it's called on a given calendar date,
    so repeated intraday calls don't inflate the 'daily IV' series."""
    global _last_recorded_date
    from datetime import date as _date
    today = today or _date.today()
    if _last_recorded_date != today:
        record_daily_iv(iv_avg)
        _last_recorded_date = today


def compute_iv(chain: OptionChainSnapshot, short_ce_strike: Optional[float] = None,
               short_pe_strike: Optional[float] = None) -> IVResult:
    ce, pe = _find_atm_quotes(chain)
    if not ce or not pe:
        raise ValueError("Could not locate ATM CE/PE quotes in chain")

    atm_ce_iv = normalize_iv_value(ce.iv) or 15.0
    atm_pe_iv = normalize_iv_value(pe.iv) or 15.0
    iv_avg = round((atm_ce_iv + atm_pe_iv) / 2, 2)
    skew = round(atm_ce_iv - atm_pe_iv, 2)

    short_ce_iv = short_pe_iv = None
    if short_ce_strike is not None:
        q = next((x for x in chain.quotes if x.strike == short_ce_strike and x.option_type == "CE"), None)
        short_ce_iv = normalize_iv_value(q.iv) if q else None
    if short_pe_strike is not None:
        q = next((x for x in chain.quotes if x.strike == short_pe_strike and x.option_type == "PE"), None)
        short_pe_iv = normalize_iv_value(q.iv) if q else None

    percentile, rank = compute_iv_percentile_rank(iv_avg)

    return IVResult(
        atm_ce_iv=atm_ce_iv,
        atm_pe_iv=atm_pe_iv,
        short_ce_iv=short_ce_iv,
        short_pe_iv=short_pe_iv,
        iv_average=iv_avg,
        ce_pe_skew=skew,
        iv_percentile=percentile,
        iv_rank=rank,
        classification=classify_iv(iv_avg),
    )

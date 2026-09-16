"""
Fyers implementation of BrokerClient using the official `fyers-apiv3` SDK.

⚠️ Verify field names against https://myapi.fyers.in/docsv3 before going
live — brokers change response shapes. This module is structured so any
drift is isolated to this one file.

Auth flow (one-time, interactive — cannot be done headlessly):
    1. client = FyersClient(app_id, secret_id, redirect_uri)
    2. url = client.generate_login_url()          -> open in browser, log in
    3. Fyers redirects to redirect_uri?auth_code=XYZ
    4. client.exchange_auth_code("XYZ")            -> stores access_token
    5. Persist client.access_token (e.g. in .env) for reuse until it expires
       (Fyers tokens are valid for the trading day).
"""
from __future__ import annotations
import time
import threading
from datetime import datetime, date, timedelta
from typing import Optional, Callable

from app.brokers.base import BrokerClient, BrokerConnectionError
from app.models import OptionChainSnapshot, OptionQuote, OrderResult, PriceBar

try:
    from fyers_apiv3 import fyersModel
    from fyers_apiv3.FyersWebsocket import data_ws
except ImportError:
    fyersModel = None
    data_ws = None


class FyersClient(BrokerClient):
    def __init__(self, app_id: str, secret_id: str, redirect_uri: str, access_token: str = ""):
        self.app_id = app_id
        self.secret_id = secret_id
        self.redirect_uri = redirect_uri
        self.access_token = access_token
        self._connected = False
        self._last_tick_at: Optional[float] = None
        self._ws = None
        self._ws_lock = threading.Lock()
        self._reconnect_symbols: list[str] = []
        self._on_tick_cb: Optional[Callable[[dict], None]] = None
        self._expiry_timestamps: dict[str, str] = {}

        self._session = fyersModel.SessionModel(
            client_id=app_id,
            secret_key=secret_id,
            redirect_uri=redirect_uri,
            response_type="code",
            grant_type="authorization_code",
        ) if fyersModel else None

        self._fyers = None
        if access_token and fyersModel:
            self._init_fyers_model()

    # ---------- Auth ----------
    def generate_login_url(self) -> str:
        return self._session.generate_authcode()

    def exchange_auth_code(self, auth_code: str) -> str:
        self._session.set_token(auth_code)
        response = self._session.generate_token()
        if response.get("s") != "ok" or not response.get("access_token"):
            raise BrokerConnectionError(f"Fyers authentication failed: {response.get('message', response)}")
        self.access_token = response["access_token"]
        self._init_fyers_model()
        return self.access_token

    def _init_fyers_model(self):
        self._fyers = fyersModel.FyersModel(
            client_id=self.app_id, is_async=False, token=self.access_token, log_path=""
        )
        self._connected = True

    # ---------- Connection status ----------
    def is_connected(self) -> bool:
        return self._connected and self._fyers is not None

    def connection_status(self) -> str:
        if not self.is_connected():
            return "DISCONNECTED"
        if self._last_tick_at and (time.time() - self._last_tick_at) > 5:
            return "DELAYED"
        return "LIVE"

    def _require_connected(self):
        if not self.is_connected():
            raise BrokerConnectionError("Fyers session not authenticated/connected")

    # ---------- Market data ----------
    def get_ltp(self, symbol: str) -> float:
        self._require_connected()
        resp = self._fyers.quotes({"symbols": symbol})
        if resp.get("s") != "ok":
            raise BrokerConnectionError(f"Fyers quotes error: {resp}")
        return float(resp["d"][0]["v"]["lp"])

    def get_quotes(self, symbols: list[str]) -> dict[str, dict]:
        """Return the broker's latest quote fields keyed by symbol."""
        self._require_connected()
        resp = self._fyers.quotes({"symbols": ",".join(symbols)})
        if resp.get("s") != "ok":
            raise BrokerConnectionError(f"Fyers quotes error: {resp}")
        quotes = {}
        for item in resp.get("d", []):
            value = item.get("v", {})
            key = value.get("symbol") or item.get("symbol")
            if key:
                quotes[key] = value
        return quotes

    def get_nearest_expiry(self, symbol: str) -> str:
        """Fyers option-chain response includes an `expiryData` list of
        available expiries with epoch timestamps — take the nearest future one.
        Keep the ISO date as the app-facing value and the broker epoch for the
        subsequent option-chain request."""
        self._require_connected()
        resp = self._fyers.optionchain({"symbol": symbol, "strikecount": 1, "timestamp": ""})
        if resp.get("s") != "ok":
            raise BrokerConnectionError(f"Fyers option chain error: {resp}")
        expiries = resp["data"].get("expiryData", [])
        if not expiries:
            raise BrokerConnectionError("No expiry data returned by broker")
        nearest = expiries[0]
        expiry_date = datetime.strptime(nearest["date"], "%d-%m-%Y").date()
        expiry = expiry_date.isoformat()
        self._expiry_timestamps[expiry] = str(nearest["expiry"])
        return expiry

    def get_expiries(self, symbol: str) -> list[dict[str, str]]:
        """Return all broker-provided future expiries and cache their timestamps."""
        self._require_connected()
        resp = self._fyers.optionchain({"symbol": symbol, "strikecount": 1, "timestamp": ""})
        if resp.get("s") != "ok":
            raise BrokerConnectionError(f"Fyers option chain error: {resp}")
        expiries = []
        for item in resp.get("data", {}).get("expiryData", []):
            expiry_date = datetime.strptime(item["date"], "%d-%m-%Y").date()
            expiry = expiry_date.isoformat()
            self._expiry_timestamps[expiry] = str(item["expiry"])
            expiries.append({"value": expiry, "label": expiry_date.strftime("%d %b %Y")})
        if not expiries:
            raise BrokerConnectionError("No expiry data returned by broker")
        return expiries

    def get_option_chain(self, symbol: str, expiry: str) -> OptionChainSnapshot:
        self._require_connected()
        timestamp = self._expiry_timestamps.get(expiry, expiry)
        resp = self._fyers.optionchain({"symbol": symbol, "strikecount": 40, "timestamp": timestamp})
        if resp.get("s") != "ok":
            raise BrokerConnectionError(f"Fyers option chain error: {resp}")

        data = resp["data"]
        spot = float(data["optionsChain"][0]["ltp"]) if data.get("optionsChain") else 0.0
        quotes: list[OptionQuote] = []
        for row in data.get("optionsChain", []):
            if row.get("option_type") not in ("CE", "PE"):
                continue
            quotes.append(OptionQuote(
                strike=float(row["strike_price"]),
                option_type=row["option_type"],
                ltp=float(row.get("ltp", 0)),
                bid=float(row.get("bid", 0)),
                ask=float(row.get("ask", 0)),
                oi=int(row.get("oi", 0)),
                change_in_oi=int(row.get("oich", 0)),
                volume=int(row.get("volume", 0)),
                symbol=row.get("symbol"),  # broker's real tradable symbol — use this for orders/quotes
            ))
        return OptionChainSnapshot(
            lot_size=self.get_lot_size(symbol),
            underlying=symbol,
            spot=spot,
            timestamp=datetime.now(),
            expiry=date.fromisoformat(expiry) if "-" in expiry else date.today(),
            quotes=quotes,
        )

    # ---------- Account ----------
    # ---------- Lot size (best-effort, from Fyers Symbol Master) ----------
    _lot_size_cache: dict[str, tuple[float, int]] = {}

    def get_lot_size(self, symbol: str, underlying_hint: str = "NIFTY") -> int:
        """Attempts to confirm the current lot size from Fyers' publicly
        published Symbol Master CSV (updated by Fyers when NSE changes lot
        sizes). Falls back to settings.nifty_lot_size on ANY failure —
        parsing, network, or unexpected column layout — since a wrong guess
        here would silently mis-size every trade.

        ⚠️ The column layout below (index 3 = min lot size, index 13 =
        underlying symbol) was inferred from community-documented CSV
        samples, not Fyers' official schema — verify it once against
        https://public.fyers.in/sym_details/NSE_FO.csv for your instrument
        before relying on this over the .env default.
        """
        cache_key = underlying_hint
        cached = self._lot_size_cache.get(cache_key)
        if cached and (time.time() - cached[0]) < 3600:  # refresh at most hourly
            return cached[1]

        from app.config import settings
        fallback = settings.nifty_lot_size
        try:
            import requests
            resp = requests.get("https://public.fyers.in/sym_details/NSE_FO.csv", timeout=5)
            resp.raise_for_status()
            for line in resp.text.splitlines():
                cols = line.split(",")
                if len(cols) < 14:
                    continue
                underlying = cols[13].strip()
                if underlying == underlying_hint:
                    lot = int(float(cols[3]))
                    if lot > 0:
                        self._lot_size_cache[cache_key] = (time.time(), lot)
                        return lot
        except Exception:
            pass  # any failure -> fall back silently, never raise into a hot path

        self._lot_size_cache[cache_key] = (time.time(), fallback)
        return fallback

    def get_margin_available(self) -> float:
        self._require_connected()
        resp = self._fyers.funds()
        if resp.get("s") != "ok":
            raise BrokerConnectionError(f"Fyers funds error: {resp}")
        for item in resp.get("fund_limit", []):
            if item.get("title") == "Available Balance":
                return float(item.get("equityAmount", 0))
        return 0.0

    def get_positions(self) -> list[dict]:
        self._require_connected()
        resp = self._fyers.positions()
        if resp.get("s") != "ok":
            raise BrokerConnectionError(f"Fyers positions error: {resp}")
        return resp.get("netPositions", [])

    def get_basket_margin(self, orders: list[dict]) -> dict:
        """Ask Fyers for multi-order/SPAN margin before a basket is sent.

        This intentionally uses the same four order payloads that will be
        submitted.  A caller must treat failures as unavailable, never as a
        zero-margin approval.
        """
        self._require_connected()
        import requests
        response = requests.post(
            "https://api-t1.fyers.in/api/v3/multiorder/margin",
            headers={"Authorization": f"{self.app_id}:{self.access_token}", "Content-Type": "application/json"},
            json={"data": orders}, timeout=10,
        )
        if not response.ok:
            raise BrokerConnectionError(f"Fyers margin calculator error: HTTP {response.status_code}")
        payload = response.json()
        if payload.get("s") != "ok" or not payload.get("data"):
            raise BrokerConnectionError(f"Fyers margin calculator error: {payload.get('message', payload)}")
        return payload["data"]

    def place_basket_orders(self, orders: list[dict]) -> dict:
        """Place an already-reviewed basket. Never call this for paper mode."""
        self._require_connected()
        response = self._fyers.place_basket_orders({"data": orders})
        if response.get("s") != "ok":
            raise BrokerConnectionError(f"Fyers basket order rejected: {response.get('message', response)}")
        return response

    # ---------- Historical market data ----------
    def _fetch_candles(self, symbol: str, resolution: str, range_from: date, range_to: date) -> list[PriceBar]:
        self._require_connected()
        payload = {
            "symbol": symbol,
            "resolution": resolution,
            "date_format": "1",
            "range_from": range_from.strftime("%Y-%m-%d"),
            "range_to": range_to.strftime("%Y-%m-%d"),
            "cont_flag": "1",
        }
        resp = self._fyers.history(payload)
        if resp.get("s") != "ok":
            raise BrokerConnectionError(f"Fyers history error: {resp}")
        bars = []
        for row in resp.get("candles", []):
            ts, o, h, l, c, v = row
            bars.append(PriceBar(timestamp=datetime.fromtimestamp(ts), open=o, high=h, low=l, close=c, volume=int(v)))
        return bars

    def get_intraday_candles(self, symbol: str, resolution_minutes: int = 5) -> list[PriceBar]:
        today = date.today()
        bars = self._fetch_candles(symbol, str(resolution_minutes), today, today)
        if not bars:
            raise BrokerConnectionError("No intraday candles returned — market may not have opened yet")
        return bars

    def get_india_vix(self) -> float:
        return self.get_ltp("NSE:INDIAVIX-INDEX")

    def get_prev_day_high_low(self, symbol: str) -> tuple[float, float]:
        # Pull a small window of daily candles and take the last one BEFORE today
        today = date.today()
        window_start = today - timedelta(days=10)
        bars = self._fetch_candles(symbol, "D", window_start, today)
        completed = [b for b in bars if b.timestamp.date() < today]
        if not completed:
            raise BrokerConnectionError("No completed prior daily candle available")
        prev_day = completed[-1]
        return prev_day.high, prev_day.low

    # ---------- Orders ----------
    def place_order(self, symbol, side, quantity, order_type="MARKET", limit_price=None, tag=None) -> OrderResult:
        self._require_connected()
        payload = {
            "symbol": symbol,
            "qty": quantity,
            "type": 2 if order_type == "MARKET" else 1,  # 2=Market, 1=Limit per Fyers spec
            "side": 1 if side == "BUY" else -1,
            "productType": "MARGIN",
            "limitPrice": limit_price or 0,
            "stopPrice": 0,
            "validity": "DAY",
            "disclosedQty": 0,
            "offlineOrder": False,
            "orderTag": tag or "condor_ai",
        }
        resp = self._fyers.place_order(payload)
        if resp.get("s") != "ok":
            return OrderResult(order_id=None, status="REJECTED", raw=resp)
        return OrderResult(order_id=str(resp.get("id")), status="PENDING", raw=resp)

    def get_order_status(self, order_id: str) -> OrderResult:
        self._require_connected()
        resp = self._fyers.orderbook({"id": order_id})
        if resp.get("s") != "ok" or not resp.get("orderBook"):
            return OrderResult(order_id=order_id, status="UNKNOWN", raw=resp)
        order = resp["orderBook"][0]
        status_map = {1: "PENDING", 2: "FILLED", 5: "REJECTED", 6: "PENDING"}
        status = status_map.get(order.get("status"), "UNKNOWN")
        filled_price = float(order.get("tradedPrice", 0)) or None
        return OrderResult(order_id=order_id, status=status, filled_price=filled_price, raw=order)

    # ---------- WebSocket with auto-reconnect ----------
    def subscribe_ticks(self, symbols: list[str], on_tick: Callable[[dict], None]) -> None:
        self._require_connected()
        self._on_tick_cb = on_tick
        self._reconnect_symbols = symbols
        self._connect_ws()

    def _connect_ws(self):
        def on_message(msg):
            self._last_tick_at = time.time()
            if self._on_tick_cb:
                self._on_tick_cb(msg)

        def on_error(msg):
            self._schedule_reconnect()

        def on_close(msg):
            self._schedule_reconnect()

        def on_open():
            self._ws.subscribe(symbols=self._reconnect_symbols, data_type="SymbolUpdate")

        with self._ws_lock:
            self._ws = data_ws.FyersDataSocket(
                access_token=f"{self.app_id}:{self.access_token}",
                log_path="",
                litemode=False,
                write_to_file=False,
                reconnect=True,
                on_connect=on_open,
                on_close=on_close,
                on_error=on_error,
                on_message=on_message,
            )
            threading.Thread(target=self._ws.connect, daemon=True).start()

    def _schedule_reconnect(self):
        self._connected = False
        time.sleep(3)
        try:
            self._connect_ws()
            self._connected = True
        except Exception:
            threading.Timer(5.0, self._schedule_reconnect).start()

    def unsubscribe_ticks(self, symbols: list[str]) -> None:
        if self._ws:
            self._ws.unsubscribe(symbols=symbols)

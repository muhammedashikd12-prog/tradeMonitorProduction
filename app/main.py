"""FastAPI entry point. Wires broker + all engines into REST endpoints and
a WebSocket broadcast for the frontend dashboard."""
from __future__ import annotations
import asyncio
import json
from pathlib import Path
from datetime import datetime, date, time as date_time
from typing import Literal
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_session, export_journal_csv, SessionLocal, PositionHistoryEntry
from app.brokers.fyers_client import FyersClient
from app.brokers.base import BrokerConnectionError
from app.models import IronCondorStructure
from pydantic import BaseModel, Field
from app.services import strategy_engine, position_sizing, daily_loss_drawdown
from app.services import risk_engine, stop_loss_engine, profit_booking_engine
from app.services import journal_service, notification_service, execution_engine
from app.services import market_data_service
from app.services.iron_condor_calculations import (
    calculate_position, calculate_fund_requirement, calculate_paper_execution_price,
    calculate_position_charges, calculate_advance_sl, advance_sl_state,
)

app = FastAPI(title="Condor AI")

# Local frontend (opened as a file or served on any localhost port) needs
# CORS to call this API from a different origin during development.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

broker = FyersClient(
    app_id=settings.fyers_app_id,
    secret_id=settings.fyers_secret_id,
    redirect_uri=settings.fyers_redirect_uri,
    access_token=settings.fyers_access_token,
)
exec_engine = execution_engine.ExecutionEngine(broker, symbol_prefix="NSE:NIFTY")

# In-memory holder for the currently open structure (single active position
# at a time, per the spec's "one trade card -> live monitor" flow). Persist
# this to DB if you need multi-position support.
_active_structure: IronCondorStructure | None = None
_active_expiry: str | None = None
_active_quantity: int = 0  # set from PositionSize.quantity when a trade is actually filled


class ManualLeg(BaseModel):
    leg: Literal["CALL BUY", "CALL SELL", "PUT SELL", "PUT BUY"]
    symbol: str = Field(min_length=1)
    strike: float = Field(gt=0)
    quantity: int = Field(gt=0)
    entry_price: float = Field(gt=0)
    order_type: Literal["MARKET", "LIMIT"] = "MARKET"


class ManualMonitorSetup(BaseModel):
    expiry: date
    legs: list[ManualLeg] = Field(min_length=4, max_length=4)
    call_sl: float | None = Field(default=None, ge=0)
    put_sl: float | None = Field(default=None, ge=0)
    mode: Literal["LIVE", "PAPER", "MARKET"] = "LIVE"
    slippage_points: float = Field(default=0.05, ge=0, le=2.0)


class BasketPlacement(ManualMonitorSetup):
    confirmed: bool = False


_manual_monitors: dict[str, ManualMonitorSetup] = {}
_manual_previous_spots: dict[str, float | None] = {}
_manual_previous_ivs: dict[str, float | None] = {}
_manual_active_history_ids: dict[str, str | None] = {}
# Compatibility request context used by the existing snapshot calculation.
_manual_monitor: ManualMonitorSetup | None = None
_manual_previous_spot: float | None = None
_manual_previous_iv: float | None = None
_manual_active_history_id: str | None = None


def _normalise_mode(mode: str) -> str:
    return "PAPER" if mode == "PAPER" else "LIVE"


def _activate_manual_mode(mode: str) -> str:
    global _manual_monitor, _manual_previous_spot, _manual_previous_iv, _manual_active_history_id
    key = _normalise_mode(mode)
    _manual_monitor = _manual_monitors.get(key)
    _manual_previous_spot = _manual_previous_spots.get(key)
    _manual_previous_iv = _manual_previous_ivs.get(key)
    _manual_active_history_id = _manual_active_history_ids.get(key)
    return key


def _save_manual_mode(key: str) -> None:
    if _manual_monitor is None:
        _manual_monitors.pop(key, None)
    else:
        _manual_monitors[key] = _manual_monitor
    _manual_previous_spots[key] = _manual_previous_spot
    _manual_previous_ivs[key] = _manual_previous_iv
    _manual_active_history_ids[key] = _manual_active_history_id

_ws_clients: list[WebSocket] = []
_main_loop: asyncio.AbstractEventLoop | None = None


@app.on_event("startup")
async def _capture_loop():
    global _main_loop
    _main_loop = asyncio.get_running_loop()


def broadcast_sync(payload: dict) -> None:
    """Safe to call from sync endpoint functions, which FastAPI runs in a
    worker thread with no running event loop of their own."""
    if _main_loop is not None:
        asyncio.run_coroutine_threadsafe(_broadcast(payload), _main_loop)


# ---------------- Auth ----------------
@app.get("/fyers/login-url")
def fyers_login_url():
    return {"url": broker.generate_login_url()}


@app.get("/fyers/callback")
def fyers_callback(auth_code: str):
    try:
        token = broker.exchange_auth_code(auth_code)
    except BrokerConnectionError:
        return RedirectResponse(url="/?connected=0")
    return RedirectResponse(url="/?connected=1" if token else "/?connected=0")


# ---------------- Status ----------------
@app.get("/status")
def status():
    return {
        "broker_status": broker.connection_status(),
        "operating_mode": settings.operating_mode,
        "execution_mode": settings.execution_mode,
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/manual-monitor/setup")
def setup_manual_monitor(payload: ManualMonitorSetup):
    global _manual_monitor, _manual_previous_spot, _manual_previous_iv, _manual_active_history_id
    mode_key = _activate_manual_mode(payload.mode)
    payload.mode = mode_key
    expected = {"CALL BUY", "CALL SELL", "PUT SELL", "PUT BUY"}
    supplied = {leg.leg for leg in payload.legs}
    if supplied != expected:
        raise HTTPException(status_code=422, detail="Enter exactly one CALL BUY, CALL SELL, PUT SELL, and PUT BUY leg")
    if len(payload.legs) != len(supplied):
        raise HTTPException(status_code=422, detail="Each leg must be entered once")
    if payload.expiry < date.today():
        raise HTTPException(status_code=422, detail="Expiry must be today or a future expiry")
    if any(not leg.symbol.startswith("NSE:") for leg in payload.legs):
        raise HTTPException(status_code=422, detail="Each leg must use a valid Fyers NSE symbol")
    if any(leg.quantity <= 0 or leg.entry_price <= 0 or leg.strike <= 0 for leg in payload.legs):
        raise HTTPException(status_code=422, detail="Every leg requires a valid strike, quantity, and positive entry price")
    connected = broker.is_connected()
    quotes = broker.get_quotes(["NSE:NIFTY50-INDEX"] + [leg.symbol for leg in payload.legs]) if connected else {}
    if not connected or any(_quote_value(quotes.get(leg.symbol, {}), "lp") is None for leg in payload.legs):
        raise HTTPException(status_code=503, detail="A live Fyers quote is required for each leg before monitoring can start")
    # Keep the single-active-position contract explicit: a replacement is
    # stopped and retained in history only after the new setup is valid.
    if _manual_monitor is not None:
        _close_active_history("Replaced by newly saved position", status="STOPPED")
    # Paper entries are immutable simulated fills.  They use the live Fyers
    # quote, but never use the broker order APIs.  Market mode retains the
    # user-entered/actual entry values exactly as it did before.
    requested_entries = {leg.leg: leg.entry_price for leg in payload.legs}
    if payload.mode == "PAPER":
        for leg in payload.legs:
            leg.entry_price = calculate_paper_execution_price(leg, quotes.get(leg.symbol), payload.slippage_points)
    _manual_monitor = payload
    _manual_previous_spot = None
    _manual_previous_iv = None
    spot = _quote_value(quotes.get("NSE:NIFTY50-INDEX", {}), "lp")
    _manual_active_history_id = _persist_manual_position_entry(
        payload, {leg.leg: _quote_value(quotes.get(leg.symbol, {}), "lp") for leg in payload.legs}, spot, quotes,
        requested_entries=requested_entries,
    )
    _save_manual_mode(mode_key)
    return {"saved": True, "expiry": payload.expiry.isoformat(), "mode": payload.mode, "position_id": _manual_active_history_id,
            "advance_sl": _advance_sl_payload({leg.leg: leg for leg in payload.legs}, spot),
            "legs": [leg.model_dump() for leg in payload.legs]}


@app.delete("/manual-monitor/setup")
def clear_manual_monitor(mode: str = "LIVE"):
    global _manual_monitor, _manual_previous_spot, _manual_previous_iv, _manual_active_history_id
    mode_key = _activate_manual_mode(mode)
    reason = "Cleared by user"
    if _manual_monitor is not None:
        _close_active_history(reason, status="STOPPED", final_spot=None)
    _manual_monitor = None
    _manual_previous_spot = None
    _manual_previous_iv = None
    _manual_active_history_id = None
    _save_manual_mode(mode_key)
    return {"saved": False, "history_kept": True}


@app.post("/manual-monitor/close")
def close_manual_monitor(reason: str = "MANUALLY CLOSED", mode: str = "LIVE"):
    global _manual_monitor, _manual_previous_spot, _manual_previous_iv, _manual_active_history_id
    mode_key = _activate_manual_mode(mode)
    final_spot = None
    if _manual_monitor is not None and broker.is_connected():
        spot_quote = broker.get_quotes(["NSE:NIFTY50-INDEX"]).get("NSE:NIFTY50-INDEX", {})
        final_spot = _quote_value(spot_quote, "lp")
    _close_active_history(reason, status="MANUALLY CLOSED" if reason else "STOPPED", final_spot=final_spot)
    _manual_monitor = None
    _manual_previous_spot = None
    _manual_previous_iv = None
    _manual_active_history_id = None
    _save_manual_mode(mode_key)
    return {"saved": False, "closed": True, "history_kept": True}


@app.get("/manual-monitor/history")
def manual_history():
    db = SessionLocal()
    rows = db.query(PositionHistoryEntry).order_by(PositionHistoryEntry.created_at.desc()).all()
    db.close()
    return [_coerce_history_entry(row) for row in rows]


@app.delete("/manual-monitor/history/{position_id}")
def delete_manual_history(position_id: str):
    """Delete only the local history row; never closes a broker position."""
    db = SessionLocal()
    try:
        row = db.query(PositionHistoryEntry).filter_by(position_id=position_id).first()
        if row is None:
            raise HTTPException(status_code=404, detail="Trading history record not found")
        db.delete(row)
        db.commit()
        return {"deleted": True, "position_id": position_id}
    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Unable to delete trading history: {exc}")
    finally:
        db.close()


def _quote_value(quote: dict, *keys: str):
    for key in keys:
        if quote.get(key) is not None:
            return quote[key]
    return None


def _advance_sl_payload(legs: dict[str, ManualLeg], spot: float | None = None) -> dict:
    result = calculate_advance_sl(legs)
    output = {}
    for name, side in (("call", "CALL"), ("put", "PUT")):
        item = result[name]
        trigger = item["sl_trigger_price"]
        output[name] = item | {
            "side": side,
            "distance": (trigger - spot if side == "CALL" else spot - trigger) if trigger is not None and spot is not None else None,
        }
    return {"sl_multiplier": settings.sl_multiple_of_credit, "call": output["call"], "put": output["put"]}


@app.post("/manual-monitor/preview")
def preview_manual_monitor(payload: ManualMonitorSetup):
    """Return advance SL values while the four-leg ticket is being edited."""
    by_name = {leg.leg: leg for leg in payload.legs}
    expected = {"CALL BUY", "CALL SELL", "PUT SELL", "PUT BUY"}
    if set(by_name) != expected or len(payload.legs) != 4:
        return {"available": False, "message": "Enter all four legs to calculate SL"}
    spot = None
    if broker.is_connected():
        try:
            spot = _quote_value(broker.get_quotes(["NSE:NIFTY50-INDEX"]).get("NSE:NIFTY50-INDEX", {}), "lp")
        except BrokerConnectionError:
            pass
    return {"available": True, "spot": spot, **_advance_sl_payload(by_name, spot)}


def _fyers_basket_orders(legs: list[ManualLeg]) -> list[dict]:
    """Translate the displayed ticket to the exact broker basket payload."""
    by_name = {leg.leg: leg for leg in legs}
    return [{
        "symbol": leg.symbol, "qty": leg.quantity, "type": 2 if leg.order_type == "MARKET" else 1,
        "side": 1 if "BUY" in leg.leg else -1, "productType": "MARGIN",
        "limitPrice": leg.entry_price if leg.order_type == "LIMIT" else 0,
        "stopPrice": 0, "validity": "DAY", "disclosedQty": 0,
        "offlineOrder": False, "orderTag": "condor_basket",
    } for leg in (by_name["CALL BUY"], by_name["PUT BUY"], by_name["CALL SELL"], by_name["PUT SELL"])]


def _basket_preview(payload: ManualMonitorSetup) -> dict:
    """Build one quote-backed basket preview; this performs no order action."""
    expected = {"CALL BUY", "CALL SELL", "PUT SELL", "PUT BUY"}
    if {leg.leg for leg in payload.legs} != expected or len(payload.legs) != 4:
        raise HTTPException(status_code=422, detail="A basket requires exactly four unique Iron Condor legs")
    if not broker.is_connected():
        raise HTTPException(status_code=503, detail="FYERS must be LIVE to review a basket")
    quotes = broker.get_quotes(["NSE:NIFTY50-INDEX"] + [leg.symbol for leg in payload.legs])
    if any(_quote_value(quotes.get(leg.symbol, {}), "lp") is None for leg in payload.legs):
        raise HTTPException(status_code=503, detail="A fresh live quote is required for every basket leg")
    # Use executable sides for the preview, never the stale form default.
    preview_legs = []
    for leg in payload.legs:
        quote = quotes[leg.symbol]
        estimated = calculate_paper_execution_price(leg, quote, 0)
        preview_legs.append(ManualLeg(**(leg.model_dump() | {"entry_price": estimated})))
    by_name = {leg.leg: leg for leg in preview_legs}
    calculations = calculate_position(by_name, {leg.leg: _quote_value(quotes[leg.symbol], "lp") for leg in preview_legs})
    estimate = calculate_fund_requirement(by_name, mode="LIVE")
    orders = _fyers_basket_orders(preview_legs)
    broker_margin = None
    margin_warning = None
    try:
        broker_margin = broker.get_basket_margin(orders)
    except BrokerConnectionError as exc:
        margin_warning = str(exc)
    available = broker.get_margin_available()
    required = float((broker_margin or {}).get("margin_total", estimate["final_estimated_fund_requirement"]))
    return {
        "mode": _normalise_mode(payload.mode), "quote_timestamp": datetime.utcnow().isoformat() + "Z",
        "legs": [leg.model_dump() | {"bid": _quote_value(quotes[leg.symbol], "bid"), "ask": _quote_value(quotes[leg.symbol], "ask"), "ltp": _quote_value(quotes[leg.symbol], "lp")} for leg in preview_legs],
        "estimated_credit": calculations["net_credit"], "maximum_profit": calculations["max_initial_profit"],
        "maximum_loss": max(
            0.0,
            (by_name["CALL BUY"].strike - by_name["CALL SELL"].strike) * by_name["CALL SELL"].quantity - calculations["call_credit"]["total"],
            (by_name["PUT SELL"].strike - by_name["PUT BUY"].strike) * by_name["PUT SELL"].quantity - calculations["put_credit"]["total"],
        ),
        "lower_breakeven": calculations["lower_breakeven"], "upper_breakeven": calculations["upper_breakeven"],
        "estimated_charges": estimate["charges"], "fund_estimate": estimate,
        "fyers_margin_required": required, "available_funds": available,
        "additional_funds_required": max(0.0, required - available),
        "margin_source": "FYERS_MARGIN_CALCULATOR" if broker_margin else "LOCAL_ESTIMATE",
        "margin_warning": margin_warning,
        "call_sl": payload.call_sl if payload.call_sl is not None else calculations["call_sl_default"],
        "put_sl": payload.put_sl if payload.put_sl is not None else calculations["put_sl_default"],
        "alerts": ["Insufficient available funds"] if required > available else [],
        "orders": orders,
    }


@app.post("/basket/preview")
def basket_preview(payload: ManualMonitorSetup):
    return _basket_preview(payload)


@app.post("/basket/live")
def place_live_basket(payload: BasketPlacement):
    """The sole real-order path; protected by review confirmation and config."""
    if _normalise_mode(payload.mode) != "LIVE":
        raise HTTPException(status_code=422, detail="Paper baskets must use the paper-trade action")
    if not payload.confirmed:
        raise HTTPException(status_code=422, detail="Review confirmation is required before placing a live basket")
    if settings.execution_mode != "on":
        raise HTTPException(status_code=403, detail="Live execution is disabled. Set EXECUTION_MODE=on only when ready to trade.")
    preview = _basket_preview(payload)
    if preview["additional_funds_required"] > 0:
        raise HTTPException(status_code=409, detail="Insufficient available funds for this basket")
    response = broker.place_basket_orders(preview["orders"])
    # Store the reviewed executable prices as the monitor's Live entry values.
    payload.legs = [ManualLeg(**leg) for leg in preview["legs"]]
    saved = setup_manual_monitor(payload)
    return {"placed": True, "basket_response": response, "monitor": saved}


def _coerce_history_entry(row: PositionHistoryEntry) -> dict:
    legs = json.loads(row.legs_json or "[]") if row.legs_json else []
    return {
        "id": row.id,
        "position_id": row.position_id,
        "mode": row.mode,
        "status": row.status,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "expiry": row.expiry,
        "initial_net_credit": row.initial_net_credit,
        "call_sl": row.call_sl,
        "put_sl": row.put_sl,
        "call_net_credit": row.call_net_credit,
        "call_max_profit": row.call_max_profit,
        "call_sl_multiplier": row.call_sl_multiplier,
        "call_sl_loss": row.call_sl_loss,
        "call_sl_trigger_price": row.call_sl_trigger_price,
        "call_sl_triggered": bool(row.call_sl_triggered),
        "call_sl_trigger_time": row.call_sl_trigger_time.isoformat() if row.call_sl_trigger_time else None,
        "call_exit_price": row.call_exit_price,
        "call_final_pnl": row.call_final_pnl,
        "call_sl_state": row.call_sl_state or "ACTIVE",
        "put_net_credit": row.put_net_credit,
        "put_max_profit": row.put_max_profit,
        "put_sl_multiplier": row.put_sl_multiplier,
        "put_sl_loss": row.put_sl_loss,
        "put_sl_trigger_price": row.put_sl_trigger_price,
        "put_sl_triggered": bool(row.put_sl_triggered),
        "put_sl_trigger_time": row.put_sl_trigger_time.isoformat() if row.put_sl_trigger_time else None,
        "put_exit_price": row.put_exit_price,
        "put_final_pnl": row.put_final_pnl,
        "put_sl_state": row.put_sl_state or "ACTIVE",
        "entry_nifty_spot": row.entry_nifty_spot,
        "final_nifty_spot": row.final_nifty_spot,
        "opened_at": row.opened_at.isoformat() if row.opened_at else None,
        "closed_at": row.closed_at.isoformat() if row.closed_at else None,
        "realized_pnl": row.realized_pnl,
        "charges": row.charges,
        "net_pnl": row.net_pnl,
        "reason_for_closing": row.reason_for_closing,
        "legs": legs,
    }


def _persist_manual_position_entry(setup: ManualMonitorSetup, live_ltp_map: dict[str, float | None], spot: float | None,
                                   quotes: dict[str, dict], requested_entries: dict[str, float] | None = None) -> str:
    by_name = {leg.leg: leg for leg in setup.legs}
    calculations = calculate_position(by_name, live_ltp_map)
    simulated = {
        "CALL BUY": calculate_paper_execution_price(by_name["CALL BUY"], quotes.get(by_name["CALL BUY"].symbol), setup.slippage_points),
        "CALL SELL": calculate_paper_execution_price(by_name["CALL SELL"], quotes.get(by_name["CALL SELL"].symbol), setup.slippage_points),
        "PUT SELL": calculate_paper_execution_price(by_name["PUT SELL"], quotes.get(by_name["PUT SELL"].symbol), setup.slippage_points),
        "PUT BUY": calculate_paper_execution_price(by_name["PUT BUY"], quotes.get(by_name["PUT BUY"].symbol), setup.slippage_points),
    }
    call_credit = calculations["call_credit"]["total"]
    put_credit = calculations["put_credit"]["total"]
    advance = calculate_advance_sl(by_name)
    position_id = f"IC-{setup.mode}-{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}-{abs(hash(setup.expiry.isoformat() + ''.join(leg.symbol for leg in setup.legs))) % 100000:05d}"
    db = SessionLocal()
    try:
        row = PositionHistoryEntry(
            position_id=position_id,
            mode=setup.mode,
            status="OPEN",
            expiry=setup.expiry.isoformat(),
            call_buy_strike=by_name["CALL BUY"].strike,
            call_buy_entry=by_name["CALL BUY"].entry_price,
            call_buy_quantity=by_name["CALL BUY"].quantity,
            call_sell_strike=by_name["CALL SELL"].strike,
            call_sell_entry=by_name["CALL SELL"].entry_price,
            call_sell_quantity=by_name["CALL SELL"].quantity,
            put_sell_strike=by_name["PUT SELL"].strike,
            put_sell_entry=by_name["PUT SELL"].entry_price,
            put_sell_quantity=by_name["PUT SELL"].quantity,
            put_buy_strike=by_name["PUT BUY"].strike,
            put_buy_entry=by_name["PUT BUY"].entry_price,
            put_buy_quantity=by_name["PUT BUY"].quantity,
            initial_net_credit=call_credit + put_credit,
            call_sl=setup.call_sl if setup.call_sl is not None else calculations["call_sl_default"],
            put_sl=setup.put_sl if setup.put_sl is not None else calculations["put_sl_default"],
            call_net_credit=advance["call"]["net_credit"], call_max_profit=advance["call"]["max_profit"],
            call_sl_multiplier=advance["call"]["sl_multiplier"], call_sl_loss=advance["call"]["sl_loss"],
            call_sl_trigger_price=advance["call"]["sl_trigger_price"], call_sl_triggered=0,
            call_sl_state="ACTIVE", put_net_credit=advance["put"]["net_credit"], put_max_profit=advance["put"]["max_profit"],
            put_sl_multiplier=advance["put"]["sl_multiplier"], put_sl_loss=advance["put"]["sl_loss"],
            put_sl_trigger_price=advance["put"]["sl_trigger_price"], put_sl_triggered=0, put_sl_state="ACTIVE",
            entry_nifty_spot=spot,
            opened_at=datetime.utcnow(),
            realized_pnl=0.0,
            charges=calculate_position_charges(by_name),
            net_pnl=-calculate_position_charges(by_name),
            reason_for_closing=None,
            legs_json=json.dumps([
                {"leg": leg.leg, "symbol": leg.symbol, "strike": leg.strike, "quantity": leg.quantity,
                 "entry_price": leg.entry_price, "requested_entry_price": (requested_entries or {}).get(leg.leg),
                 "simulated_entry_price": simulated[leg.leg] if setup.mode == "PAPER" else None,
                 "entry_at": datetime.utcnow().isoformat(), "mode": setup.mode}
                for leg in setup.legs
            ]),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.position_id
    finally:
        db.close()


def _close_active_history(reason: str, status: str = "STOPPED", final_spot: float | None = None) -> dict | None:
    if _manual_monitor is None or _manual_active_history_id is None:
        return None
    db = SessionLocal()
    try:
        row = db.query(PositionHistoryEntry).filter_by(position_id=_manual_active_history_id).first()
        if row is None:
            return None
        final_ltps: dict[str, float | None] = {}
        quotes: dict[str, dict] = {}
        if broker.is_connected():
            try:
                symbols = ["NSE:NIFTY50-INDEX"] + [leg.symbol for leg in _manual_monitor.legs]
                quotes = broker.get_quotes(symbols)
                final_ltps = {leg.leg: _quote_value(quotes.get(leg.symbol, {}), "lp") for leg in _manual_monitor.legs}
                final_spot = final_spot if final_spot is not None else _quote_value(quotes.get("NSE:NIFTY50-INDEX", {}), "lp")
            except BrokerConnectionError:
                # Preserve the latest persisted mark if a connection drops at close.
                final_ltps = {}
        by_name = {leg.leg: leg for leg in _manual_monitor.legs}
        complete_ltps = {name: price for name, price in final_ltps.items() if price is not None}
        calculations = calculate_position(by_name, complete_ltps)
        legs = json.loads(row.legs_json or "[]")
        now = datetime.utcnow().isoformat()
        for leg in legs:
            exit_price = final_ltps.get(leg.get("leg"))
            if exit_price is not None:
                leg["exit_price"] = exit_price
                leg["exit_at"] = now
        row.legs_json = json.dumps(legs)
        row.status = status
        row.final_nifty_spot = final_spot if final_spot is not None else row.final_nifty_spot or row.entry_nifty_spot
        row.closed_at = datetime.utcnow()
        row.reason_for_closing = reason
        row.updated_at = datetime.utcnow()
        # Closing charges are added only once the final executable prices are
        # available; open rows show entry charges only.
        row.charges = calculate_position_charges(by_name, complete_ltps or None)
        row.realized_pnl = calculations["total_pnl"] if len(complete_ltps) == 4 else row.realized_pnl
        row.net_pnl = (row.realized_pnl or 0.0) - (row.charges or 0.0)
        db.commit()
        db.refresh(row)
        return _coerce_history_entry(row)
    finally:
        db.close()


def _manual_snapshot() -> dict:
    if _manual_monitor is None:
        return {"configured": False, "connection": broker.connection_status()}
    connected = broker.is_connected()
    quotes = {}
    if connected:
        symbols = ["NSE:NIFTY50-INDEX"] + [leg.symbol for leg in _manual_monitor.legs]
        quotes = broker.get_quotes(symbols)
    spot_quote = quotes.get("NSE:NIFTY50-INDEX", {})
    spot = _quote_value(spot_quote, "lp")
    spot_change = _quote_value(spot_quote, "ch")
    spot_change_pct = _quote_value(spot_quote, "chp")
    by_name = {leg.leg: leg for leg in _manual_monitor.legs}
    live_ltps = {}
    live_legs = []
    for leg in _manual_monitor.legs:
        quote = quotes.get(leg.symbol, {})
        ltp = _quote_value(quote, "lp")
        live_ltps[leg.leg] = ltp
        live_legs.append(leg.model_dump() | {
            "ltp": ltp, "bid": _quote_value(quote, "bid"), "ask": _quote_value(quote, "ask"),
            "change": _quote_value(quote, "ch"), "volume": _quote_value(quote, "volume"),
            "oi": _quote_value(quote, "oi"), "iv": _quote_value(quote, "iv"),
        })
    calculations = calculate_position(by_name, live_ltps)
    advance = calculate_advance_sl(by_name)
    for leg in live_legs:
        leg["pnl"] = calculations["pnl"][leg["leg"]]
    # Once an active trade is entered, its entry prices (including paper
    # simulated fills) are fixed.  Do not re-simulate them on every tick.
    # Entry prices are fixed at setup (paper prices already include one
    # slippage application), so fund calculations must not simulate again.
    fund_requirement = calculate_fund_requirement(by_name, mode="LIVE")
    call_buy, call_sell = by_name["CALL BUY"], by_name["CALL SELL"]
    put_sell, put_buy = by_name["PUT SELL"], by_name["PUT BUY"]
    lower_short, upper_short = put_sell.strike, call_sell.strike
    lower_hedge, upper_hedge = put_buy.strike, call_buy.strike
    call_iv = next((leg["iv"] for leg in live_legs if leg["leg"] == "CALL SELL"), None)
    put_iv = next((leg["iv"] for leg in live_legs if leg["leg"] == "PUT SELL"), None)
    short_ivs = [iv for iv in (call_iv, put_iv) if iv is not None]
    global _manual_previous_spot, _manual_previous_iv
    spot_move = None if spot is None or _manual_previous_spot is None else spot - _manual_previous_spot
    average_iv = sum(short_ivs) / len(short_ivs) if short_ivs else None
    iv_change = None if average_iv is None or _manual_previous_iv is None else average_iv - _manual_previous_iv
    _manual_previous_spot = spot
    _manual_previous_iv = average_iv

    distance_call = None if spot is None else upper_short - spot
    distance_put = None if spot is None else spot - lower_short
    distance_lower_be = None if spot is None else spot - calculations["lower_breakeven"]
    distance_upper_be = None if spot is None else calculations["upper_breakeven"] - spot
    boundary_distance = min(
        abs(distance_call) if distance_call is not None else 10**9,
        abs(distance_put) if distance_put is not None else 10**9,
    )
    outside_risk = spot is not None and (spot <= calculations["lower_breakeven"] or spot >= calculations["upper_breakeven"])
    risk = "DANGER" if outside_risk or boundary_distance <= 50 else "WARNING" if boundary_distance <= 100 else "SAFE"
    alerts = []
    if not connected or spot is None:
        alerts.append("Unable to fetch live price")
    if any(leg["ltp"] is None for leg in live_legs):
        alerts.append("Missing live leg data")
    if distance_lower_be is not None and 0 < distance_lower_be <= 100:
        alerts.append(f"NIFTY approaching lower break-even ({distance_lower_be:.0f} points)")
    if distance_upper_be is not None and 0 < distance_upper_be <= 100:
        alerts.append(f"NIFTY approaching upper break-even ({distance_upper_be:.0f} points)")
    if outside_risk:
        alerts.append("NIFTY has crossed an Iron Condor breakeven")
    if spot_move is not None and abs(spot_move) >= 100:
        alerts.append(f"Large NIFTY movement: {spot_move:+.0f} points")
    if iv_change is not None and iv_change >= 2:
        alerts.append(f"Short IV increased by {iv_change:.1f} points")
    history_row = None
    if _manual_active_history_id is not None:
        db = SessionLocal()
        try:
            history_row = db.query(PositionHistoryEntry).filter_by(position_id=_manual_active_history_id).first()
        finally:
            db.close()
    call_complete = live_ltps.get("CALL SELL") is not None and live_ltps.get("CALL BUY") is not None
    put_complete = live_ltps.get("PUT SELL") is not None and live_ltps.get("PUT BUY") is not None
    call_pnl = calculations["call_spread_pnl"] if call_complete else None
    put_pnl = calculations["put_spread_pnl"] if put_complete else None
    call_state, call_new_trigger = advance_sl_state(
        call_pnl, advance["call"]["sl_loss"], spot, advance["call"]["sl_trigger_price"], "CALL",
        history_row.call_sl_state if history_row else "ACTIVE")
    put_state, put_new_trigger = advance_sl_state(
        put_pnl, advance["put"]["sl_loss"], spot, advance["put"]["sl_trigger_price"], "PUT",
        history_row.put_sl_state if history_row else "ACTIVE")
    if call_new_trigger:
        alerts.append("CALL SIDE SL TRIGGERED")
    elif call_state == "APPROACHING_SL":
        alerts.append("CALL SL APPROACHING")
    if put_new_trigger:
        alerts.append("PUT SIDE SL TRIGGERED")
    elif put_state == "APPROACHING_SL":
        alerts.append("PUT SL APPROACHING")
    expiry_at = datetime.combine(_manual_monitor.expiry, date_time(15, 30))
    seconds_left = max(0, int((expiry_at - datetime.now()).total_seconds()))
    if seconds_left == 0:
        alerts.append("Expired position")
    if _manual_active_history_id is not None:
        db = SessionLocal()
        try:
            row = db.query(PositionHistoryEntry).filter_by(position_id=_manual_active_history_id).first()
            if row is not None:
                row.entry_nifty_spot = row.entry_nifty_spot if row.entry_nifty_spot is not None else spot
                row.final_nifty_spot = spot
                row.updated_at = datetime.utcnow()
                row.charges = calculate_position_charges(by_name)
                row.realized_pnl = calculations["total_pnl"]
                row.net_pnl = calculations["total_pnl"] - row.charges
                row.mode = _manual_monitor.mode
                row.call_sl_state = call_state
                row.put_sl_state = put_state
                if call_new_trigger and not row.call_sl_triggered:
                    row.call_sl_triggered = 1
                    row.call_sl_trigger_time = datetime.utcnow()
                    row.call_exit_price = live_ltps.get("CALL SELL")
                    row.call_final_pnl = call_pnl
                    row.reason_for_closing = "SL"
                if put_new_trigger and not row.put_sl_triggered:
                    row.put_sl_triggered = 1
                    row.put_sl_trigger_time = datetime.utcnow()
                    row.put_exit_price = live_ltps.get("PUT SELL")
                    row.put_final_pnl = put_pnl
                    row.reason_for_closing = "SL"
                db.commit()
        finally:
            db.close()
    return {"configured": True, "connection": broker.connection_status() if connected else "DISCONNECTED", "expiry": _manual_monitor.expiry.isoformat(),
            "spot": spot, "spot_change": spot_change, "spot_change_pct": spot_change_pct,
            "mode": _manual_monitor.mode, "position_status": "OPEN", "legs": live_legs, "total_pnl": calculations["total_pnl"],
            "call_spread_pnl": calculations["call_spread_pnl"], "put_spread_pnl": calculations["put_spread_pnl"],
            "call_credit": calculations["call_credit"]["total"], "put_credit": calculations["put_credit"]["total"],
            "call_credit_per_unit": calculations["call_credit"]["per_unit"], "put_credit_per_unit": calculations["put_credit"]["per_unit"],
            "net_credit": calculations["net_credit"], "net_credit_per_unit": calculations["net_credit_per_unit"],
            "max_initial_profit": calculations["max_initial_profit"], "lower_short": lower_short,
            "upper_short": upper_short, "lower_hedge": lower_hedge, "upper_hedge": upper_hedge,
            "lower_breakeven": calculations["lower_breakeven"], "upper_breakeven": calculations["upper_breakeven"],
            "distance_call_short": distance_call, "distance_put_short": distance_put,
            "distance_lower_breakeven": distance_lower_be, "distance_upper_breakeven": distance_upper_be,
            "risk": risk, "call_iv": call_iv, "put_iv": put_iv, "average_short_iv": average_iv,
            "iv_change": iv_change, "seconds_to_expiry": seconds_left,
            "call_sl": advance["call"]["sl_loss"], "put_sl": advance["put"]["sl_loss"],
            "sl_multiplier": settings.sl_multiple_of_credit,
            "advance_sl": _advance_sl_payload(by_name, spot),
            "call_sl_state": call_state, "put_sl_state": put_state,
            "call_sl_triggered": bool(history_row.call_sl_triggered) if history_row else call_new_trigger,
            "put_sl_triggered": bool(history_row.put_sl_triggered) if history_row else put_new_trigger,
            "alerts": alerts,
            "fund_requirement": fund_requirement,
            "fund_requirement_market": calculate_fund_requirement(by_name, mode="LIVE"),
            "fund_requirement_paper": calculate_fund_requirement(by_name, mode="LIVE"),
            "paper_slippage_points": _manual_monitor.slippage_points,
            "paper_execution_prices": {leg.leg: calculate_paper_execution_price(leg, quotes.get(leg.symbol), _manual_monitor.slippage_points) for leg in _manual_monitor.legs},
            "history_id": _manual_active_history_id}


@app.get("/manual-monitor")
def manual_monitor(mode: str = "LIVE"):
    mode_key = _activate_manual_mode(mode)
    try:
        snapshot = _manual_snapshot()
        _save_manual_mode(mode_key)
        return snapshot
    except BrokerConnectionError as exc:
        return {"configured": _manual_monitor is not None, "connection": "RECONNECTING", "error": str(exc), "alerts": ["Broker connection lost"]}


# ---------------- Raw option chain (for the Option Chain screen) ----------------
@app.get("/expiries")
def expiries_endpoint(symbol: str = "NSE:NIFTY50-INDEX"):
    try:
        return {"expiries": broker.get_expiries(symbol)}
    except BrokerConnectionError as e:
        raise HTTPException(status_code=503, detail=f"Broker/data unavailable: {e}")


@app.get("/chain")
def chain_endpoint(symbol: str = "NSE:NIFTY50-INDEX", expiry: str | None = None):
    try:
        selected_expiry = expiry or broker.get_nearest_expiry(symbol)
        chain = broker.get_option_chain(symbol, selected_expiry)
    except BrokerConnectionError as e:
        raise HTTPException(status_code=503, detail=f"Broker/data unavailable: {e}")
    return chain.model_dump(mode="json")


# ---------------- Analysis ----------------
@app.get("/analyze")
def analyze(symbol: str = "NSE:NIFTY50-INDEX", db: Session = Depends(get_session)):
    allowed, msg = daily_loss_drawdown.is_trading_allowed_today(db)
    if not allowed:
        raise HTTPException(status_code=423, detail=msg)

    try:
        expiry = broker.get_nearest_expiry(symbol)
        expiry_verified = True
    except BrokerConnectionError as e:
        expiry = str(date.today())
        expiry_verified = False

    try:
        chain = broker.get_option_chain(symbol, expiry)
        margin = broker.get_margin_available()
        broker_status = broker.connection_status()
    except BrokerConnectionError as e:
        raise HTTPException(status_code=503, detail=f"Broker/data unavailable: {e}")

    index_symbol = "NSE:NIFTY50-INDEX"
    try:
        context = market_data_service.get_market_context(broker, symbol, index_symbol)
    except BrokerConnectionError as e:
        # Missing/unreliable market data is itself a no-trade condition —
        # don't silently fall back to stubbed values, surface it.
        raise HTTPException(status_code=503, detail=f"Market data unavailable: {e}")

    decision = strategy_engine.analyze(
        chain=chain, price_bars=context["price_bars"], india_vix=context["india_vix"],
        prev_day_high=context["prev_day_high"], prev_day_low=context["prev_day_low"],
        available_margin=margin, broker_status=broker_status,
        expiry_verified=expiry_verified, event_risk_flag=context["event_risk_flag"],
    )

    global _active_structure, _active_expiry
    if decision.decision == "TRADE" and decision.structure:
        _active_structure = decision.structure
        _active_expiry = expiry
        journal_service.log_entry(db, decision, chain.spot, expiry)

    broadcast_sync({"type": "decision", "data": decision.model_dump(mode="json")})
    return decision.model_dump(mode="json")


# ---------------- Position sizing ----------------
@app.post("/size")
def size(structure: IronCondorStructure, margin_per_lot: float):
    available_margin = broker.get_margin_available()
    return position_sizing.size_position(structure, margin_per_lot, available_margin,
                                          lot_size=settings.nifty_lot_size).model_dump()


# ---------------- Execution ----------------
@app.post("/execute")
def execute(db: Session = Depends(get_session)):
    if _active_structure is None:
        raise HTTPException(status_code=400, detail="No active structure to execute")
    allowed, msg = daily_loss_drawdown.is_trading_allowed_today(db)
    if not allowed:
        raise HTTPException(status_code=423, detail=msg)

    margin = broker.get_margin_available()
    size_result = position_sizing.size_position(_active_structure, margin_per_lot=margin, available_margin=margin,
                                                 lot_size=settings.nifty_lot_size)
    result = exec_engine.execute_iron_condor(_active_structure, size_result, _active_expiry, margin)
    if not result.success:
        raise HTTPException(status_code=409, detail=result.aborted_reason)

    global _active_quantity
    _active_quantity = size_result.quantity
    return {"success": True, "orders": {k: v.model_dump() for k, v in result.orders.items()}}


# ---------------- Live monitoring ----------------
@app.get("/monitor")
def monitor():
    if _active_structure is None:
        raise HTTPException(status_code=404, detail="No active position")
    live_ltp = {leg.role: broker.get_ltp(leg.symbol or f"NSE:NIFTY{int(leg.strike)}{leg.option_type}")
                for leg in _active_structure.legs}
    spot = broker.get_ltp("NSE:NIFTY50-INDEX")

    from app.services.expected_move import days_to_expiry as _dte
    expiry_date = date.fromisoformat(_active_expiry) if _active_expiry and "-" in _active_expiry else date.today()
    dte_days = _dte(datetime.now(), expiry_date)

    call_sl = stop_loss_engine.check_call_side_sl(_active_structure, live_ltp)
    put_sl = stop_loss_engine.check_put_side_sl(_active_structure, live_ltp)

    # Monitoring is alert-only.  A stop breach is surfaced to the user but
    # never submits, modifies, or exits a broker order.
    if call_sl.triggered:
        notification_service.notify("call_sl", "🔴", call_sl.reason)
    if put_sl.triggered:
        notification_service.notify("put_sl", "🔴", put_sl.reason)

    snapshot = risk_engine.build_risk_snapshot(
        _active_structure, live_ltp, spot,
        call_sl_distance=call_sl.pnl_per_share - call_sl.sl_threshold_per_share,
        put_sl_distance=put_sl.pnl_per_share - put_sl.sl_threshold_per_share,
        days_to_expiry=dte_days, lot_size=settings.nifty_lot_size,
    )
    should_book, book_reason = profit_booking_engine.should_evaluate_exit(_active_structure, live_ltp, dte_days)

    payload = {
        "risk": snapshot.model_dump(),
        "call_sl": call_sl.model_dump(),
        "put_sl": put_sl.model_dump(),
        "profit_booking": {"should_evaluate_exit": should_book, "reason": book_reason},
        "structure": _active_structure.model_dump(mode="json"),
        "live_ltp": live_ltp,
        "spot": spot,
    }
    broadcast_sync({"type": "monitor", "data": payload})
    return payload


# ---------------- Journal ----------------
@app.get("/journal")
def journal_list(db: Session = Depends(get_session), limit: int = 50):
    from app.database import TradeJournalEntry
    rows = db.query(TradeJournalEntry).order_by(TradeJournalEntry.id.desc()).limit(limit).all()
    return [
        {
            "id": r.id, "date": r.date, "entry_time": r.entry_time, "spot_at_entry": r.spot_at_entry,
            "expiry": r.expiry, "entry_premium": r.entry_premium, "exit_premium": r.exit_premium,
            "market_regime": r.market_regime, "ai_score": r.ai_score,
            "net_pnl": r.net_pnl, "reason_for_exit": r.reason_for_exit,
        }
        for r in rows
    ]


@app.get("/journal/export")
def journal_export():
    return {"csv": export_journal_csv()}


# ---------------- Settings ----------------
@app.get("/settings")
def get_settings():
    return settings.model_dump()


# ---------------- Event risk (manual flag until a calendar feed is wired in) ----------------
@app.post("/event-risk")
def set_event_risk(flag: bool):
    market_data_service.set_event_risk(flag)
    return {"event_risk_flag": market_data_service.get_event_risk()}


@app.get("/event-risk")
def get_event_risk():
    return {"event_risk_flag": market_data_service.get_event_risk()}


# ---------------- WebSocket broadcast ----------------
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    _ws_clients.append(ws)
    try:
        while True:
            await ws.receive_text()  # keepalive / ignored
    except WebSocketDisconnect:
        _ws_clients.remove(ws)


async def _broadcast(payload: dict):
    dead = []
    for client in _ws_clients:
        try:
            await client.send_text(json.dumps(payload))
        except Exception:
            dead.append(client)
    for d in dead:
        _ws_clients.remove(d)


# Serve the monitor from the same origin as the API, so local use needs only
# one URL and the FYERS callback can stay on port 8000.
app.mount("/", StaticFiles(directory=Path(__file__).resolve().parent.parent / "frontend", html=True), name="frontend")

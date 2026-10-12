"""SQLite-backed persistence: trade journal, runtime settings overrides,
and daily P&L (needed by the daily-loss-limit / drawdown engines)."""
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Text, ForeignKey, UniqueConstraint, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime
import csv
import io
import json

engine = create_engine("sqlite:///condor_ai.db", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


class TradeJournalEntry(Base):
    __tablename__ = "trade_journal"
    id = Column(Integer, primary_key=True)
    date = Column(String)
    entry_time = Column(String)
    spot_at_entry = Column(Float)
    expiry = Column(String)
    strikes_json = Column(Text)           # the four strikes, JSON-encoded
    entry_premium = Column(Float)
    exit_premium = Column(Float, nullable=True)
    iv = Column(Float)
    iv_percentile = Column(Float, nullable=True)
    oi_snapshot_json = Column(Text, nullable=True)
    market_regime = Column(String)
    ai_score = Column(Float)
    reason_for_entry = Column(Text)
    reason_for_exit = Column(Text, nullable=True)
    gross_pnl = Column(Float, nullable=True)
    charges = Column(Float, nullable=True)
    net_pnl = Column(Float, nullable=True)
    max_adverse_excursion = Column(Float, nullable=True)
    max_favorable_excursion = Column(Float, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class PositionHistoryEntry(Base):
    __tablename__ = "position_history"
    id = Column(Integer, primary_key=True)
    position_id = Column(String, unique=True, index=True)
    mode = Column(String, default="MARKET")
    status = Column(String, default="OPEN")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)
    expiry = Column(String, nullable=True)
    call_buy_strike = Column(Float, nullable=True)
    call_buy_entry = Column(Float, nullable=True)
    call_buy_quantity = Column(Integer, nullable=True)
    call_sell_strike = Column(Float, nullable=True)
    call_sell_entry = Column(Float, nullable=True)
    call_sell_quantity = Column(Integer, nullable=True)
    put_sell_strike = Column(Float, nullable=True)
    put_sell_entry = Column(Float, nullable=True)
    put_sell_quantity = Column(Integer, nullable=True)
    put_buy_strike = Column(Float, nullable=True)
    put_buy_entry = Column(Float, nullable=True)
    put_buy_quantity = Column(Integer, nullable=True)
    initial_net_credit = Column(Float, nullable=True)
    call_sl = Column(Float, nullable=True)
    put_sl = Column(Float, nullable=True)
    call_net_credit = Column(Float, nullable=True)
    call_max_profit = Column(Float, nullable=True)
    call_sl_multiplier = Column(Float, nullable=True)
    call_sl_loss = Column(Float, nullable=True)
    call_sl_trigger_price = Column(Float, nullable=True)
    call_sl_triggered = Column(Integer, default=0)
    call_sl_trigger_time = Column(DateTime, nullable=True)
    call_exit_price = Column(Float, nullable=True)
    call_final_pnl = Column(Float, nullable=True)
    call_sl_state = Column(String, default="ACTIVE")
    put_net_credit = Column(Float, nullable=True)
    put_max_profit = Column(Float, nullable=True)
    put_sl_multiplier = Column(Float, nullable=True)
    put_sl_loss = Column(Float, nullable=True)
    put_sl_trigger_price = Column(Float, nullable=True)
    put_sl_triggered = Column(Integer, default=0)
    put_sl_trigger_time = Column(DateTime, nullable=True)
    put_exit_price = Column(Float, nullable=True)
    put_final_pnl = Column(Float, nullable=True)
    put_sl_state = Column(String, default="ACTIVE")
    entry_nifty_spot = Column(Float, nullable=True)
    final_nifty_spot = Column(Float, nullable=True)
    opened_at = Column(DateTime, nullable=True)
    closed_at = Column(DateTime, nullable=True)
    realized_pnl = Column(Float, nullable=True)
    gross_pnl = Column(Float, nullable=True)
    charges = Column(Float, nullable=True)
    charge_breakdown_json = Column(Text, nullable=True)
    net_pnl = Column(Float, nullable=True)
    reason_for_closing = Column(Text, nullable=True)
    legs_json = Column(Text, nullable=True)
    net_pnl_percent = Column(Float, nullable=True)
    snapshot_frozen = Column(Integer, default=0)
    pnl_basis = Column(String, nullable=True)
    exit_capture_status = Column(String, default="NOT_CAPTURED")


class PositionHistoryLeg(Base):
    __tablename__ = "position_history_legs"
    __table_args__ = (UniqueConstraint("position_history_id", "leg", name="uq_position_history_leg"),)
    id = Column(Integer, primary_key=True)
    position_history_id = Column(Integer, ForeignKey("position_history.id", ondelete="CASCADE"), nullable=False, index=True)
    leg = Column(String, nullable=False)
    symbol = Column(String, nullable=True)
    strike = Column(Float, nullable=True)
    quantity = Column(Integer, nullable=True)
    entry_price = Column(Float, nullable=True)
    entry_at = Column(DateTime, nullable=True)
    entry_value = Column(Float, nullable=True)
    exit_price = Column(Float, nullable=True)
    exit_at = Column(DateTime, nullable=True)
    exit_value = Column(Float, nullable=True)
    pnl = Column(Float, nullable=True)
    pnl_pct = Column(Float, nullable=True)
    price_status = Column(String, default="NOT_CAPTURED")
    last_ltp = Column(Float, nullable=True)
    last_ltp_at = Column(DateTime, nullable=True)
    last_pnl = Column(Float, nullable=True)
    last_pnl_pct = Column(Float, nullable=True)


def _stored_datetime(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


class DailyPnL(Base):
    __tablename__ = "daily_pnl"
    id = Column(Integer, primary_key=True)
    date = Column(String, unique=True)
    realized_pnl = Column(Float, default=0.0)
    trading_disabled = Column(Integer, default=0)  # 0/1


class AccountState(Base):
    __tablename__ = "account_state"
    id = Column(Integer, primary_key=True)
    peak_equity = Column(Float, default=0.0)
    current_equity = Column(Float, default=0.0)
    updated_at = Column(DateTime, default=datetime.utcnow)


class SettingOverride(Base):
    __tablename__ = "setting_override"
    key = Column(String, primary_key=True)
    value = Column(String)


Base.metadata.create_all(engine)

# create_all does not add columns to an existing SQLite database. Keep the
# lightweight local database compatible with upgrades without dropping history.
with engine.begin() as connection:
    existing = {column["name"] for column in inspect(engine).get_columns("position_history")}
    for column in PositionHistoryEntry.__table__.columns:
        if column.name not in existing:
            column_type = column.type.compile(dialect=engine.dialect)
            connection.execute(text(f"ALTER TABLE position_history ADD COLUMN {column.name} {column_type}"))
    connection.execute(text("UPDATE position_history SET gross_pnl = realized_pnl WHERE gross_pnl IS NULL AND realized_pnl IS NOT NULL"))
    connection.execute(text("UPDATE position_history SET snapshot_frozen = 0 WHERE snapshot_frozen IS NULL"))
    connection.execute(text("UPDATE position_history SET snapshot_frozen = 1 WHERE status != 'OPEN'"))

    legacy_leg_columns = {
        "CALL BUY": ("call_buy_strike", "call_buy_entry", "call_buy_quantity"),
        "CALL SELL": ("call_sell_strike", "call_sell_entry", "call_sell_quantity"),
        "PUT SELL": ("put_sell_strike", "put_sell_entry", "put_sell_quantity"),
        "PUT BUY": ("put_buy_strike", "put_buy_entry", "put_buy_quantity"),
    }
    legacy_rows = connection.execute(text(
        "SELECT id, status, created_at, opened_at, legs_json, gross_pnl, pnl_basis, net_pnl, net_pnl_percent, "
        "call_buy_strike, call_buy_entry, call_buy_quantity, "
        "call_sell_strike, call_sell_entry, call_sell_quantity, "
        "put_sell_strike, put_sell_entry, put_sell_quantity, "
        "put_buy_strike, put_buy_entry, put_buy_quantity FROM position_history"
    )).mappings().all()
    for row in legacy_rows:
        stored_names = set(connection.execute(text(
            "SELECT leg FROM position_history_legs WHERE position_history_id = :row_id"
        ), {"row_id": row["id"]}).scalars().all())
        try:
            saved_legs = json.loads(row["legs_json"] or "[]")
        except (TypeError, ValueError):
            saved_legs = []
        if not isinstance(saved_legs, list):
            saved_legs = []
        saved_by_name = {item.get("leg"): item for item in saved_legs if isinstance(item, dict) and item.get("leg")}
        total_entry_value = 0.0
        for name, columns in legacy_leg_columns.items():
            saved = saved_by_name.get(name, {})
            strike_column, entry_column, quantity_column = columns
            strike = saved.get("strike", row[strike_column])
            entry_price = saved.get("entry_price", row[entry_column])
            quantity = saved.get("quantity", row[quantity_column])
            entry_value = float(entry_price) * int(quantity) if entry_price is not None and quantity is not None else None
            if entry_value is not None:
                total_entry_value += entry_value
            exit_price = saved.get("exit_price")
            pnl = saved.get("pnl")
            direction = 1 if "BUY" in name else -1
            if pnl is None and exit_price is not None and entry_price is not None and quantity is not None:
                pnl = (float(exit_price) - float(entry_price)) * int(quantity) * direction
            pnl_pct = saved.get("pnl_pct")
            if pnl_pct is None and pnl is not None and entry_value:
                pnl_pct = pnl / entry_value * 100
            if name not in stored_names:
                connection.execute(PositionHistoryLeg.__table__.insert().values(
                    position_history_id=row["id"],
                    leg=name,
                    symbol=saved.get("symbol"),
                    strike=strike,
                    quantity=quantity,
                    entry_price=entry_price,
                    entry_at=_stored_datetime(saved.get("entry_at")) or _stored_datetime(row["opened_at"] or row["created_at"]),
                    entry_value=entry_value,
                    exit_price=exit_price,
                    exit_at=_stored_datetime(saved.get("exit_at")),
                    exit_value=float(exit_price) * int(quantity) if exit_price is not None and quantity is not None else None,
                    pnl=pnl,
                    pnl_pct=pnl_pct,
                    price_status="CAPTURED" if exit_price is not None else "NOT_CAPTURED",
                    last_ltp=saved.get("current_ltp"),
                    last_pnl=saved.get("pnl"),
                    last_pnl_pct=saved.get("pnl_pct"),
                ))
            if exit_price is not None:
                connection.execute(text(
                    "UPDATE position_history_legs SET "
                    "exit_price = COALESCE(exit_price, :exit_price), "
                    "exit_at = COALESCE(exit_at, :exit_at), "
                    "exit_value = COALESCE(exit_value, :exit_value), "
                    "pnl = COALESCE(pnl, :pnl), "
                    "pnl_pct = COALESCE(pnl_pct, :pnl_pct), "
                    "price_status = CASE WHEN exit_price IS NULL THEN 'CAPTURED' ELSE price_status END "
                    "WHERE position_history_id = :row_id AND leg = :leg"
                ), {
                    "exit_price": exit_price,
                    "exit_at": _stored_datetime(saved.get("exit_at")),
                    "exit_value": float(exit_price) * int(quantity) if quantity is not None else None,
                    "pnl": pnl,
                    "pnl_pct": pnl_pct,
                    "row_id": row["id"],
                    "leg": name,
                })
        captured_count = connection.execute(text(
            "SELECT count(*) FROM position_history_legs WHERE position_history_id = :row_id AND exit_price IS NOT NULL"
        ), {"row_id": row["id"]}).scalar_one()
        capture_status = "CAPTURED" if captured_count == 4 else "PARTIAL" if captured_count else "NOT_CAPTURED"
        connection.execute(text(
            "UPDATE position_history SET exit_capture_status = :capture_status "
            "WHERE id = :row_id AND exit_capture_status IS NULL"
        ), {"capture_status": capture_status, "row_id": row["id"]})
        if row["pnl_basis"] is None:
            pnl_basis = "EXIT_QUOTES" if captured_count == 4 else "LAST_MARK" if row["gross_pnl"] is not None else "NOT_CAPTURED"
            connection.execute(text(
                "UPDATE position_history SET pnl_basis = :pnl_basis WHERE id = :row_id"
            ), {"pnl_basis": pnl_basis, "row_id": row["id"]})
        if row["net_pnl_percent"] is None and row["net_pnl"] is not None and total_entry_value:
            connection.execute(text(
                "UPDATE position_history SET net_pnl_percent = :percent WHERE id = :row_id"
            ), {"percent": row["net_pnl"] / total_entry_value * 100, "row_id": row["id"]})


def get_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def export_journal_csv() -> str:
    db = SessionLocal()
    rows = db.query(TradeJournalEntry).all()
    buf = io.StringIO()
    if not rows:
        db.close()
        return ""
    writer = csv.writer(buf)
    cols = [c.name for c in TradeJournalEntry.__table__.columns]
    writer.writerow(cols)
    for r in rows:
        writer.writerow([getattr(r, c) for c in cols])
    db.close()
    return buf.getvalue()

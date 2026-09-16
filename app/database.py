"""SQLite-backed persistence: trade journal, runtime settings overrides,
and daily P&L (needed by the daily-loss-limit / drawdown engines)."""
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Text, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime
import csv
import io

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
    charges = Column(Float, nullable=True)
    net_pnl = Column(Float, nullable=True)
    reason_for_closing = Column(Text, nullable=True)
    legs_json = Column(Text, nullable=True)


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

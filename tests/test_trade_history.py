import json
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, PositionHistoryEntry, PositionHistoryLeg
from app.main import _coerce_history_entry, _expire_due_positions, _persist_complete_history_marks, manual_history


LEG_DATA = (
    ("CALL BUY", 24300, 2.25),
    ("CALL SELL", 23800, 11.55),
    ("PUT SELL", 22950, 9.45),
    ("PUT BUY", 22000, 2.25),
)


class TradeHistoryTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        self.session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
        self.db = self.session_factory()
        self.trade = PositionHistoryEntry(
            position_id="IC-PAPER-history-test",
            mode="PAPER",
            status="OPEN",
            expiry=(date.today() + timedelta(days=1)).isoformat(),
            opened_at=datetime.utcnow(),
            initial_net_credit=1072.5,
            realized_pnl=50.0,
            gross_pnl=50.0,
            charges=3.0,
            net_pnl=47.0,
            net_pnl_percent=0.028,
            charge_breakdown_json=json.dumps({
                "brokerage": 0.0, "stt": 0.2, "exchange_transaction": 1.0,
                "gst": 0.2, "sebi": 0.1, "stamp_duty": 0.1, "other": 1.4,
            }),
            snapshot_frozen=0,
            pnl_basis="LAST_MARK",
            exit_capture_status="NOT_CAPTURED",
        )
        self.db.add(self.trade)
        self.db.flush()
        for name, strike, entry_price in LEG_DATA:
            quantity = 65
            pnl = 12.5
            self.db.add(PositionHistoryLeg(
                position_history_id=self.trade.id,
                leg=name,
                symbol=f"NSE:{name.replace(' ', '')}",
                strike=strike,
                quantity=quantity,
                entry_price=entry_price,
                entry_at=self.trade.opened_at,
                entry_value=entry_price * quantity,
                last_ltp=entry_price + (pnl / quantity) * (1 if "BUY" in name else -1),
                last_ltp_at=self.trade.opened_at,
                last_pnl=pnl,
                last_pnl_pct=pnl / (entry_price * quantity) * 100,
                price_status="NOT_CAPTURED",
            ))
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def test_history_reads_all_four_saved_legs_without_exit_quotes(self):
        payload = _coerce_history_entry(self.trade, self.db)

        self.assertEqual(payload["status"], "OPEN")
        self.assertEqual(len(payload["legs"]), 4)
        self.assertEqual(payload["legs"][0]["strike"], 24300)
        self.assertEqual(payload["legs"][0]["quantity"], 65)
        self.assertEqual(payload["legs"][0]["entry_price"], 2.25)
        self.assertEqual(payload["legs"][0]["entry_value"], 146.25)
        self.assertIsNone(payload["legs"][0]["exit_price"])
        self.assertEqual(payload["legs"][0]["exit_status"], "NOT_CAPTURED")
        self.assertEqual(payload["legs"][0]["pnl"], 12.5)

    def test_history_endpoint_does_not_contact_broker(self):
        with patch("app.main.SessionLocal", return_value=self.db), \
                patch("app.main.broker.is_connected", side_effect=AssertionError("history requested broker status")), \
                patch("app.main.broker.get_quotes", side_effect=AssertionError("history requested quotes")):
            payload = manual_history()

        self.assertEqual(len(payload), 1)
        self.assertEqual(len(payload[0]["legs"]), 4)

    def test_complete_mark_updates_all_four_leg_snapshots(self):
        marks = {name: entry_price + 1 for name, _, entry_price in LEG_DATA}

        updated = _persist_complete_history_marks(self.trade, self.db, marks, datetime.utcnow())

        self.assertTrue(updated)
        self.assertEqual(self.trade.pnl_basis, "LAST_MARK")
        self.assertEqual(self.trade.gross_pnl, self.trade.realized_pnl)
        self.assertTrue(all(leg.last_ltp is not None for leg in self.db.query(PositionHistoryLeg).all()))

    def test_incomplete_quote_set_does_not_overwrite_captured_snapshot(self):
        original = (self.trade.realized_pnl, self.trade.charges, self.trade.net_pnl)
        marks_before = [leg.last_ltp for leg in self.db.query(PositionHistoryLeg).all()]

        updated = _persist_complete_history_marks(self.trade, self.db, {
            "CALL BUY": 3.0,
            "CALL SELL": 10.0,
            "PUT SELL": None,
            "PUT BUY": 2.0,
        })

        self.assertFalse(updated)
        self.assertEqual((self.trade.realized_pnl, self.trade.charges, self.trade.net_pnl), original)
        self.assertEqual([leg.last_ltp for leg in self.db.query(PositionHistoryLeg).all()], marks_before)

    def test_expiry_freezes_trade_without_fabricating_exit_prices(self):
        self.trade.expiry = (date.today() - timedelta(days=1)).isoformat()
        self.db.commit()

        _expire_due_positions(self.db)

        self.assertEqual(self.trade.status, "EXPIRED")
        self.assertEqual(self.trade.snapshot_frozen, 1)
        self.assertEqual(self.trade.pnl_basis, "LAST_MARK")
        self.assertIsNotNone(self.trade.closed_at)
        legs = self.db.query(PositionHistoryLeg).filter_by(position_history_id=self.trade.id).all()
        self.assertEqual(len(legs), 4)
        self.assertTrue(all(leg.exit_price is None for leg in legs))
        self.assertTrue(all(leg.price_status == "NOT_CAPTURED" for leg in legs))
        self.assertFalse(_persist_complete_history_marks(self.trade, self.db, {
            name: 1.0 for name, _, _ in LEG_DATA
        }))


if __name__ == "__main__":
    unittest.main()
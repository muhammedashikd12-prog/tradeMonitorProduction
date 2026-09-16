import unittest
from types import SimpleNamespace

from app.services.iron_condor_calculations import calculate_position, calculate_advance_sl, advance_sl_state


def _legs():
    return {
        "CALL BUY": SimpleNamespace(leg="CALL BUY", strike=22500, quantity=25, entry_price=4),
        "CALL SELL": SimpleNamespace(leg="CALL SELL", strike=22000, quantity=25, entry_price=10),
        "PUT SELL": SimpleNamespace(leg="PUT SELL", strike=21000, quantity=25, entry_price=10),
        "PUT BUY": SimpleNamespace(leg="PUT BUY", strike=20500, quantity=25, entry_price=4),
    }


class IronCondorCalculationTests(unittest.TestCase):
    def test_credit_and_stop_loss_are_quantity_aware(self):
        result = calculate_position(_legs(), {})
        self.assertEqual(result["call_credit"]["total"], 150)
        self.assertEqual(result["put_credit"]["total"], 150)
        self.assertEqual(result["net_credit"], 300)
        self.assertEqual(result["max_initial_profit"], 300)
        self.assertEqual(result["call_sl_default"], 450)
        self.assertEqual(result["put_sl_default"], 450)


    def test_live_pnl_uses_each_leg_direction_and_quantity(self):
        result = calculate_position(_legs(), {
            "CALL BUY": 6, "CALL SELL": 14, "PUT SELL": 8, "PUT BUY": 3,
        })
        self.assertEqual(result["pnl"], {"CALL BUY": 50, "CALL SELL": -100, "PUT SELL": 50, "PUT BUY": -25})
        self.assertEqual(result["call_spread_pnl"], -50)
        self.assertEqual(result["put_spread_pnl"], 25)
        self.assertEqual(result["total_pnl"], -25)

    def test_advance_sl_calculates_credit_profit_and_loss(self):
        result = calculate_advance_sl(_legs())
        self.assertEqual(result["call"]["net_credit"], 6)
        self.assertEqual(result["call"]["max_profit"], 150)
        self.assertEqual(result["call"]["sl_loss"], 450)
        self.assertEqual(result["put"]["sl_loss"], 450)
        self.assertEqual(result["call"]["sl_trigger_price"], 22018)
        self.assertEqual(result["put"]["sl_trigger_price"], 20982)

    def test_advance_sl_rejects_invalid_credit_and_quantity(self):
        legs = _legs()
        legs["CALL BUY"].entry_price = 12
        self.assertIsNone(calculate_advance_sl(legs)["call"]["sl_trigger_price"])
        legs = _legs()
        legs["CALL BUY"].quantity = 20
        self.assertIsNone(calculate_advance_sl(legs)["call"]["sl_trigger_price"])

    def test_advance_sl_state_does_not_duplicate_trigger(self):
        state, triggered = advance_sl_state(-450, 450, 22018, 22018, "CALL")
        self.assertEqual((state, triggered), ("SL_TRIGGERED", True))
        self.assertEqual(advance_sl_state(-500, 450, 22020, 22018, "CALL", state), ("SL_TRIGGERED", False))

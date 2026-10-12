import unittest
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from app.main import ManualLeg, ManualMonitorSetup, _fyers_basket_orders
from app.models import OptionChainSnapshot, OptionQuote
from app.services.iron_condor_calculations import (
    calculate_position, calculate_advance_sl, advance_sl_state,
    calculate_position_charge_breakdown, calculate_position_charges,
    calculate_fund_requirement,
)
from app.services.iv_greeks_engine import calculate_current_atm_iv


def _legs():
    return {
        "CALL BUY": SimpleNamespace(leg="CALL BUY", strike=22500, quantity=25, entry_price=4),
        "CALL SELL": SimpleNamespace(leg="CALL SELL", strike=22000, quantity=25, entry_price=10),
        "PUT SELL": SimpleNamespace(leg="PUT SELL", strike=21000, quantity=25, entry_price=10),
        "PUT BUY": SimpleNamespace(leg="PUT BUY", strike=20500, quantity=25, entry_price=4),
    }


class IronCondorCalculationTests(unittest.TestCase):
    def test_current_atm_iv_averages_nearest_live_call_and_put(self):
        chain = OptionChainSnapshot(
            spot=22010,
            timestamp=datetime.now(),
            expiry=date.today() + timedelta(days=4),
            quotes=[
                OptionQuote(strike=22000, option_type="CE", ltp=100, bid=99, ask=101),
                OptionQuote(strike=22000, option_type="PE", ltp=90, bid=89, ask=91),
                OptionQuote(strike=22100, option_type="CE", ltp=60, bid=59, ask=61),
            ],
        )
        with patch("app.services.iv_greeks_engine.black_scholes_iv_estimate", side_effect=[12.0, 16.0]):
            self.assertEqual(calculate_current_atm_iv(chain), 14.0)

    def test_current_atm_iv_returns_none_without_a_valid_call_put_pair(self):
        chain = OptionChainSnapshot(
            spot=22000,
            timestamp=datetime.now(),
            expiry=date.today() + timedelta(days=4),
            quotes=[OptionQuote(strike=22000, option_type="CE", ltp=100, bid=99, ask=101)],
        )
        self.assertIsNone(calculate_current_atm_iv(chain))

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

    def test_charge_breakdown_preserves_existing_total(self):
        breakdown = calculate_position_charge_breakdown(_legs(), {
            "CALL BUY": 6, "CALL SELL": 14, "PUT SELL": 8, "PUT BUY": 3,
        })
        self.assertEqual(set(breakdown), {"brokerage", "stt", "exchange_transaction", "gst", "sebi", "stamp_duty", "other"})
        self.assertAlmostEqual(sum(breakdown.values()), calculate_position_charges(_legs(), {
            "CALL BUY": 6, "CALL SELL": 14, "PUT SELL": 8, "PUT BUY": 3,
        }))

    def test_fund_requirement_breakdown_includes_basket_margin_fields(self):
        result = calculate_fund_requirement(_legs(), broker_margin={"margin_total": 1234.5})
        self.assertIn("gross_margin_requirement", result)
        self.assertIn("hedge_benefit", result)
        self.assertIn("net_margin_requirement", result)
        self.assertIn("short_leg_premium_credit", result)
        self.assertIn("long_leg_premium_debit", result)
        self.assertIn("net_premium_cash_flow", result)
        self.assertIn("estimated_charges", result)
        self.assertIn("final_funds_required", result)
        self.assertAlmostEqual(result["final_funds_required"], 1234.5 + result["estimated_charges"])
        self.assertAlmostEqual(result["final_estimated_fund_requirement"], 1234.5 + result["estimated_charges"])

    def test_fund_requirement_does_not_infer_margin_from_premiums(self):
        result = calculate_fund_requirement(_legs())
        self.assertEqual(result["margin_source"], "UNAVAILABLE")
        self.assertIsNone(result["gross_margin_requirement"])
        self.assertIsNone(result["hedge_benefit"])
        self.assertIsNone(result["net_margin_requirement"])
        self.assertIsNone(result["final_funds_required"])
        self.assertGreater(result["net_premium_cash_flow"], 0)

    def test_manual_monitor_uses_master_quantity_and_multiplier(self):
        setup = ManualMonitorSetup(
            expiry=date.today(),
            base_quantity=65,
            multiplier=2,
            legs=[
                ManualLeg(leg="CALL BUY", symbol="NSE:ABC-CE-1", strike=21400, quantity=65, entry_price=100),
                ManualLeg(leg="CALL SELL", symbol="NSE:ABC-CE-2", strike=23150, quantity=65, entry_price=100),
                ManualLeg(leg="PUT SELL", symbol="NSE:ABC-PE-2", strike=22500, quantity=65, entry_price=100),
                ManualLeg(leg="PUT BUY", symbol="NSE:ABC-PE-1", strike=22150, quantity=65, entry_price=100),
            ],
        )
        self.assertEqual(setup.effective_quantity, 130)
        self.assertTrue(all(leg.quantity == 130 for leg in setup.legs))

    def test_fyers_basket_orders_keep_effective_quantity_and_buy_hedges_first(self):
        setup = ManualMonitorSetup(
            expiry=date.today(), base_quantity=65, multiplier=2,
            legs=[
                ManualLeg(leg="CALL BUY", symbol="NSE:ABC-CE-1", strike=23750, quantity=65, entry_price=2.4),
                ManualLeg(leg="CALL SELL", symbol="NSE:ABC-CE-2", strike=23150, quantity=65, entry_price=9.4),
                ManualLeg(leg="PUT SELL", symbol="NSE:ABC-PE-2", strike=22500, quantity=65, entry_price=10.35),
                ManualLeg(leg="PUT BUY", symbol="NSE:ABC-PE-1", strike=22250, quantity=65, entry_price=2.5),
            ],
        )
        orders = _fyers_basket_orders(setup.legs)
        self.assertEqual([order["qty"] for order in orders], [130, 130, 130, 130])
        self.assertEqual([order["side"] for order in orders], [1, 1, -1, -1])
        self.assertTrue(all(order["productType"] == "MARGIN" for order in orders))

    def test_master_quantity_rejects_invalid_values(self):
        with self.assertRaises(ValueError):
            ManualMonitorSetup(
                expiry=date.today(),
                base_quantity=0,
                multiplier=1,
                legs=[
                    ManualLeg(leg="CALL BUY", symbol="NSE:ABC-CE-1", strike=21400, quantity=1, entry_price=100),
                    ManualLeg(leg="CALL SELL", symbol="NSE:ABC-CE-2", strike=23150, quantity=1, entry_price=100),
                    ManualLeg(leg="PUT SELL", symbol="NSE:ABC-PE-2", strike=22500, quantity=1, entry_price=100),
                    ManualLeg(leg="PUT BUY", symbol="NSE:ABC-PE-1", strike=22150, quantity=1, entry_price=100),
                ],
            )

        with self.assertRaises(ValueError):
            ManualMonitorSetup(
                expiry=date.today(),
                base_quantity=65,
                multiplier=0,
                legs=[
                    ManualLeg(leg="CALL BUY", symbol="NSE:ABC-CE-1", strike=21400, quantity=65, entry_price=100),
                    ManualLeg(leg="CALL SELL", symbol="NSE:ABC-CE-2", strike=23150, quantity=65, entry_price=100),
                    ManualLeg(leg="PUT SELL", symbol="NSE:ABC-PE-2", strike=22500, quantity=65, entry_price=100),
                    ManualLeg(leg="PUT BUY", symbol="NSE:ABC-PE-1", strike=22150, quantity=65, entry_price=100),
                ],
            )

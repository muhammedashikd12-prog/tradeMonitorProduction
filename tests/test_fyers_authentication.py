import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.brokers.base import BrokerConnectionError
from app.brokers import fyers_client
from app import main


class FakeSession:
    token_response = {"s": "ok", "access_token": "fresh-token"}

    def __init__(self, **kwargs):
        self.auth_code = None

    def generate_authcode(self):
        return "https://login.fyers.in/fake"

    def set_token(self, auth_code):
        self.auth_code = auth_code

    def generate_token(self):
        return self.token_response


class FakeFyersModel:
    quote_response = {"s": "ok", "d": [{"v": {"lp": 22000}}]}
    quote_calls = 0

    def __init__(self, **kwargs):
        pass

    def quotes(self, payload):
        type(self).quote_calls += 1
        return type(self).quote_response


class FakeFyersSdk:
    SessionModel = FakeSession
    FyersModel = FakeFyersModel


class FyersAuthenticationTests(unittest.TestCase):
    def setUp(self):
        FakeFyersModel.quote_response = {"s": "ok", "d": [{"v": {"lp": 22000}}]}
        FakeFyersModel.quote_calls = 0
        FakeSession.token_response = {"s": "ok", "access_token": "fresh-token"}

    def client(self, token=""):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return FyersClientFactory(directory.name, token)

    def test_missing_token_requires_authentication(self):
        with patch.object(fyers_client, "fyersModel", FakeFyersSdk):
            client = self.client()
            self.assertEqual(client.auth_status()["state"], "AUTHENTICATION_REQUIRED")
            self.assertFalse(client.auth_status()["connected"])

    def test_valid_token_is_connected_only_after_health_check(self):
        with patch.object(fyers_client, "fyersModel", FakeFyersSdk):
            client = self.client(token="valid-token")
            self.assertFalse(client.is_connected())
            status = client.auth_status()
            self.assertEqual(status["state"], "CONNECTED")
            self.assertTrue(status["connected"])
            self.assertEqual(FakeFyersModel.quote_calls, 1)

    def test_invalid_token_code_15_is_latched_and_removed(self):
        FakeFyersModel.quote_response = {"s": "error", "code": -15, "message": "Please provide valid token"}
        with patch.object(fyers_client, "fyersModel", FakeFyersSdk):
            client = self.client(token="expired-token")
            status = client.auth_status()
            self.assertEqual(status["state"], "AUTHENTICATION_FAILED")
            self.assertFalse(status["connected"])
            with self.assertRaises(BrokerConnectionError):
                client.get_expiries("NSE:NIFTY50-INDEX")
            self.assertEqual(FakeFyersModel.quote_calls, 1)
            self.assertEqual(client.access_token, "")

    def test_successful_callback_persists_token_without_returning_it(self):
        with patch.object(fyers_client, "fyersModel", FakeFyersSdk):
            client = self.client()
            result = client.exchange_auth_code("auth-code")
            self.assertIsNone(result)
            self.assertTrue(client.verify_connection())
            stored = json.loads(client.token_file.read_text(encoding="utf-8"))
            self.assertEqual(stored["access_token"], "fresh-token")

    def test_failed_callback_does_not_persist_token(self):
        FakeSession.token_response = {"s": "error", "message": "invalid code"}
        with patch.object(fyers_client, "fyersModel", FakeFyersSdk):
            client = self.client()
            with self.assertRaises(BrokerConnectionError):
                client.exchange_auth_code("bad-code")
            self.assertFalse(client.token_file.exists())

    def test_uppercase_ok_status_is_accepted(self):
        FakeSession.token_response = {"s": "OK", "access_token": "uppercase-token"}
        FakeFyersModel.quote_response = {"s": "OK", "d": [{"v": {"lp": 22000}}]}
        with patch.object(fyers_client, "fyersModel", FakeFyersSdk):
            client = self.client()
            client.exchange_auth_code("auth-code")
            self.assertTrue(client.verify_connection())
            self.assertEqual(client.access_token, "uppercase-token")

    def test_token_file_survives_client_reload(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(fyers_client, "fyersModel", FakeFyersSdk):
            token_file = str(Path(directory) / "fyers-token.json")
            first = fyers_client.FyersClient("app", "secret", "redirect", token_file=token_file)
            first.exchange_auth_code("auth-code")
            reloaded = fyers_client.FyersClient("app", "secret", "redirect", token_file=token_file)
            self.assertEqual(reloaded.access_token, "fresh-token")
            self.assertEqual(reloaded.auth_status()["state"], "CONNECTED")

    def test_successful_callback_redirects_without_auth_data(self):
        class Broker:
            def exchange_auth_code(self, auth_code):
                self.auth_code = auth_code

            def verify_connection(self):
                return True

        with patch.object(main, "broker", Broker()):
            response = main.fyers_callback(auth_code="one-time-code")
        self.assertEqual(response.headers["location"], "/?fyers=connected")
        self.assertNotIn("token", response.headers["location"])

    def test_failed_callback_redirects_without_error_details(self):
        class Broker:
            def exchange_auth_code(self, auth_code):
                raise BrokerConnectionError("exchange rejected")

        with patch.object(main, "broker", Broker()):
            response = main.fyers_callback(auth_code="bad-code")
        self.assertEqual(response.headers["location"], "/?fyers=failed")
        self.assertNotIn("bad-code", response.headers["location"])


def FyersClientFactory(directory, token):
    return fyers_client.FyersClient(
        "app-id",
        "secret",
        "http://localhost/callback",
        access_token=token,
        token_file=str(Path(directory) / "fyers-token.json"),
    )


if __name__ == "__main__":
    unittest.main()

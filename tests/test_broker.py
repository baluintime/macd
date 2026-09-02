"""Credentials, the OAuth exchange, and the guards around a real order."""

import json
import os
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from macd_desk.broker.tokens import IST, Token, TokenStore, expiry_after
from macd_desk.broker.upstox import UpstoxClient, UpstoxError
from macd_desk.config import UpstoxSettings, load_settings, mask, parse_env_file


class FakeTransport:
    """Stands in for urllib: scripted responses, and a record of what was sent."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append({"method": method, "url": url, "headers": dict(headers),
                           "body": body.decode() if body else ""})
        status, payload = self.responses.pop(0) if self.responses else (200, {})
        return status, json.dumps(payload).encode()


def settings(**overrides):
    base = {"api_key": "KEY123", "api_secret": "SECRET456",
            "redirect_uri": "http://127.0.0.1:8000/broker/callback"}
    base.update(overrides)
    return UpstoxSettings(**base)


class ConfigTests(unittest.TestCase):
    def test_env_file_is_read_and_overridden_by_the_real_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text(
                "# a comment\n"
                "UPSTOX_API_KEY=from-file\n"
                'export UPSTOX_API_SECRET="quoted-secret"\n'
                "UPSTOX_REDIRECT_URI=http://127.0.0.1:8000/broker/callback\n")

            loaded = load_settings(env={}, env_file=env_file)
            self.assertEqual(loaded.upstox.api_key, "from-file")
            self.assertEqual(loaded.upstox.api_secret, "quoted-secret")

            overridden = load_settings(env={"UPSTOX_API_KEY": "from-env"}, env_file=env_file)
            self.assertEqual(overridden.upstox.api_key, "from-env")

    def test_missing_credentials_are_named_not_guessed(self):
        upstox = load_settings(env={}, env_file=Path("/nonexistent")).upstox
        self.assertFalse(upstox.configured)
        self.assertEqual(upstox.missing,
                         ["UPSTOX_API_KEY", "UPSTOX_API_SECRET", "UPSTOX_REDIRECT_URI"])

    def test_live_trading_is_off_unless_explicitly_enabled(self):
        for value in ("", "no", "false", "0", "off"):
            self.assertFalse(load_settings(env={"UPSTOX_LIVE_TRADING": value},
                                           env_file=Path("/nonexistent")).upstox.live_trading_enabled)
        for value in ("yes", "true", "1", "enabled"):
            self.assertTrue(load_settings(env={"UPSTOX_LIVE_TRADING": value},
                                          env_file=Path("/nonexistent")).upstox.live_trading_enabled)

    def test_the_public_view_never_carries_the_secret(self):
        # A realistic key is a UUID; only its ends are shown.
        real_key = "38a37735-3825-4ac6-8a76-e7e5da85ebdd"
        public = settings(api_key=real_key).public()
        self.assertNotIn("SECRET456", json.dumps(public))
        self.assertTrue(public["apiSecretSet"])
        self.assertEqual(public["apiKey"], "38a3••••••ebdd")
        self.assertNotIn(real_key, json.dumps(public))

    def test_a_key_too_short_to_hint_at_is_masked_whole(self):
        self.assertEqual(settings(api_key="KEY123").public()["apiKey"], "••••••")

    def test_masking_never_reveals_a_short_secret(self):
        self.assertEqual(mask("abcd"), "••••")
        self.assertNotIn("secret", mask("supersecretvalue"))

    def test_a_malformed_env_line_is_skipped_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text("garbage line without equals\nUPSTOX_API_KEY=ok\n")
            self.assertEqual(parse_env_file(env_file), {"UPSTOX_API_KEY": "ok"})


class TokenTests(unittest.TestCase):
    def test_tokens_expire_at_the_next_three_thirty_ist(self):
        evening = datetime(2026, 9, 2, 20, 0, tzinfo=IST)
        self.assertEqual(expiry_after(evening), datetime(2026, 9, 3, 3, 30, tzinfo=IST))
        small_hours = datetime(2026, 9, 2, 2, 30, tzinfo=IST)
        self.assertEqual(expiry_after(small_hours), datetime(2026, 9, 2, 3, 30, tzinfo=IST))

    def test_an_expired_token_is_not_offered(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TokenStore(Path(tmp) / "token.json")
            stale = Token("abc", datetime.now(timezone.utc) - timedelta(days=2),
                          datetime.now(timezone.utc) - timedelta(days=1))
            store.save(stale)
            self.assertIsNotNone(store.load())
            self.assertIsNone(store.valid_token())

    def test_the_token_file_is_written_owner_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "token.json"
            TokenStore(path).save(Token.issued_now("secret-token"))
            mode = stat.S_IMODE(os.stat(path).st_mode)
            self.assertEqual(mode, 0o600)

    def test_the_public_view_never_carries_the_token(self):
        token = Token.issued_now("secret-token", {"user_name": "Trader"})
        self.assertNotIn("secret-token", json.dumps(token.public()))


class OAuthTests(unittest.TestCase):
    def test_login_url_carries_the_registered_redirect(self):
        url = UpstoxClient(settings()).login_url(state="abc")
        self.assertIn("/v2/login/authorization/dialog", url)
        self.assertIn("client_id=KEY123", url)
        self.assertIn("response_type=code", url)
        self.assertIn("state=abc", url)

    def test_login_is_refused_until_the_credentials_exist(self):
        with self.assertRaises(UpstoxError) as caught:
            UpstoxClient(UpstoxSettings()).login_url()
        self.assertIn("UPSTOX_API_KEY", str(caught.exception))

    def test_the_code_exchange_posts_the_secret_and_caches_the_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            transport = FakeTransport((200, {"access_token": "tok-1", "user_name": "Trader",
                                             "user_id": "U1"}))
            store = TokenStore(Path(tmp) / "token.json")
            client = UpstoxClient(settings(), token_store=store, transport=transport)

            token = client.exchange_code("auth-code")
            sent = transport.calls[0]
            self.assertEqual(sent["method"], "POST")
            self.assertIn("grant_type=authorization_code", sent["body"])
            self.assertIn("client_secret=SECRET456", sent["body"])
            self.assertEqual(token.user_name, "Trader")
            self.assertEqual(store.valid_token().access_token, "tok-1")

    def test_an_upstox_error_surfaces_its_own_message(self):
        transport = FakeTransport((400, {"status": "error", "errors": [
            {"errorCode": "UDAPI100068", "message": "Invalid client_id or redirect_uri"}]}))
        client = UpstoxClient(settings(), transport=transport)
        with self.assertRaises(UpstoxError) as caught:
            client.exchange_code("bad")
        self.assertIn("Invalid client_id", str(caught.exception))
        self.assertIn("UDAPI100068", str(caught.exception))


class AuthorisedRequestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = TokenStore(Path(self.tmp.name) / "token.json")
        self.store.save(Token.issued_now("tok-1"))

    def tearDown(self):
        self.tmp.cleanup()

    def client(self, *responses, **overrides):
        self.transport = FakeTransport(*responses)
        return UpstoxClient(settings(**overrides), token_store=self.store,
                            transport=self.transport)

    def test_calls_without_a_token_fail_before_reaching_the_network(self):
        empty = TokenStore(Path(self.tmp.name) / "missing.json")
        client = UpstoxClient(settings(), token_store=empty, transport=FakeTransport())
        with self.assertRaises(UpstoxError) as caught:
            client.profile()
        self.assertIn("Not connected", str(caught.exception))

    def test_the_bearer_token_is_sent(self):
        client = self.client((200, {"data": {"user_name": "Trader"}}))
        client.profile()
        self.assertEqual(self.transport.calls[0]["headers"]["Authorization"], "Bearer tok-1")

    def test_ltp_maps_instrument_keys_to_prices(self):
        client = self.client((200, {"data": {
            "NSE_FO:NIFTY": {"instrument_token": "NSE_FO|CE1", "last_price": 255.5}}}))
        self.assertEqual(client.ltp(["NSE_FO|CE1"]), {"NSE_FO|CE1": 255.5})

    def test_candles_are_returned_oldest_first(self):
        newest_first = [["2026-09-02T10:05:00+05:30", 0, 0, 0, 102, 0],
                        ["2026-09-02T10:00:00+05:30", 0, 0, 0, 101, 0]]
        client = self.client((200, {"data": {"candles": newest_first}}))
        candles = client.intraday_candles("NSE_INDEX|Nifty 50", "5m")
        self.assertEqual([row[4] for row in candles], [101, 102])

    def test_the_warmup_window_asks_for_three_days(self):
        from datetime import date
        client = self.client((200, {"data": {"candles": []}}))
        client.historical_candles("NSE_INDEX|Nifty 50", "1m", days=3, today=date(2026, 9, 2))
        self.assertIn("/minutes/1/2026-09-02/2026-08-30", self.transport.calls[0]["url"])

    def test_an_unsupported_timeframe_is_refused(self):
        client = self.client()
        with self.assertRaises(UpstoxError):
            client.intraday_candles("NSE_INDEX|Nifty 50", "15m")


class OrderGuardTests(AuthorisedRequestTests):
    def test_placing_an_order_needs_the_live_flag(self):
        client = self.client((200, {"data": {"order_ids": ["X1"]}}))
        with self.assertRaises(UpstoxError) as caught:
            client.place_order(instrument_key="NSE_FO|CE1", quantity=75, transaction_type="BUY")
        self.assertIn("UPSTOX_LIVE_TRADING", str(caught.exception))
        self.assertEqual(self.transport.calls, [])          # nothing left the process

    def test_an_armed_order_goes_to_the_hft_host(self):
        client = self.client((200, {"data": {"order_ids": ["X1"]}}), live_trading_enabled=True)
        result = client.place_order(instrument_key="NSE_FO|CE1", quantity=75,
                                    transaction_type="BUY")
        call = self.transport.calls[0]
        self.assertTrue(call["url"].startswith("https://api-hft.upstox.com/v3/order/place"))
        self.assertEqual(json.loads(call["body"])["transaction_type"], "BUY")
        self.assertEqual(result["order_ids"], ["X1"])

    def test_a_nonsense_order_is_rejected_locally(self):
        client = self.client(live_trading_enabled=True)
        with self.assertRaises(UpstoxError):
            client.place_order(instrument_key="NSE_FO|CE1", quantity=0, transaction_type="BUY")
        with self.assertRaises(UpstoxError):
            client.place_order(instrument_key="NSE_FO|CE1", quantity=75, transaction_type="HOLD")
        self.assertEqual(self.transport.calls, [])


if __name__ == "__main__":
    unittest.main()

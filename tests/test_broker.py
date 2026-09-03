"""Credentials, the OAuth exchange, and the guards around a real order."""

import json
import logging
import os
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from macd_desk.broker.tokens import IST, Token, TokenStore, expiry_after
from macd_desk.broker.upstox import UpstoxClient, UpstoxError
from macd_desk.config import UpstoxSettings, load_settings, mask, parse_env_file


# The client logs failed responses on purpose; keep that out of test output.
logging.getLogger("macd_desk.broker.upstox").setLevel(logging.CRITICAL)


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


class RequestHeaderTests(AuthorisedRequestTests):
    def test_every_request_identifies_itself(self):
        # urllib's default agent draws a bodyless 403 from some edges.
        client = self.client((200, {"data": {}}))
        client.profile()
        agent = self.transport.calls[0]["headers"]["User-Agent"]
        self.assertIn("macd-desk", agent)
        self.assertNotIn("urllib", agent)

    def test_v2_endpoints_carry_the_api_version_header(self):
        client = self.client((200, {"data": {}}))
        client.profile()                                    # /v2/user/profile
        self.assertEqual(self.transport.calls[0]["headers"]["Api-Version"], "2.0")

    def test_v3_endpoints_do_not(self):
        client = self.client((200, {"data": {}}))
        client.ltp(["NSE_FO|CE1"])                          # /v3/market-quote/ltp
        self.assertNotIn("Api-Version", self.transport.calls[0]["headers"])

    def test_the_token_exchange_identifies_itself_too(self):
        transport = FakeTransport((200, {"access_token": "tok"}))
        UpstoxClient(settings(), token_store=self.store, transport=transport).exchange_code("c")
        self.assertIn("macd-desk", transport.calls[0]["headers"]["User-Agent"])


class ErrorReportingTests(unittest.TestCase):
    """A bare status code is not a diagnosis — the message has to carry evidence."""

    def client(self, status, payload_bytes):
        class RawTransport:
            calls = []

            def __call__(self, method, url, headers, body):
                return status, payload_bytes

        return UpstoxClient(settings(), token_store=TokenStore(Path("/nonexistent")),
                            transport=RawTransport())

    def test_a_bodyless_403_explains_the_likely_causes(self):
        with self.assertRaises(UpstoxError) as caught:
            self.client(403, b"").exchange_code("code")
        message = str(caught.exception)
        self.assertIn("403", message)
        self.assertIn("no body", message)
        self.assertIn("static IP", message)
        self.assertIn("redirect URI", message)
        self.assertIn("already used", message)

    def test_a_non_json_body_is_quoted_back(self):
        with self.assertRaises(UpstoxError) as caught:
            self.client(403, b"<html><title>403 Forbidden</title></html>").exchange_code("c")
        self.assertIn("403 Forbidden", str(caught.exception))

    def test_upstox_own_error_still_wins_over_the_hint(self):
        payload = json.dumps({"status": "error", "errors": [
            {"errorCode": "UDAPI100057", "message": "Invalid auth code"}]}).encode()
        with self.assertRaises(UpstoxError) as caught:
            self.client(400, payload).exchange_code("code")
        self.assertEqual(str(caught.exception), "Invalid auth code (UDAPI100057)")

    def test_the_body_is_kept_on_the_exception(self):
        with self.assertRaises(UpstoxError) as caught:
            self.client(403, b"denied by edge").exchange_code("code")
        self.assertEqual(caught.exception.body, "denied by edge")
        self.assertEqual(caught.exception.status, 403)

    def test_a_query_string_is_redacted_before_logging(self):
        from macd_desk.broker.upstox import _redact
        self.assertEqual(_redact("https://api.upstox.com/v2/token?code=SECRET&x=1"),
                         "https://api.upstox.com/v2/token")


class DiagnoseTests(unittest.TestCase):
    def test_it_names_what_is_missing_and_exits_non_zero(self):
        import io as _io
        from contextlib import redirect_stdout

        from macd_desk import diagnose
        from macd_desk.config import Settings

        captured = _io.StringIO()
        with redirect_stdout(captured):
            code = diagnose.run(Settings(upstox=UpstoxSettings()))
        output = captured.getvalue()

        self.assertEqual(code, 1)
        self.assertIn("UPSTOX_API_KEY", output)
        self.assertIn("Redirect URI", output)

    def test_a_configured_desk_reports_the_key_masked_and_never_the_secret(self):
        import io as _io
        from contextlib import redirect_stdout

        from macd_desk import diagnose
        from macd_desk.config import Settings

        with tempfile.TemporaryDirectory() as tmp:
            upstox = settings(api_key="38a37735-3825-4ac6-8a76-e7e5da85ebdd",
                              token_file=Path(tmp) / "token.json")
            captured = _io.StringIO()
            with redirect_stdout(captured):
                diagnose.run(Settings(upstox=upstox))
            output = captured.getvalue()

        self.assertIn("38a3", output)
        self.assertNotIn("SECRET456", output)
        self.assertNotIn("e7e5da85ebdd", output.split("Public IP")[0].replace("38a3••••••ebdd", ""))


class CallbackRoutingTests(unittest.TestCase):
    """Upstox returns to the URI registered on the app, not a path we chose."""

    def app_for(self, redirect_uri):
        from macd_desk.app import create_app
        from macd_desk.config import Settings

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Keep the token in the temp dir — the default path is the working
        # directory, and a token left there would leak into other tests.
        upstox = settings(redirect_uri=redirect_uri,
                          token_file=Path(self.tmp.name) / "token.json")
        return create_app(Path(self.tmp.name) / "desk.json",
                          settings=Settings(upstox=upstox))

    def paths(self, app):
        return sorted(rule.rule for rule in app.url_map.iter_rules() if "callback" in rule.rule)

    def test_the_configured_path_is_served_alongside_the_canonical_one(self):
        app = self.app_for("http://localhost:8000/api/auth/callback")
        self.assertEqual(self.paths(app), ["/api/auth/callback", "/broker/callback"])

    def test_a_configured_callback_completes_the_login(self):
        app = self.app_for("http://localhost:8000/api/auth/callback")
        app.config["BROKER"]._transport = FakeTransport(
            (200, {"access_token": "tok-1", "user_name": "Trader"}))
        response = app.test_client().get("/api/auth/callback?code=abc&state=xyz")
        self.assertEqual(response.status_code, 302)
        self.assertIn("Connected+as+Trader", response.headers["Location"])
        self.assertEqual(app.config["BROKER"].current_token().access_token, "tok-1")

    def test_the_canonical_path_keeps_working(self):
        app = self.app_for("http://127.0.0.1:8000/broker/callback")
        self.assertEqual(self.paths(app), ["/broker/callback"])

    def test_a_path_already_in_use_is_reported_not_hijacked(self):
        app = self.app_for("http://localhost:8000/healthz")
        self.assertEqual(app.config["CALLBACK_CONFLICT"], "/healthz")
        self.assertEqual(app.test_client().get("/healthz").get_json(), {"status": "ok"})
        self.assertIn("Callback path clash",
                      app.test_client().get("/broker").get_data(as_text=True))

    def test_a_malformed_redirect_uri_does_not_break_startup(self):
        for uri in ("", "not a url", "http://localhost:8000"):
            app = self.app_for(uri)
            self.assertEqual(self.paths(app), ["/broker/callback"])

    def test_the_broker_page_shows_which_path_is_served(self):
        app = self.app_for("http://localhost:8000/api/auth/callback")
        html = app.test_client().get("/broker").get_data(as_text=True)
        self.assertIn("/api/auth/callback", html)
        self.assertIn("Callback served at", html)


class SymbolSearchTests(unittest.TestCase):
    """Tickers change; the desk has to be able to find the current one."""

    ROWS = [
        {"segment": "NSE_EQ", "trading_symbol": "TMPV", "name": "Tata Motors Passenger Vehicles",
         "instrument_key": "NSE_EQ|INE0LXG01040"},
        {"segment": "NSE_EQ", "trading_symbol": "TATAMOTORS", "name": "Tata Motors",
         "instrument_key": "NSE_EQ|INE155A01022"},
        {"segment": "NSE_EQ", "trading_symbol": "TATASTEEL", "name": "Tata Steel",
         "instrument_key": "NSE_EQ|INE081A01020"},
        {"segment": "NSE_FO", "trading_symbol": "TATAMOTORS 700 CE", "name": "Tata Motors",
         "instrument_key": "NSE_FO|1"},
    ]

    def test_an_exact_symbol_ranks_first(self):
        from macd_desk.symbols import search
        hits = search(self.ROWS, "TATAMOTORS")
        self.assertEqual(hits[0]["symbol"], "TATAMOTORS")
        self.assertEqual(hits[0]["instrument_key"], "NSE_EQ|INE155A01022")

    def test_a_company_name_finds_the_renamed_listing(self):
        from macd_desk.symbols import search
        symbols = [hit["symbol"] for hit in search(self.ROWS, "Tata Motors")]
        self.assertIn("TMPV", symbols)
        self.assertIn("TATAMOTORS", symbols)

    def test_options_rows_are_not_offered_as_underlyings(self):
        from macd_desk.symbols import search
        for hit in search(self.ROWS, "TATA"):
            self.assertTrue(hit["instrument_key"].startswith("NSE_EQ|"))

    def test_a_disconnected_desk_says_so_instead_of_failing(self):
        import io as _io
        from contextlib import redirect_stdout

        from macd_desk import symbols
        from macd_desk.config import Settings

        with tempfile.TemporaryDirectory() as tmp:
            captured = _io.StringIO()
            with redirect_stdout(captured):
                code = symbols.run("TATA", Settings(upstox=settings(
                    token_file=Path(tmp) / "none.json")))
            self.assertEqual(code, 1)
            self.assertIn("Not connected", captured.getvalue())

    def test_index_symbols_need_no_lookup(self):
        import io as _io
        from contextlib import redirect_stdout

        from macd_desk import symbols
        from macd_desk.config import Settings

        with tempfile.TemporaryDirectory() as tmp:
            captured = _io.StringIO()
            with redirect_stdout(captured):
                symbols.run("NIFTY", Settings(upstox=settings(
                    token_file=Path(tmp) / "none.json")))
            self.assertIn("NSE_INDEX|Nifty 50", captured.getvalue())

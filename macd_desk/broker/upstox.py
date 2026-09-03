"""Upstox Developer API client.

Stdlib HTTP only (urllib), so the desk keeps its single dependency. Every
request goes through `_request`, which the tests replace with a fake transport.

Endpoint versions differ across the Upstox surface — market data on the main
API host, order placement on the HFT host — so each path is named once here
rather than scattered through the engine.
"""

from __future__ import annotations

import gzip
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from ..config import UpstoxSettings
from .tokens import Token, TokenStore

# --- endpoints -----------------------------------------------------------
# Auth (v2)
AUTH_DIALOG = "/v2/login/authorization/dialog"
AUTH_TOKEN = "/v2/login/authorization/token"
LOGOUT = "/v2/logout"
# Account (v2)
PROFILE = "/v2/user/profile"
FUNDS = "/v2/user/get-funds-and-margin"
# Market data (v3 where Upstox has moved it)
LTP = "/v3/market-quote/ltp"
OHLC = "/v3/market-quote/ohlc"
FULL_QUOTE = "/v2/market-quote/quotes"
HISTORICAL = "/v3/historical-candle"           # /{key}/{unit}/{interval}/{to}/{from}
INTRADAY = "/v3/historical-candle/intraday"    # /{key}/{unit}/{interval}
FEED_AUTHORIZE = "/v3/feed/market-data-feed/authorize"
OPTION_CONTRACTS = "/v2/option/contract"       # expiries and lot sizes per underlying
OPTION_CHAIN = "/v2/option/chain"              # strikes with live premiums and greeks
# Orders (v3, HFT host)
PLACE_ORDER = "/v3/order/place"
CANCEL_ORDER = "/v3/order/cancel"
ORDER_BOOK = "/v2/order/retrieve-all"
POSITIONS = "/v2/portfolio/short-term-positions"

# Candle units accepted by the v3 historical endpoints.
UNIT_MINUTES = "minutes"

logger = logging.getLogger(__name__)

# urllib's default agent ("Python-urllib/3.x") is filtered at some edges, which
# returns a bodyless 403 that looks nothing like an API error. Identify properly.
USER_AGENT = "macd-desk/1.1 (+https://github.com/baluintime/macd)"
BODY_SNIPPET = 400

HTTP_HINTS = {
    403: ("Upstox refused the request outright. Usual causes, in order: the app has a "
          "static IP restriction that does not include this machine's public IP; the "
          "redirect URI sent does not match the one registered on the app; or the "
          "authorization code was already used or has expired — each code works once."),
    401: "The access token is missing, expired, or was issued to a different app.",
    429: "Rate limited by Upstox — slow the polling interval and retry.",
}

TIMEFRAME_TO_INTERVAL = {"1m": (UNIT_MINUTES, 1), "5m": (UNIT_MINUTES, 5)}


class UpstoxError(RuntimeError):
    """Any failure talking to Upstox, with the broker's own message if given."""

    def __init__(self, message: str, status: Optional[int] = None,
                 payload: Optional[Any] = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.payload = payload
        self.body = body


class UpstoxClient:
    def __init__(self, settings: UpstoxSettings, token_store: Optional[TokenStore] = None,
                 transport=None):
        self.settings = settings
        self.tokens = token_store or TokenStore(settings.token_file)
        # Injected in tests; production uses urllib.
        self._transport = transport or self._urlopen

    # ---------------------------------------------------------------- HTTP

    def _urlopen(self, method: str, url: str, headers: Mapping[str, str],
                 body: Optional[bytes]) -> tuple:
        request = urllib.request.Request(url, data=body, method=method,
                                         headers=dict(headers))
        try:
            with urllib.request.urlopen(request, timeout=self.settings.timeout_seconds) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()
        except urllib.error.URLError as error:
            raise UpstoxError(f"Could not reach Upstox: {error.reason}") from error

    def _request(self, method: str, url: str, *, token: Optional[str] = None,
                 params: Optional[Mapping[str, Any]] = None,
                 form: Optional[Mapping[str, Any]] = None,
                 payload: Optional[Mapping[str, Any]] = None) -> Any:
        if params:
            url = f"{url}?{urllib.parse.urlencode(params, safe='|,')}"

        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        if "/v2/" in url:
            headers["Api-Version"] = "2.0"
        body: Optional[bytes] = None
        if form is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            body = urllib.parse.urlencode(form).encode()
        elif payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode()
        if token:
            headers["Authorization"] = f"Bearer {token}"

        status, raw = self._transport(method, url, headers, body)
        text = raw.decode("utf-8", "replace") if raw else ""
        try:
            parsed = json.loads(text) if text else {}
        except json.JSONDecodeError:
            parsed = None

        if status >= 400 or (isinstance(parsed, dict) and parsed.get("status") == "error"):
            # The body is the only evidence when Upstox answers without a coded
            # error, so log it: the URL and status carry no credentials.
            logger.warning("Upstox %s %s -> HTTP %s %s", method, _redact(url), status,
                           text[:BODY_SNIPPET] or "(empty body)")
            raise UpstoxError(_error_message(parsed, status, text), status, parsed,
                              text[:BODY_SNIPPET])
        if parsed is None:
            raise UpstoxError(f"Unreadable response from Upstox (HTTP {status}): "
                              f"{text[:BODY_SNIPPET]}", status, body=text[:BODY_SNIPPET])
        return parsed

    def _authorised(self, method: str, path: str, *, host: Optional[str] = None,
                    **kwargs) -> Any:
        token = self.tokens.valid_token()
        if not token:
            raise UpstoxError("Not connected to Upstox — authorise the app first.")
        base = host or self.settings.api_base
        return self._request(method, f"{base}{path}", token=token.access_token, **kwargs)

    # ---------------------------------------------------------------- auth

    def login_url(self, state: str = "") -> str:
        """The page to send the operator to; Upstox redirects back with a code."""
        if not self.settings.configured:
            raise UpstoxError("Upstox credentials are not configured — "
                              f"missing {', '.join(self.settings.missing)}.")
        params = {
            "client_id": self.settings.api_key,
            "redirect_uri": self.settings.redirect_uri,
            "response_type": "code",
        }
        if state:
            params["state"] = state
        return f"{self.settings.api_base}{AUTH_DIALOG}?{urllib.parse.urlencode(params)}"

    def exchange_code(self, code: str) -> Token:
        """Swap the authorization code for the day's access token, then cache it."""
        response = self._request(
            "POST", f"{self.settings.api_base}{AUTH_TOKEN}",
            form={
                "code": code,
                "client_id": self.settings.api_key,
                "client_secret": self.settings.api_secret,
                "redirect_uri": self.settings.redirect_uri,
                "grant_type": "authorization_code",
            })
        access_token = response.get("access_token")
        if not access_token:
            raise UpstoxError("Upstox returned no access token.", payload=response)

        token = Token.issued_now(access_token, response)
        self.tokens.save(token)
        return token

    def disconnect(self) -> None:
        self.tokens.clear()

    def current_token(self) -> Optional[Token]:
        return self.tokens.valid_token()

    # ------------------------------------------------------------- account

    def profile(self) -> Dict[str, Any]:
        return self._authorised("GET", PROFILE).get("data", {})

    def funds(self) -> Dict[str, Any]:
        return self._authorised("GET", FUNDS).get("data", {})

    # --------------------------------------------------------- market data

    def ltp(self, instrument_keys: Sequence[str]) -> Dict[str, float]:
        """Last traded price per instrument key."""
        if not instrument_keys:
            return {}
        data = self._authorised("GET", LTP, params={
            "instrument_key": ",".join(instrument_keys)}).get("data", {})
        prices: Dict[str, float] = {}
        for entry in data.values():
            key = entry.get("instrument_token") or entry.get("instrument_key")
            price = entry.get("last_price")
            if key and price is not None:
                prices[key] = float(price)
        return prices

    def intraday_candles(self, instrument_key: str, timeframe: str) -> List[List[Any]]:
        """Today's candles at 1-min or 5-min, oldest first."""
        unit, interval = _interval_for(timeframe)
        path = f"{INTRADAY}/{urllib.parse.quote(instrument_key, safe='|')}/{unit}/{interval}"
        data = self._authorised("GET", path).get("data", {})
        return _oldest_first(data.get("candles", []))

    def historical_candles(self, instrument_key: str, timeframe: str,
                           days: int = 3, today: Optional[date] = None) -> List[List[Any]]:
        """The warmup window the SRS requires before the first live MACD value."""
        unit, interval = _interval_for(timeframe)
        to_date = today or date.today()
        from_date = to_date - timedelta(days=days)
        path = (f"{HISTORICAL}/{urllib.parse.quote(instrument_key, safe='|')}"
                f"/{unit}/{interval}/{to_date.isoformat()}/{from_date.isoformat()}")
        data = self._authorised("GET", path).get("data", {})
        return _oldest_first(data.get("candles", []))

    def feed_authorize(self) -> str:
        """Authorised wss:// URL for the market data feed."""
        data = self._authorised("GET", FEED_AUTHORIZE).get("data", {})
        url = data.get("authorized_redirect_uri") or data.get("authorizedRedirectUri")
        if not url:
            raise UpstoxError("Upstox returned no feed URL.", payload=data)
        return url

    def option_contracts(self, underlying_key: str,
                         expiry: Optional[date] = None) -> List[Dict[str, Any]]:
        """Every option contract on an underlying — expiries, strikes, lot sizes."""
        params: Dict[str, Any] = {"instrument_key": underlying_key}
        if expiry:
            params["expiry_date"] = expiry.isoformat()
        return self._authorised("GET", OPTION_CONTRACTS, params=params).get("data", []) or []

    def option_chain(self, underlying_key: str, expiry: date) -> List[Dict[str, Any]]:
        """The live chain for one expiry: premium and greeks per strike, both sides."""
        return self._authorised("GET", OPTION_CHAIN, params={
            "instrument_key": underlying_key,
            "expiry_date": expiry.isoformat(),
        }).get("data", []) or []

    def instruments(self) -> List[Dict[str, Any]]:
        """The daily instrument master — how a symbol maps to an instrument key."""
        request = urllib.request.Request(self.settings.instruments_url,
                                         headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.settings.timeout_seconds) as response:
                raw = response.read()
        except urllib.error.URLError as error:
            raise UpstoxError(f"Could not download the instrument master: {error.reason}")
        if self.settings.instruments_url.endswith(".gz"):
            raw = gzip.decompress(raw)
        return json.loads(raw.decode("utf-8"))

    # -------------------------------------------------------------- orders

    def place_order(self, *, instrument_key: str, quantity: int, transaction_type: str,
                    product: str = "I", order_type: str = "MARKET", price: float = 0.0,
                    validity: str = "DAY", tag: str = "macd-desk",
                    disclosed_quantity: int = 0, trigger_price: float = 0.0,
                    is_amo: bool = False, slice_order: bool = True) -> Dict[str, Any]:
        """Send a real order. Callers must have cleared the live-trading guard."""
        if not self.settings.live_trading_enabled:
            raise UpstoxError(
                "Live trading is disabled — set UPSTOX_LIVE_TRADING=yes to arm it.")
        if transaction_type not in ("BUY", "SELL"):
            raise UpstoxError(f"Unknown transaction type {transaction_type!r}.")
        if quantity <= 0:
            raise UpstoxError("Order quantity must be positive.")

        payload = {
            "quantity": int(quantity),
            "product": product,
            "validity": validity,
            "price": float(price),
            "tag": tag,
            "instrument_token": instrument_key,
            "order_type": order_type,
            "transaction_type": transaction_type,
            "disclosed_quantity": int(disclosed_quantity),
            "trigger_price": float(trigger_price),
            "is_amo": bool(is_amo),
            "slice": bool(slice_order),
        }
        return self._authorised("POST", PLACE_ORDER, host=self.settings.hft_base,
                                payload=payload).get("data", {})

    def positions(self) -> List[Dict[str, Any]]:
        return self._authorised("GET", POSITIONS).get("data", []) or []


# ------------------------------------------------------------------ helpers

def _interval_for(timeframe: str):
    try:
        return TIMEFRAME_TO_INTERVAL[timeframe]
    except KeyError:
        raise UpstoxError(f"Unsupported engine timeframe {timeframe!r}.")


def _oldest_first(candles: Iterable[Sequence[Any]]) -> List[List[Any]]:
    """Upstox returns newest-first; the indicators need chronological order."""
    rows = [list(candle) for candle in candles or []]
    if len(rows) > 1 and str(rows[0][0]) > str(rows[-1][0]):
        rows.reverse()
    return rows


def _redact(url: str) -> str:
    """Drop the query string — it can carry an auth code."""
    return url.split("?", 1)[0]


def _error_message(parsed: Any, status: int, text: str = "") -> str:
    """Prefer Upstox's own coded error; fall back to the body, then to a hint."""
    if isinstance(parsed, dict):
        errors = parsed.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0]
            if isinstance(first, dict):
                code = first.get("errorCode") or first.get("error_code") or ""
                message = first.get("message") or first.get("error") or ""
                if message:
                    return f"{message} ({code})" if code else message
        for key in ("message", "error_description", "error"):
            if parsed.get(key):
                return str(parsed[key])

    parts = [f"Upstox request failed (HTTP {status})"]
    snippet = " ".join(text.split())[:200]
    if snippet and not (isinstance(parsed, dict) and parsed):
        parts.append(f"Response: {snippet}")
    elif not snippet:
        parts.append("The response had no body.")
    hint = HTTP_HINTS.get(status)
    if hint:
        parts.append(hint)
    return " — ".join(parts)

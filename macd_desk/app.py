"""Flask application for the Upstox MACD options desk.

The page is rendered server-side and every figure on it is computed in Python.
The form works with JavaScript disabled — submitting it recomputes and
re-renders. With JavaScript on, the same form is posted to /api/book, which
returns the same numbers as JSON so the page updates without a reload.
"""

from __future__ import annotations

import csv
import io
import os
import secrets
from pathlib import Path
from urllib.parse import urlparse
from typing import Any, Dict, List, Mapping, Optional

from flask import (Flask, Response, jsonify, redirect, render_template, request,
                   session, url_for)

from . import analysis, charges, formatting, state as state_module
from .broker import UpstoxClient, UpstoxError
from .config import Settings, load_settings
from .engine.runner import EngineRunner
from .engine.selection import ContractSelector

FIELD_SEPARATOR = "-"


# --------------------------------------------------------------- form parsing

def _indexed(form: Mapping[str, Any], prefix: str, fields) -> List[Dict[str, Any]]:
    """Collect `prefix-<i>-<field>` inputs back into an ordered list of dicts."""
    rows: Dict[int, Dict[str, Any]] = {}
    for key in form.keys():
        parts = key.split(FIELD_SEPARATOR)
        if len(parts) != 3 or parts[0] != prefix:
            continue
        try:
            index = int(parts[1])
        except ValueError:
            continue
        if parts[2] in fields:
            rows.setdefault(index, {})[parts[2]] = form.get(key)
    return [rows[i] for i in sorted(rows)]


INSTRUMENT_FIELDS = frozenset(
    {"symbol", "kind", "side", "mode", "timeframe", "lots", "targetPoints", "lotSize", "entryPrice"})
TRADE_FIELDS = frozenset({"symbol", "side", "reason", "timeframe", "entryPrice", "exitPrice", "lots", "lotSize"})


def state_from_form(form: Mapping[str, Any], current: Mapping[str, Any]) -> Dict[str, Any]:
    """Rebuild desk state from a submitted form, falling back to what we hold."""
    instruments = _indexed(form, "inst", INSTRUMENT_FIELDS)
    trades = _indexed(form, "trade", TRADE_FIELDS)

    rates = {}
    for key in charges.DEFAULT_RATES:
        field = "rate" + FIELD_SEPARATOR + key
        if field in form:
            rates[key] = form.get(field)

    raw = {
        "instruments": instruments or current.get("instruments"),
        # An empty blotter is a legitimate state, so only fall back when the
        # form carried no trade fields at all.
        "trades": trades if any(k.startswith("trade" + FIELD_SEPARATOR) for k in form.keys())
        else current.get("trades"),
        "rates": rates or current.get("rates"),
    }
    return state_module.clean_state(raw)


def apply_action(action: str, desk: Dict[str, Any], form: Mapping[str, Any]) -> Dict[str, Any]:
    """Toolbar actions. Unknown actions simply save what was submitted."""
    if action == "add-trade":
        last = desk["trades"][-1] if desk["trades"] else None
        desk["trades"].append(state_module.clean_trade({
            "symbol": last["symbol"] if last else "NIFTY",
            "side": "CE",
            "reason": "Target",
            "entryPrice": last["entryPrice"] if last else 100,
            "exitPrice": last["entryPrice"] if last else 100,
            "lots": 1,
            "lotSize": last["lotSize"] if last else charges.DEFAULT_LOT_SIZES["NIFTY"],
            "timeframe": "5m",
        }))
    elif action == "delete-trade":
        try:
            index = int(form.get("delete-index", ""))
        except (TypeError, ValueError):
            index = -1
        if 0 <= index < len(desk["trades"]):
            desk["trades"].pop(index)
    elif action == "clear-trades":
        desk["trades"] = []
    elif action == "reset-rates":
        desk["rates"] = dict(charges.DEFAULT_RATES)
    return desk


# --------------------------------------------------------------- view model

def build_view(desk: Mapping[str, Any],
               snapshots: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Everything the template and the JSON API render, computed once.

    A card's projection needs a premium, and the only premium this app will use
    is the live one from the option chain. Without a live snapshot the figures
    are shown as unavailable rather than invented.
    """
    rates = desk["rates"]
    snapshots = snapshots or {}
    book = charges.summarize(desk["trades"], rates)
    totals = book["totals"]

    instruments = []
    for instrument in desk["instruments"]:
        contract = snapshots.get(instrument["symbol"])
        projection = None
        if contract and contract.get("premium"):
            projection = charges.project_at_target({
                **instrument,
                "entryPrice": contract["premium"],
                "lotSize": contract.get("lotSize") or instrument["lotSize"],
            }, rates)
        instruments.append({**instrument, "contract": contract, "projection": projection})

    live = sum(1 for i in desk["instruments"] if i["mode"] == "live")
    one_min = sum(1 for i in desk["instruments"] if i["timeframe"] == "1m")

    return {
        "instruments": instruments,
        "rows": book["rows"],
        "totals": totals,
        "breakdown": charges.charge_breakdown(totals),
        "rates": rates,
        "engine": {
            "live": live,
            "paper": len(desk["instruments"]) - live,
            "oneMin": one_min,
            "fiveMin": len(desk["instruments"]) - one_min,
        },
    }


def json_payload(view: Mapping[str, Any]) -> Dict[str, Any]:
    """Formatted strings keyed by element id — the browser only assigns text."""
    totals = view["totals"]
    fmt = formatting

    fields: Dict[str, Dict[str, str]] = {
        "kpi-gross": {"text": fmt.signed(totals["grossPnl"]), "cls": fmt.sign_class(totals["grossPnl"])},
        "kpi-gross-sub": {"text": f'{totals["trades"]} trades · {totals["wins"]} up / '
                                  f'{totals["losses"]} down · {fmt.pct(totals["winRatePct"])} win'},
        "kpi-charges": {"text": fmt.money(totals["totalCharges"])},
        "kpi-charges-sub": {"text": (f'{fmt.pct(totals["chargeRatioPct"])} of gross · turnover '
                                     f'{fmt.money(totals["turnover"])}') if totals["grossPnl"] > 0
                            else f'Turnover {fmt.money(totals["turnover"])}'},
        "kpi-net": {"text": fmt.signed(totals["netPnl"]), "cls": fmt.sign_class(totals["netPnl"])},
        "kpi-net-sub": {"text": (f'Gross {fmt.signed(totals["grossPnl"])} − charges '
                                 f'{fmt.money(totals["totalCharges"])}') if totals["trades"]
                        else "No trades yet"},
        "kpi-be": {"text": fmt.points(totals["avgBreakEvenPoints"])},
        "t-turnover": {"text": fmt.money(totals["turnover"])},
        "t-gross": {"text": fmt.signed(totals["grossPnl"]), "cls": fmt.sign_class(totals["grossPnl"])},
        "t-charges": {"text": fmt.MINUS + fmt.money(totals["totalCharges"])},
        "t-net": {"text": fmt.signed(totals["netPnl"]), "cls": fmt.sign_class(totals["netPnl"])},
        "ct-total": {"text": fmt.money(totals["totalCharges"])},
        "ct-gross-pct": {"text": fmt.pct(totals["chargeRatioPct"]) if totals["grossPnl"] > 0 else "—"},
        "mode-summary": {"text": f'{view["engine"]["live"]} live · {view["engine"]["paper"]} paper'},
        "engine-summary": {"text": f'MACD 12/26/9 · {view["engine"]["oneMin"]}× 1-min, '
                                   f'{view["engine"]["fiveMin"]}× 5-min'},
    }

    for index, row in enumerate(view["rows"]):
        fields[f"row-{index}-qty"] = {"text": fmt.qty(row["qty"])}
        fields[f"row-{index}-turnover"] = {"text": fmt.money(row["turnover"])}
        fields[f"row-{index}-gross"] = {"text": fmt.signed(row["grossPnl"]),
                                        "cls": fmt.sign_class(row["grossPnl"])}
        fields[f"row-{index}-charges"] = {"text": fmt.MINUS + fmt.money(row["totalCharges"])}
        fields[f"row-{index}-net"] = {"text": fmt.signed(row["netPnl"]),
                                      "cls": fmt.sign_class(row["netPnl"])}

    for index, instrument in enumerate(view["instruments"]):
        projection = instrument["projection"]
        if projection is None:
            for suffix in ("gross", "net", "charges", "be"):
                fields[f"inst-{index}-{suffix}"] = {"text": "—", "cls": ""}
        else:
            fields[f"inst-{index}-gross"] = {"text": fmt.money(projection["grossPnl"])}
            fields[f"inst-{index}-net"] = {"text": fmt.signed(projection["netPnl"]),
                                           "cls": fmt.sign_class(projection["netPnl"])}
            fields[f"inst-{index}-charges"] = {"text": fmt.money(projection["totalCharges"])}
            fields[f"inst-{index}-be"] = {"text": fmt.points(projection["breakEvenPoints"])}
        fields[f"inst-{index}-side"] = {"text": f'{instrument["side"]} · {instrument["timeframe"]}'}

    for head in view["breakdown"]:
        fields[f"bar-{head['key']}-amount"] = {"text": fmt.money(head["amount"])}
        fields[f"head-{head['key']}-amount"] = {"text": fmt.money(head["amount"])}
        fields[f"head-{head['key']}-share"] = {"text": fmt.pct(head["sharePct"]) if head["sharePct"] else "—"}
        fields[f"head-{head['key']}-gross"] = {"text": fmt.pct(head["grossPct"]) if head["grossPct"] is not None else "—"}

    caption = (f'Charges of <b>{fmt.money(totals["totalCharges"])}</b> on '
               f'<b>{fmt.money(totals["turnover"])}</b> of premium turnover — ')
    caption += (f'<b>{fmt.pct(totals["chargeRatioPct"])}</b> of gross profit.'
                if totals["grossPnl"] > 0 else "no gross profit to offset.")

    return {
        "fields": fields,
        "caption": caption,
        "bars": [{"key": h["key"], "widthPct": h["barPct"]} for h in view["breakdown"]],
        "netPnl": totals["netPnl"],
    }


def blotter_csv(view: Mapping[str, Any]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Symbol", "Side", "Exit reason", "Entry", "Exit", "Lots", "LotSize",
                     "Qty", "Turnover", "Gross", "Charges", "Net"])
    for row in view["rows"]:
        writer.writerow([row["symbol"], row["side"], row["reason"], row["entryPrice"],
                         row["exitPrice"], row["lots"], row["lotSize"], row["qty"],
                         row["turnover"], row["grossPnl"], row["totalCharges"], row["netPnl"]])
    totals = view["totals"]
    writer.writerow(["TOTAL", "", "", "", "", "", "", totals["qty"], totals["turnover"],
                     totals["grossPnl"], totals["totalCharges"], totals["netPnl"]])
    return buffer.getvalue()


# --------------------------------------------------------------- application

class DeskStateIO:
    """The engine reads and writes desk state through the same file the page uses."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> Dict[str, Any]:
        return state_module.load(self.path)

    def save(self, desk: Mapping[str, Any]) -> None:
        state_module.save(self.path, desk)


def create_app(state_path: Path = state_module.DEFAULT_STATE_PATH,
               settings: Optional[Settings] = None) -> Flask:
    app = Flask(__name__)
    app.config["STATE_PATH"] = Path(state_path)
    app.config["SETTINGS"] = settings or load_settings()
    # Signs only the short-lived OAuth state parameter, nothing sensitive.
    app.secret_key = os.environ.get("MACD_DESK_SECRET_KEY") or secrets.token_hex(16)

    broker = UpstoxClient(app.config["SETTINGS"].upstox)
    selector = ContractSelector(broker)
    engine = EngineRunner(settings=app.config["SETTINGS"].upstox, client=broker,
                          selector=selector, state_io=DeskStateIO(app.config["STATE_PATH"]))
    app.config["BROKER"] = broker
    app.config["ENGINE"] = engine

    def read_state() -> Dict[str, Any]:
        return state_module.load(app.config["STATE_PATH"])

    @app.get("/")
    def index():
        desk = read_state()
        connected = broker.current_token() is not None
        return render_template("index.html",
                               view=build_view(desk, engine.snapshots), fmt=formatting,
                               reasons=state_module.EXIT_REASONS, sides=state_module.SIDES,
                               connected=connected, engine=engine.status(),
                               problem=request.args.get("problem", ""),
                               notice=request.args.get("notice", ""))

    @app.post("/")
    def update():
        desk = state_from_form(request.form, read_state())
        # A row's delete button carries its own index rather than an action name.
        action = request.form.get("action") or (
            "delete-trade" if "delete-index" in request.form else "save")
        desk = apply_action(action, desk, request.form)
        state_module.save(app.config["STATE_PATH"], desk)
        return redirect(url_for("index"))

    @app.post("/api/book")
    def api_book():
        """Recompute from a submitted form without touching stored state."""
        desk = state_from_form(request.form, read_state())
        return jsonify(json_payload(build_view(desk, engine.snapshots)))

    @app.post("/market/refresh")
    def market_refresh():
        """Pull live chains now, so the cards show real premiums before the open."""
        if not broker.current_token():
            return redirect(url_for("index",
                                    problem="Connect to Upstox to pull live quotes."))
        problems = engine.refresh_all_snapshots()
        if problems:
            first = next(iter(problems.items()))
            return redirect(url_for("index", problem=f"{first[0]}: {first[1]}"))
        return redirect(url_for("index", notice="Live quotes refreshed from the option chain."))

    @app.post("/api/state")
    def api_save_state():
        """Persist the submitted form — what the JS layer calls on a change."""
        desk = state_from_form(request.form, read_state())
        state_module.save(app.config["STATE_PATH"], desk)
        return jsonify({"saved": True})

    @app.get("/api/state")
    def api_state():
        return jsonify(read_state())

    @app.get("/export.csv")
    def export_csv():
        view = build_view(read_state(), engine.snapshots)
        return Response(
            blotter_csv(view),
            mimetype="text/csv",
            headers={"Content-Disposition": 'attachment; filename="macd-desk-blotter.csv"'},
        )

    # --------------------------------------------------------------- macd

    def candle_history(symbol: str, timeframe: str) -> List[List[Any]]:
        """Warmup window plus today, de-duplicated — what the engine itself sees."""
        underlying = selector.underlying_key(symbol)
        if not underlying:
            raise UpstoxError(f"No instrument key for {symbol}.")
        merged: Dict[str, List[Any]] = {}
        for candle in broker.historical_candles(underlying, timeframe, days=3):
            merged[str(candle[0])] = list(candle)
        for candle in broker.intraday_candles(underlying, timeframe):
            merged[str(candle[0])] = list(candle)
        return [merged[key] for key in sorted(merged)]

    def instrument_config(symbol: str) -> Optional[Dict[str, Any]]:
        for config in read_state().get("instruments", []):
            if config["symbol"].upper() == symbol.upper():
                return config
        return None

    def macd_context(symbol: str, timeframe: Optional[str] = None):
        config = instrument_config(symbol)
        if config is None:
            raise UpstoxError(f"{symbol} is not on the desk.")
        timeframe = timeframe if timeframe in ("1m", "5m") else config["timeframe"]
        candles = candle_history(symbol, timeframe)
        rows = analysis.build_rows(candles)
        return config, timeframe, rows

    @app.get("/macd/<symbol>")
    def macd_page(symbol: str):
        """Every value the indicator produced, for checking against a chart."""
        if not broker.current_token():
            return redirect(url_for("index",
                                    problem="Connect to Upstox to read live candles."))
        try:
            config, timeframe, rows = macd_context(symbol, request.args.get("tf"))
        except UpstoxError as error:
            return redirect(url_for("index", problem=str(error)))

        return render_template(
            "macd.html", symbol=config["symbol"], kind=config.get("kind", ""),
            timeframe=timeframe, rows=rows[::-1][:400], summary=analysis.summarise(rows),
            chart=analysis.build_chart(rows), fmt=formatting,
            underlying=selector.underlying_key(config["symbol"]))

    @app.get("/macd/<symbol>.csv")
    def macd_csv(symbol: str):
        """The same rows as a file, for a side-by-side against the real market."""
        if not broker.current_token():
            return redirect(url_for("index",
                                    problem="Connect to Upstox to read live candles."))
        try:
            config, timeframe, rows = macd_context(symbol, request.args.get("tf"))
        except UpstoxError as error:
            return redirect(url_for("index", problem=str(error)))

        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(analysis.CSV_COLUMNS)
        for row in rows:
            writer.writerow(["" if row[column] is None else row[column]
                             for column in analysis.CSV_COLUMNS])
        stamp = rows[-1]["at"][:10] if rows else "empty"
        filename = f"{config['symbol']}-{timeframe}-macd-{stamp}.csv"
        return Response(buffer.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    # ------------------------------------------------------------- broker

    @app.get("/broker")
    def broker_page():
        upstox = app.config["SETTINGS"].upstox
        token = broker.current_token()
        return render_template(
            "broker.html",
            config=upstox.public(),
            callback_paths=app.config.get("CALLBACK_PATHS", ["/broker/callback"]),
            callback_conflict=app.config.get("CALLBACK_CONFLICT", ""),
            token=token.public() if token else None,
            engine=engine.status(),
            env_file=str(app.config["SETTINGS"].env_file_loaded or ""),
            notice=request.args.get("notice", ""),
            problem=request.args.get("problem", ""),
        )

    @app.get("/broker/connect")
    def broker_connect():
        """Send the operator to Upstox; they come back to /broker/callback."""
        state_token = secrets.token_urlsafe(16)
        session["oauth_state"] = state_token
        try:
            return redirect(broker.login_url(state=state_token))
        except UpstoxError as error:
            return redirect(url_for("broker_page", problem=str(error)))

    def handle_callback():
        """Complete the OAuth round trip, whatever path Upstox came back to."""
        if request.args.get("error"):
            return redirect(url_for("broker_page", problem=request.args.get(
                "error_description") or request.args["error"]))

        expected = session.pop("oauth_state", None)
        received = request.args.get("state")
        if expected and received and received != expected:
            return redirect(url_for("broker_page",
                                    problem="Login state did not match — start again."))

        code = request.args.get("code", "")
        if not code:
            return redirect(url_for("broker_page", problem="Upstox returned no code."))
        try:
            token = broker.exchange_code(code)
        except UpstoxError as error:
            return redirect(url_for("broker_page", problem=str(error)))
        return redirect(url_for("broker_page",
                                notice=f"Connected as {token.user_name or token.user_id}."))

    app.add_url_rule("/broker/callback", "broker_callback", handle_callback, methods=["GET"])

    @app.post("/broker/disconnect")
    def broker_disconnect():
        broker.disconnect()
        return redirect(url_for("broker_page", notice="Disconnected — the cached token was deleted."))

    @app.post("/broker/test")
    def broker_test():
        """Prove the credentials reach a real account before trusting the engine."""
        try:
            profile = broker.profile()
            funds = broker.funds()
        except UpstoxError as error:
            return redirect(url_for("broker_page", problem=str(error)))
        equity = (funds.get("equity") or {}) if isinstance(funds, dict) else {}
        available = equity.get("available_margin")
        detail = f" · available margin {formatting.money(available)}" if available is not None else ""
        return redirect(url_for("broker_page", notice=(
            f"Live data reached: {profile.get('user_name', 'account')} "
            f"({profile.get('user_id', '')}){detail}.")))

    # ------------------------------------------------------------- engine

    @app.post("/engine/start")
    def engine_start():
        if not broker.current_token():
            return redirect(url_for("broker_page",
                                    problem="Connect to Upstox before starting the engine."))
        engine.start()
        return redirect(url_for("broker_page", notice="Engine started."))

    @app.post("/engine/stop")
    def engine_stop():
        engine.stop()
        return redirect(url_for("broker_page", notice="Engine stopped."))

    @app.get("/api/engine")
    def api_engine():
        return jsonify(engine.status())

    @app.get("/healthz")
    def healthz():
        return jsonify({"status": "ok"})

    # Upstox redirects to the URI registered on the app, which need not be the
    # canonical /broker/callback. Serve that path too rather than 404 on it.
    app.config["CALLBACK_PATHS"] = register_callback_alias(app, handle_callback)

    return app


def callback_path(redirect_uri: str) -> str:
    """The path Upstox will come back to, from the configured redirect URI."""
    try:
        path = urlparse(redirect_uri).path or ""
    except ValueError:
        return ""
    return path if path.startswith("/") else ""


def register_callback_alias(app: Flask, view) -> List[str]:
    """Also serve the callback at the configured path, if it differs and is free."""
    paths = ["/broker/callback"]
    configured = callback_path(app.config["SETTINGS"].upstox.redirect_uri)
    if not configured or configured in paths:
        return paths
    if any(rule.rule == configured for rule in app.url_map.iter_rules()):
        # Something already answers there; leave it alone and say so on the page.
        app.config["CALLBACK_CONFLICT"] = configured
        return paths
    app.add_url_rule(configured, "broker_callback_configured", view, methods=["GET"])
    paths.append(configured)
    return paths

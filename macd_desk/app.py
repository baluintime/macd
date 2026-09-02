"""Flask application for the Upstox MACD options desk.

The page is rendered server-side and every figure on it is computed in Python.
The form works with JavaScript disabled — submitting it recomputes and
re-renders. With JavaScript on, the same form is posted to /api/book, which
returns the same numbers as JSON so the page updates without a reload.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any, Dict, List, Mapping

from flask import Flask, Response, jsonify, redirect, render_template, request, url_for

from . import charges, formatting, state as state_module

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
    elif action == "load-sample":
        desk["trades"] = state_module.sample_trades()
    elif action == "reset-rates":
        desk["rates"] = dict(charges.DEFAULT_RATES)
    return desk


# --------------------------------------------------------------- view model

def build_view(desk: Mapping[str, Any]) -> Dict[str, Any]:
    """Everything the template and the JSON API render, computed once."""
    rates = desk["rates"]
    book = charges.summarize(desk["trades"], rates)
    totals = book["totals"]

    instruments = []
    for instrument in desk["instruments"]:
        projection = charges.project_at_target(instrument, rates)
        instruments.append({**instrument, "projection": projection})

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

def create_app(state_path: Path = state_module.DEFAULT_STATE_PATH) -> Flask:
    app = Flask(__name__)
    app.config["STATE_PATH"] = Path(state_path)

    def read_state() -> Dict[str, Any]:
        return state_module.load(app.config["STATE_PATH"])

    @app.get("/")
    def index():
        desk = read_state()
        return render_template("index.html", view=build_view(desk), fmt=formatting,
                               reasons=state_module.EXIT_REASONS, sides=state_module.SIDES)

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
        return jsonify(json_payload(build_view(desk)))

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
        view = build_view(read_state())
        return Response(
            blotter_csv(view),
            mimetype="text/csv",
            headers={"Content-Disposition": 'attachment; filename="macd-desk-blotter.csv"'},
        )

    @app.get("/healthz")
    def healthz():
        return jsonify({"status": "ok"})

    return app

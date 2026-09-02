/*
 * charges.js — Upstox (NSE F&O) cost model and net-profit calculator.
 *
 * Pure functions, no DOM. Loaded as a classic script in the browser
 * (window.MacdCharges) and required directly by the Node tests.
 *
 * All rates live in one place so the desk can re-tune them when the exchange
 * or the broker revises a slab; nothing below hard-codes a number.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.MacdCharges = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  /**
   * Indicative Upstox charges for NSE index/stock OPTIONS, in percent of the
   * turnover they apply to (premium turnover, not notional). Verify against the
   * live Upstox tariff sheet before trusting these for accounting.
   */
  var DEFAULT_RATES = {
    brokeragePerOrder: 20,        // flat Rs per executed order
    brokeragePctCap: 2.5,         // ...or % of premium turnover, whichever is lower
    sttSellPct: 0.1,              // STT, sell side only, on premium
    exchangeTxnPct: 0.03503,      // NSE options exchange transaction charge
    ipftPct: 0.0005,              // NSE Investor Protection Fund Trust
    sebiPct: 0.0001,              // SEBI turnover fee (Rs 10 per crore)
    stampDutyBuyPct: 0.003,       // stamp duty, buy side only
    gstPct: 18                    // GST on brokerage + exchange + SEBI + IPFT
  };

  /** Exchange lot sizes are revised periodically — these are defaults, not law. */
  var DEFAULT_LOT_SIZES = {
    NIFTY: 75,
    BANKNIFTY: 35,
    FINNIFTY: 65,
    MIDCPNIFTY: 140,
    NIFTYNXT50: 25
  };

  function pct(value, rate) {
    return value * (rate / 100);
  }

  function round2(n) {
    return Math.round((n + Number.EPSILON) * 100) / 100;
  }

  function brokerageForOrder(turnover, rates) {
    // Upstox bills the lower of the flat per-order fee and the % cap.
    return Math.min(rates.brokeragePerOrder, pct(turnover, rates.brokeragePctCap));
  }

  /**
   * Cost a single round trip (one buy order + one sell order) on an option.
   *
   * @param {object} trade
   * @param {number} trade.entryPrice  premium paid per unit
   * @param {number} trade.exitPrice   premium received per unit
   * @param {number} trade.lots
   * @param {number} trade.lotSize
   * @param {object} [ratesOverride]   partial override of DEFAULT_RATES
   * @returns {object} gross, per-head charges, totalCharges, net, breakEvenPoints
   */
  function computeTrade(trade, ratesOverride) {
    var rates = Object.assign({}, DEFAULT_RATES, ratesOverride || {});

    var qty = (Number(trade.lots) || 0) * (Number(trade.lotSize) || 0);
    var entry = Number(trade.entryPrice) || 0;
    var exit = Number(trade.exitPrice) || 0;

    var buyTurnover = entry * qty;
    var sellTurnover = exit * qty;
    var turnover = buyTurnover + sellTurnover;

    var gross = sellTurnover - buyTurnover;

    var brokerage = brokerageForOrder(buyTurnover, rates) + brokerageForOrder(sellTurnover, rates);
    var stt = pct(sellTurnover, rates.sttSellPct);
    var exchangeTxn = pct(turnover, rates.exchangeTxnPct);
    var ipft = pct(turnover, rates.ipftPct);
    var sebi = pct(turnover, rates.sebiPct);
    // Stamp duty is levied on the buy leg and rounded to the nearest rupee.
    var stampDuty = Math.round(pct(buyTurnover, rates.stampDutyBuyPct));
    var gst = pct(brokerage + exchangeTxn + sebi + ipft, rates.gstPct);

    var charges = {
      brokerage: round2(brokerage),
      stt: round2(stt),
      exchangeTxn: round2(exchangeTxn),
      gst: round2(gst),
      sebi: round2(sebi),
      ipft: round2(ipft),
      stampDuty: round2(stampDuty)
    };

    var totalCharges = round2(
      charges.brokerage + charges.stt + charges.exchangeTxn +
      charges.gst + charges.sebi + charges.ipft + charges.stampDuty
    );

    return {
      qty: qty,
      buyTurnover: round2(buyTurnover),
      sellTurnover: round2(sellTurnover),
      turnover: round2(turnover),
      grossPnl: round2(gross),
      charges: charges,
      totalCharges: totalCharges,
      netPnl: round2(gross - totalCharges),
      // Points the option must move just to cover costs.
      breakEvenPoints: qty > 0 ? round2(totalCharges / qty) : 0
    };
  }

  /**
   * Net profit if a position is closed exactly at its configured target points.
   * This is the projection shown on each instrument card.
   */
  function projectAtTarget(config, ratesOverride) {
    return computeTrade({
      entryPrice: config.entryPrice,
      exitPrice: (Number(config.entryPrice) || 0) + (Number(config.targetPoints) || 0),
      lots: config.lots,
      lotSize: config.lotSize
    }, ratesOverride);
  }

  var CHARGE_HEADS = [
    { key: 'brokerage', label: 'Brokerage', note: 'Lower of flat fee or % cap, both legs' },
    { key: 'stt', label: 'STT', note: 'Sell-side premium only' },
    { key: 'exchangeTxn', label: 'Exchange txn', note: 'NSE, on premium turnover' },
    { key: 'gst', label: 'GST', note: 'On brokerage + exchange + SEBI + IPFT' },
    { key: 'stampDuty', label: 'Stamp duty', note: 'Buy side, rounded to the rupee' },
    { key: 'sebi', label: 'SEBI fee', note: 'Turnover fee' },
    { key: 'ipft', label: 'IPFT', note: 'Investor protection fund' }
  ];

  /** Roll a list of trades into one book-level P&L with per-head charge totals. */
  function summarize(trades, ratesOverride) {
    var totals = {
      trades: 0, wins: 0, losses: 0,
      qty: 0, turnover: 0, grossPnl: 0, totalCharges: 0, netPnl: 0,
      charges: {}
    };
    CHARGE_HEADS.forEach(function (h) { totals.charges[h.key] = 0; });

    var rows = (trades || []).map(function (t) {
      var r = computeTrade(t, ratesOverride);
      totals.trades += 1;
      if (r.grossPnl >= 0) totals.wins += 1; else totals.losses += 1;
      totals.qty += r.qty;
      totals.turnover += r.turnover;
      totals.grossPnl += r.grossPnl;
      totals.totalCharges += r.totalCharges;
      totals.netPnl += r.netPnl;
      CHARGE_HEADS.forEach(function (h) { totals.charges[h.key] += r.charges[h.key]; });
      return Object.assign({}, t, r);
    });

    ['turnover', 'grossPnl', 'totalCharges', 'netPnl'].forEach(function (k) {
      totals[k] = round2(totals[k]);
    });
    CHARGE_HEADS.forEach(function (h) {
      totals.charges[h.key] = round2(totals.charges[h.key]);
    });
    totals.chargeRatioPct = totals.grossPnl > 0
      ? round2((totals.totalCharges / totals.grossPnl) * 100)
      : 0;
    totals.winRatePct = totals.trades ? round2((totals.wins / totals.trades) * 100) : 0;
    totals.avgBreakEvenPoints = totals.qty > 0
      ? round2(totals.totalCharges / totals.qty)
      : 0;

    return { rows: rows, totals: totals };
  }

  return {
    DEFAULT_RATES: DEFAULT_RATES,
    DEFAULT_LOT_SIZES: DEFAULT_LOT_SIZES,
    CHARGE_HEADS: CHARGE_HEADS,
    computeTrade: computeTrade,
    projectAtTarget: projectAtTarget,
    summarize: summarize,
    round2: round2
  };
});

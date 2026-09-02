const test = require('node:test');
const assert = require('node:assert/strict');
const C = require('../assets/charges.js');

const near = (actual, expected, tol = 0.011) =>
  assert.ok(Math.abs(actual - expected) <= tol, `${actual} !~= ${expected}`);

test('gross P&L is the premium move times quantity', () => {
  const r = C.computeTrade({ entryPrice: 128.2, exitPrice: 148.2, lots: 1, lotSize: 75 });
  assert.equal(r.qty, 75);
  near(r.grossPnl, 20 * 75);
});

test('each charge head follows its own base', () => {
  const r = C.computeTrade({ entryPrice: 100, exitPrice: 130, lots: 2, lotSize: 75 });
  const buy = 100 * 150, sell = 130 * 150, turnover = buy + sell;

  near(r.charges.brokerage, 40);                        // flat 20 x 2 legs
  near(r.charges.stt, sell * 0.001);                    // sell side only
  near(r.charges.exchangeTxn, turnover * 0.0003503);
  near(r.charges.ipft, turnover * 0.000005);
  near(r.charges.sebi, turnover * 0.000001);
  assert.equal(r.charges.stampDuty, Math.round(buy * 0.00003));
  near(r.charges.gst,
    (r.charges.brokerage + r.charges.exchangeTxn + r.charges.sebi + r.charges.ipft) * 0.18);
});

test('net profit is gross less every charge head', () => {
  const r = C.computeTrade({ entryPrice: 100, exitPrice: 130, lots: 2, lotSize: 75 });
  const sum = Object.values(r.charges).reduce((a, b) => a + b, 0);
  near(r.totalCharges, sum);
  near(r.netPnl, r.grossPnl - r.totalCharges);
  assert.ok(r.netPnl < r.grossPnl, 'charges must reduce the take');
});

test('brokerage falls back to the percentage cap on tiny orders', () => {
  // 2.5% of a Rs 100 leg is Rs 2.50, below the Rs 20 flat fee.
  const r = C.computeTrade({ entryPrice: 1, exitPrice: 1, lots: 1, lotSize: 100 });
  near(r.charges.brokerage, 2.5 + 2.5);
});

test('break-even is the per-unit cost of the round trip', () => {
  const r = C.computeTrade({ entryPrice: 100, exitPrice: 100, lots: 1, lotSize: 75 });
  near(r.breakEvenPoints, r.totalCharges / 75, 0.02);
  near(r.netPnl, -r.totalCharges);
});

test('a losing trade carries charges on top of the loss', () => {
  const r = C.computeTrade({ entryPrice: 120, exitPrice: 100, lots: 1, lotSize: 75 });
  assert.ok(r.grossPnl < 0);
  assert.ok(r.netPnl < r.grossPnl);
});

test('rate overrides are honoured and defaults left untouched', () => {
  const base = C.computeTrade({ entryPrice: 100, exitPrice: 110, lots: 1, lotSize: 75 });
  const free = C.computeTrade({ entryPrice: 100, exitPrice: 110, lots: 1, lotSize: 75 },
    { brokeragePerOrder: 0, brokeragePctCap: 0 });
  assert.equal(free.charges.brokerage, 0);
  assert.ok(free.netPnl > base.netPnl);
  assert.equal(C.DEFAULT_RATES.brokeragePerOrder, 20);
});

test('projectAtTarget exits exactly target points above entry', () => {
  const p = C.projectAtTarget({ entryPrice: 142.5, targetPoints: 20, lots: 1, lotSize: 75 });
  near(p.grossPnl, 20 * 75);
  near(p.netPnl, p.grossPnl - p.totalCharges);
});

test('summarize rolls trades into one book', () => {
  const trades = [
    { symbol: 'NIFTY', entryPrice: 100, exitPrice: 120, lots: 1, lotSize: 75 },
    { symbol: 'NIFTY', entryPrice: 100, exitPrice: 90, lots: 1, lotSize: 75 }
  ];
  const { rows, totals } = C.summarize(trades);
  assert.equal(rows.length, 2);
  assert.equal(totals.trades, 2);
  assert.equal(totals.wins, 1);
  assert.equal(totals.losses, 1);
  near(totals.grossPnl, rows[0].grossPnl + rows[1].grossPnl);
  near(totals.totalCharges, rows[0].totalCharges + rows[1].totalCharges);
  near(totals.netPnl, totals.grossPnl - totals.totalCharges);
  near(totals.charges.stt, rows[0].charges.stt + rows[1].charges.stt);
});

test('an empty book is all zeroes, not NaN', () => {
  const { totals } = C.summarize([]);
  assert.equal(totals.netPnl, 0);
  assert.equal(totals.chargeRatioPct, 0);
  assert.equal(totals.avgBreakEvenPoints, 0);
});

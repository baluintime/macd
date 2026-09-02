const test = require('node:test');
const assert = require('node:assert/strict');
const C = require('./charges.js');

const near = (actual, expected, tol = 0.011) =>
  assert.ok(Math.abs(actual - expected) <= tol, `${actual} !~= ${expected}`);

test('gross P&L is the premium move times quantity', () => {
  const r = C.computeTrade({ entryPrice: 128.2, exitPrice: 148.2, lots: 1, lotSize: 75 });
  assert.equal(r.qty, 75);
  near(r.grossPnl, 20 * 75);
});

test('net profit is gross less every charge head', () => {
  const r = C.computeTrade({ entryPrice: 100, exitPrice: 130, lots: 2, lotSize: 75 });
  const sum = Object.values(r.charges).reduce((a, b) => a + b, 0);
  near(r.totalCharges, sum);
  near(r.netPnl, r.grossPnl - r.totalCharges);
});

test('summarize rolls trades into one book', () => {
  const { rows, totals } = C.summarize([
    { entryPrice: 100, exitPrice: 120, lots: 1, lotSize: 75 },
    { entryPrice: 100, exitPrice: 90, lots: 1, lotSize: 75 }
  ]);
  assert.equal(rows.length, 2);
  assert.equal(totals.wins, 1);
  near(totals.netPnl, totals.grossPnl - totals.totalCharges);
});

/*
 * app.js — console wiring for the Upstox MACD options desk.
 *
 * State lives in one object; every mutation calls render(). The money maths is
 * entirely in charges.js — nothing here re-implements a rate.
 */
(function () {
  'use strict';

  var C = window.MacdCharges;
  var STORE_KEY = 'macd-desk-v1';

  var inr = new Intl.NumberFormat('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  var inr0 = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 0 });

  function money(n) { return '₹' + inr.format(n); }
  function signed(n) { return (n < 0 ? '−₹' : '₹') + inr.format(Math.abs(n)); }
  function signClass(n) { return n > 0 ? 'pos' : (n < 0 ? 'neg' : ''); }

  // ---------------------------------------------------------------- state

  function defaultState() {
    return {
      rates: Object.assign({}, C.DEFAULT_RATES),
      instruments: [
        inst('NIFTY',      'Index',    C.DEFAULT_LOT_SIZES.NIFTY,      1, 20, 'CE', 'live',  '5m', 142.50),
        inst('BANKNIFTY',  'Index',    C.DEFAULT_LOT_SIZES.BANKNIFTY,  1, 35, 'PE', 'live',  '5m', 268.00),
        inst('FINNIFTY',   'Index',    C.DEFAULT_LOT_SIZES.FINNIFTY,   1, 25, 'CE', 'paper', '1m',  96.75),
        inst('RELIANCE',   'Momentum', 500,                            1, 12, 'CE', 'paper', '5m',  38.40),
        inst('HDFCBANK',   'Momentum', 550,                            2, 10, 'PE', 'paper', '1m',  24.15),
        inst('TATAMOTORS', 'Momentum', 800,                            1,  8, 'CE', 'paper', '1m',  17.90)
      ],
      trades: sampleTrades()
    };
  }

  function inst(symbol, kind, lotSize, lots, targetPoints, side, mode, timeframe, entryPrice) {
    return {
      symbol: symbol, kind: kind, lotSize: lotSize, lots: lots,
      targetPoints: targetPoints, side: side, mode: mode,
      timeframe: timeframe, entryPrice: entryPrice
    };
  }

  function sampleTrades() {
    return [
      trade('NIFTY',     'CE', 'Target',     128.20, 148.20, 1, 75,  '5m'),
      trade('NIFTY',     'PE', 'Reversal',  112.60,  98.35, 1, 75,  '5m'),
      trade('BANKNIFTY', 'PE', 'Target',     245.00, 280.00, 1, 35,  '5m'),
      trade('BANKNIFTY', 'CE', 'Reversal',  262.40, 251.10, 1, 35,  '5m'),
      trade('FINNIFTY',  'CE', 'Target',      88.10, 113.10, 1, 65,  '1m'),
      trade('RELIANCE',  'CE', 'Reversal',   41.25,  37.80, 1, 500, '5m'),
      trade('HDFCBANK',  'PE', 'Target',      22.40,  32.40, 2, 550, '1m')
    ];
  }

  function trade(symbol, side, reason, entryPrice, exitPrice, lots, lotSize, timeframe) {
    return {
      symbol: symbol, side: side, reason: reason,
      entryPrice: entryPrice, exitPrice: exitPrice,
      lots: lots, lotSize: lotSize, timeframe: timeframe
    };
  }

  var state = load() || defaultState();

  function save() {
    try { localStorage.setItem(STORE_KEY, JSON.stringify(state)); } catch (e) { /* private mode */ }
  }

  function load() {
    try {
      var raw = localStorage.getItem(STORE_KEY);
      if (!raw) return null;
      var parsed = JSON.parse(raw);
      if (!parsed || !parsed.instruments || !parsed.trades) return null;
      parsed.rates = Object.assign({}, C.DEFAULT_RATES, parsed.rates);
      return parsed;
    } catch (e) { return null; }
  }

  // ---------------------------------------------------------------- helpers

  function el(tag, className, text) {
    var n = document.createElement(tag);
    if (className) n.className = className;
    if (text !== undefined) n.textContent = text;
    return n;
  }

  function numberField(labelText, value, step, onChange) {
    var f = el('div', 'field');
    var id = 'f' + Math.random().toString(36).slice(2, 9);
    var l = el('label', null, labelText);
    l.setAttribute('for', id);
    var i = document.createElement('input');
    i.type = 'number';
    i.id = id;
    i.step = step;
    i.min = '0';
    i.value = value;
    i.addEventListener('input', function () { onChange(Number(i.value)); });
    f.appendChild(l);
    f.appendChild(i);
    return f;
  }

  var segSeq = 0;

  function segmented(options, current, onPick, extraClass) {
    // Radio inputs, matching the markup the Flask template renders, so one
    // stylesheet dresses both the app and this preview.
    var seg = el('div', 'seg' + (extraClass ? ' ' + extraClass : ''));
    var name = 'seg' + (segSeq += 1);
    options.forEach(function (opt) {
      var id = name + '-' + opt.value;
      var input = document.createElement('input');
      input.type = 'radio';
      input.name = name;
      input.id = id;
      input.value = opt.value;
      input.checked = opt.value === current;
      input.addEventListener('change', function () { onPick(opt.value); });
      var label = el('label', null, opt.label);
      label.setAttribute('for', id);
      seg.appendChild(input);
      seg.appendChild(label);
    });
    return seg;
  }

  // ---------------------------------------------------------------- instrument desk

  function renderDesk() {
    var desk = document.getElementById('desk');
    desk.textContent = '';

    state.instruments.forEach(function (ins, idx) {
      var card = el('div', 'card inst');

      var head = el('div', 'inst-head');
      head.appendChild(el('span', 'sym', ins.symbol));
      head.appendChild(el('span', 'tag', ins.kind));
      head.appendChild(el('span', 'side', ins.side + ' · ' + ins.timeframe));
      card.appendChild(head);

      // Execution mode + engine timeframe (SRS §5 controls)
      var controls = el('div', 'controls');
      controls.appendChild(segmented(
        [{ label: 'Live', value: 'live' }, { label: 'Paper', value: 'paper' }],
        ins.mode,
        function (v) { state.instruments[idx].mode = v; commit(); }
      ));
      controls.appendChild(segmented(
        [{ label: '1-min', value: '1m' }, { label: '5-min', value: '5m' }],
        ins.timeframe,
        function (v) { state.instruments[idx].timeframe = v; commit(); },
        'tf'
      ));
      controls.appendChild(segmented(
        [{ label: 'CE', value: 'CE' }, { label: 'PE', value: 'PE' }],
        ins.side,
        function (v) { state.instruments[idx].side = v; commit(); }
      ));
      card.appendChild(controls);

      var fields = el('div', 'fields');
      fields.appendChild(numberField('Position size (lots)', ins.lots, '1',
        function (v) { state.instruments[idx].lots = v; commit(); }));
      fields.appendChild(numberField('Target points', ins.targetPoints, '0.05',
        function (v) { state.instruments[idx].targetPoints = v; commit(); }));
      fields.appendChild(numberField('Lot size', ins.lotSize, '1',
        function (v) { state.instruments[idx].lotSize = v; commit(); }));
      fields.appendChild(numberField('Entry premium', ins.entryPrice, '0.05',
        function (v) { state.instruments[idx].entryPrice = v; commit(); }));
      card.appendChild(fields);

      // Net profit if this position exits exactly at its target.
      var p = C.projectAtTarget(ins, state.rates);
      var foot = el('div', 'inst-foot');

      var g = el('div');
      g.appendChild(el('div', 'k', 'Gross @ target'));
      g.appendChild(el('div', 'v', money(p.grossPnl)));
      foot.appendChild(g);

      var n = el('div');
      n.appendChild(el('div', 'k', 'Net @ target'));
      var nv = el('div', 'v ' + signClass(p.netPnl), signed(p.netPnl));
      n.appendChild(nv);
      foot.appendChild(n);

      var c = el('div');
      c.appendChild(el('div', 'k', 'Charges'));
      c.appendChild(el('div', 'v', money(p.totalCharges)));
      foot.appendChild(c);

      var b = el('div');
      b.appendChild(el('div', 'k', 'Break-even'));
      b.appendChild(el('div', 'v', inr.format(p.breakEvenPoints) + ' pts'));
      foot.appendChild(b);

      card.appendChild(foot);
      desk.appendChild(card);
    });

    var live = state.instruments.filter(function (i) { return i.mode === 'live'; }).length;
    var paper = state.instruments.length - live;
    document.getElementById('mode-summary').textContent = live + ' live · ' + paper + ' paper';
    document.getElementById('mode-dot').className = 'dot' + (live ? '' : ' paper');

    var oneMin = state.instruments.filter(function (i) { return i.timeframe === '1m'; }).length;
    document.getElementById('engine-summary').textContent =
      'MACD 12/26/9 · ' + oneMin + '× 1-min, ' + (state.instruments.length - oneMin) + '× 5-min';
  }

  // ---------------------------------------------------------------- blotter

  function cellInput(value, step, onChange) {
    var td = el('td', 'num');
    var i = document.createElement('input');
    i.type = 'number';
    i.step = step;
    i.value = value;
    i.addEventListener('input', function () { onChange(Number(i.value)); });
    td.appendChild(i);
    return td;
  }

  function cellSelect(value, options, onChange) {
    var td = el('td');
    var s = document.createElement('select');
    options.forEach(function (o) {
      var opt = document.createElement('option');
      opt.value = o; opt.textContent = o;
      if (o === value) opt.selected = true;
      s.appendChild(opt);
    });
    s.addEventListener('change', function () { onChange(s.value); });
    td.appendChild(s);
    return td;
  }

  function renderBlotter(book) {
    var body = document.getElementById('blotter-body');
    body.textContent = '';

    book.rows.forEach(function (r, idx) {
      var tr = document.createElement('tr');

      var sym = el('td');
      var si = document.createElement('input');
      si.type = 'text';
      si.value = r.symbol;
      si.className = 'sym-input';
      si.addEventListener('input', function () { state.trades[idx].symbol = si.value; commit(); });
      sym.appendChild(si);
      tr.appendChild(sym);

      tr.appendChild(cellSelect(r.side, ['CE', 'PE'], function (v) { state.trades[idx].side = v; commit(); }));
      tr.appendChild(cellSelect(r.reason, ['Target', 'Reversal', 'EOD close'],
        function (v) { state.trades[idx].reason = v; commit(); }));
      tr.appendChild(cellInput(r.entryPrice, '0.05', function (v) { state.trades[idx].entryPrice = v; commit(); }));
      tr.appendChild(cellInput(r.exitPrice, '0.05', function (v) { state.trades[idx].exitPrice = v; commit(); }));
      tr.appendChild(cellInput(r.lots, '1', function (v) { state.trades[idx].lots = v; commit(); }));
      tr.appendChild(cellInput(r.lotSize, '1', function (v) { state.trades[idx].lotSize = v; commit(); }));

      tr.appendChild(el('td', 'num', inr0.format(r.qty)));
      tr.appendChild(el('td', 'num', money(r.turnover)));
      tr.appendChild(el('td', 'num ' + signClass(r.grossPnl), signed(r.grossPnl)));
      tr.appendChild(el('td', 'num', '−' + money(r.totalCharges)));
      tr.appendChild(el('td', 'num net ' + signClass(r.netPnl), signed(r.netPnl)));

      var del = el('td');
      var db = el('button', 'row-del', '×');
      db.type = 'button';
      db.title = 'Remove trade';
      db.addEventListener('click', function () { state.trades.splice(idx, 1); commit(); });
      del.appendChild(db);
      tr.appendChild(del);

      body.appendChild(tr);
    });

    var t = book.totals;
    document.getElementById('t-turnover').textContent = money(t.turnover);
    setText('t-gross', signed(t.grossPnl), signClass(t.grossPnl));
    document.getElementById('t-charges').textContent = '−' + money(t.totalCharges);
    setText('t-net', signed(t.netPnl), signClass(t.netPnl));
  }

  function setText(id, text, cls) {
    var n = document.getElementById(id);
    var isNet = id === 't-net';
    n.textContent = text;
    n.className = 'num' + (isNet ? ' net' : '') + (cls ? ' ' + cls : '');
  }

  // ---------------------------------------------------------------- KPIs

  function renderKpis(t) {
    document.getElementById('kpi-gross').textContent = signed(t.grossPnl);
    document.getElementById('kpi-gross').className = 'value ' + signClass(t.grossPnl);
    document.getElementById('kpi-gross-sub').textContent =
      t.trades + ' trades · ' + t.wins + ' up / ' + t.losses + ' down · ' + inr.format(t.winRatePct) + '% win';

    document.getElementById('kpi-charges').textContent = money(t.totalCharges);
    document.getElementById('kpi-charges-sub').textContent =
      t.grossPnl > 0
        ? inr.format(t.chargeRatioPct) + '% of gross · turnover ' + money(t.turnover)
        : 'Turnover ' + money(t.turnover);

    document.getElementById('kpi-net').textContent = signed(t.netPnl);
    document.getElementById('kpi-net').className = 'value ' + signClass(t.netPnl);
    document.getElementById('kpi-net-sub').textContent =
      t.trades ? 'Gross ' + signed(t.grossPnl) + ' − charges ' + money(t.totalCharges) : 'No trades yet';

    document.getElementById('kpi-be').textContent = inr.format(t.avgBreakEvenPoints) + ' pts';
  }

  // ---------------------------------------------------------------- charge breakdown

  function renderCharges(t) {
    var heads = C.CHARGE_HEADS.map(function (h) {
      return { key: h.key, label: h.label, note: h.note, amount: t.charges[h.key] };
    }).sort(function (a, b) { return b.amount - a.amount; });

    var max = heads.reduce(function (m, h) { return Math.max(m, h.amount); }, 0) || 1;
    var total = t.totalCharges || 0;

    document.getElementById('chart-caption').innerHTML =
      'Charges of <b>' + money(total) + '</b> on <b>' + money(t.turnover) + '</b> of premium turnover — ' +
      (t.grossPnl > 0 ? '<b>' + inr.format(t.chargeRatioPct) + '%</b> of gross profit.' : 'no gross profit to offset.');

    var bars = document.getElementById('charge-bars');
    bars.textContent = '';
    heads.forEach(function (h) {
      var row = el('div', 'bar-row');
      row.title = h.label + ': ' + money(h.amount) + ' — ' + h.note;
      row.appendChild(el('div', 'name', h.label));
      var track = el('div', 'bar-track');
      var fill = el('div', 'bar-fill');
      fill.style.width = Math.max(0.6, (h.amount / max) * 100) + '%';
      track.appendChild(fill);
      row.appendChild(track);
      row.appendChild(el('div', 'amt', money(h.amount)));
      bars.appendChild(row);
    });

    var body = document.getElementById('charge-table-body');
    body.textContent = '';
    heads.forEach(function (h) {
      var tr = document.createElement('tr');
      tr.appendChild(el('td', null, h.label));
      tr.appendChild(el('td', null, h.note));
      tr.appendChild(el('td', 'num', money(h.amount)));
      tr.appendChild(el('td', 'num', total ? inr.format((h.amount / total) * 100) + '%' : '—'));
      tr.appendChild(el('td', 'num', t.grossPnl > 0 ? inr.format((h.amount / t.grossPnl) * 100) + '%' : '—'));
      body.appendChild(tr);
    });

    document.getElementById('ct-total').textContent = money(total);
    document.getElementById('ct-gross-pct').textContent =
      t.grossPnl > 0 ? inr.format(t.chargeRatioPct) + '%' : '—';
  }

  // ---------------------------------------------------------------- rate card

  var RATE_FIELDS = [
    ['brokeragePerOrder', 'Brokerage / order (₹)', '1'],
    ['brokeragePctCap', 'Brokerage cap (%)', '0.1'],
    ['sttSellPct', 'STT sell (%)', '0.001'],
    ['exchangeTxnPct', 'Exchange txn (%)', '0.00001'],
    ['gstPct', 'GST (%)', '0.5'],
    ['stampDutyBuyPct', 'Stamp duty buy (%)', '0.001'],
    ['sebiPct', 'SEBI (%)', '0.0001'],
    ['ipftPct', 'IPFT (%)', '0.0001']
  ];

  function renderRates() {
    var host = document.getElementById('rates');
    if (host.dataset.built) return;   // inputs are uncontrolled; build once
    host.dataset.built = '1';
    RATE_FIELDS.forEach(function (f) {
      host.appendChild(numberField(f[1], state.rates[f[0]], f[2], function (v) {
        state.rates[f[0]] = v;
        commit();
      }));
    });
  }

  // ---------------------------------------------------------------- render loop

  function render() {
    renderRates();
    renderDesk();
    var book = C.summarize(state.trades, state.rates);
    renderBlotter(book);
    renderKpis(book.totals);
    renderCharges(book.totals);
  }

  var pendingFocus = null;

  function commit() {
    // Keep the caret where the user is typing across the re-render.
    var a = document.activeElement;
    pendingFocus = a && a.tagName === 'INPUT' ? describeFocus(a) : null;
    save();
    render();
    restoreFocus();
  }

  function describeFocus(node) {
    var tds = Array.prototype.slice.call(document.querySelectorAll('#blotter-body input'));
    var i = tds.indexOf(node);
    if (i >= 0) return { scope: 'blotter', index: i, pos: node.selectionStart };
    var desk = Array.prototype.slice.call(document.querySelectorAll('#desk input'));
    var j = desk.indexOf(node);
    if (j >= 0) return { scope: 'desk', index: j, pos: node.selectionStart };
    return null;
  }

  function restoreFocus() {
    if (!pendingFocus) return;
    var sel = pendingFocus.scope === 'blotter' ? '#blotter-body input' : '#desk input';
    var nodes = document.querySelectorAll(sel);
    var node = nodes[pendingFocus.index];
    if (node) {
      node.focus();
      try { node.setSelectionRange(pendingFocus.pos, pendingFocus.pos); } catch (e) { /* number inputs */ }
    }
    pendingFocus = null;
  }

  // ---------------------------------------------------------------- toolbar

  document.getElementById('add-trade').addEventListener('click', function () {
    var last = state.trades[state.trades.length - 1];
    state.trades.push(trade(
      last ? last.symbol : 'NIFTY', 'CE', 'Target',
      last ? last.entryPrice : 100, last ? last.entryPrice : 100,
      1, last ? last.lotSize : C.DEFAULT_LOT_SIZES.NIFTY, '5m'
    ));
    commit();
  });

  document.getElementById('clear-trades').addEventListener('click', function () {
    state.trades = [];
    commit();
  });

  document.getElementById('load-sample').addEventListener('click', function () {
    state.trades = sampleTrades();
    commit();
  });

  document.getElementById('export-csv').addEventListener('click', function (e) {
    var book = C.summarize(state.trades, state.rates);
    var lines = [['Symbol', 'Side', 'Exit reason', 'Entry', 'Exit', 'Lots', 'LotSize', 'Qty',
      'Turnover', 'Gross', 'Charges', 'Net'].join(',')];
    book.rows.forEach(function (r) {
      lines.push([r.symbol, r.side, r.reason, r.entryPrice, r.exitPrice, r.lots, r.lotSize,
        r.qty, r.turnover, r.grossPnl, r.totalCharges, r.netPnl].join(','));
    });
    var t = book.totals;
    lines.push(['TOTAL', '', '', '', '', '', '', t.qty, t.turnover, t.grossPnl, t.totalCharges, t.netPnl].join(','));
    copyText(lines.join('\n'), e.target);
  });

  function copyText(text, button) {
    var done = function () {
      var original = button.textContent;
      button.textContent = 'Copied';
      setTimeout(function () { button.textContent = original; }, 1400);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, function () { fallbackCopy(text, done); });
    } else {
      fallbackCopy(text, done);
    }
  }

  function fallbackCopy(text, done) {
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); done(); } catch (e) { /* nothing else to try */ }
    document.body.removeChild(ta);
  }

  document.getElementById('theme-toggle').addEventListener('click', function () {
    var root = document.documentElement;
    var dark = root.getAttribute('data-theme') === 'dark' ||
      (!root.getAttribute('data-theme') && window.matchMedia('(prefers-color-scheme: dark)').matches);
    root.setAttribute('data-theme', dark ? 'light' : 'dark');
  });

  render();
})();

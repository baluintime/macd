/* Polls /api/engine so the broker page reflects the running loop without a reload. */
(function () {
  'use strict';

  var dot = document.getElementById('engine-dot');
  var text = document.getElementById('engine-text');
  var log = document.getElementById('engine-log');
  var body = document.getElementById('positions-body');
  if (!dot || !text) return;

  function cell(value, numeric) {
    var td = document.createElement('td');
    if (numeric) td.className = 'num';
    td.textContent = value;
    return td;
  }

  function renderPositions(rows) {
    body.textContent = '';
    if (!rows.length) {
      var empty = document.createElement('tr');
      var td = cell('No open positions.', false);
      td.className = 'empty';
      td.colSpan = 8;
      empty.appendChild(td);
      body.appendChild(empty);
      return;
    }
    rows.forEach(function (row) {
      var tr = document.createElement('tr');
      var p = row.position;
      [[row.symbol], [row.timeframe], [p.tradingSymbol],
       [Math.round(p.quantity), true], [p.entryPrice, true], [p.lastPrice, true],
       [p.targetPrice, true], [Math.round(p.unrealised * 100) / 100, true]]
        .forEach(function (spec) { tr.appendChild(cell(spec[0], spec[1])); });
      body.appendChild(tr);
    });
  }

  function renderLog(events) {
    log.textContent = '';
    if (!events.length) {
      var li = document.createElement('li');
      li.className = 'empty-log';
      li.textContent = 'Nothing yet — the log fills as the engine warms up and trades.';
      log.appendChild(li);
      return;
    }
    events.forEach(function (event) {
      var li = document.createElement('li');
      li.className = event.level;
      var at = document.createElement('span');
      at.className = 'at';
      at.textContent = event.at;
      var scope = document.createElement('span');
      scope.className = 'scope';
      scope.textContent = event.scope;
      li.appendChild(at);
      li.appendChild(scope);
      li.appendChild(document.createTextNode(event.message));
      log.appendChild(li);
    });
  }

  function refresh() {
    fetch('/api/engine')
      .then(function (response) { return response.ok ? response.json() : null; })
      .then(function (status) {
        if (!status) return;
        dot.className = 'dot' + (status.running ? '' : ' paper');
        text.textContent = status.running
          ? 'Running · cycle ' + status.cycles + ' at ' + status.lastCycleAt
          : 'Stopped';
        renderPositions(status.openPositions || []);
        renderLog(status.events || []);
      })
      .catch(function () { /* the desk is down; the next tick retries */ });
  }

  setInterval(refresh, 5000);
})();

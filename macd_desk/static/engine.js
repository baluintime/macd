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

      tr.appendChild(cell(row.symbol, false));
      tr.appendChild(cell(row.timeframe, false));

      var contract = cell(p.label, false);
      contract.className = 'mono';
      tr.appendChild(contract);

      // Call or put reads at a glance, not only from the sign of the delta.
      var type = document.createElement('td');
      var chip = document.createElement('span');
      chip.className = 'chip ' + (p.side === 'CE' ? 'call' : 'put');
      chip.textContent = p.side;
      type.appendChild(chip);
      type.appendChild(document.createTextNode(' ' + p.optionType));
      tr.appendChild(type);

      tr.appendChild(cell(Math.round(p.quantity), true));

      var premium = cell(p.entryPrice + ' → ' + p.lastPrice, true);
      premium.classList.add('mono');
      tr.appendChild(premium);

      // The target is on the underlying, so show where spot is against it.
      var spot = cell(p.lastSpot + ' → ' + p.targetSpot, true);
      spot.classList.add('mono');
      tr.appendChild(spot);

      var pnl = cell(Math.round(p.unrealised * 100) / 100, true);
      pnl.classList.add(p.unrealised > 0 ? 'pos' : p.unrealised < 0 ? 'neg' : 'flat');
      tr.appendChild(pnl);

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

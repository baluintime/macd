/*
 * desk.js — the only client-side code in the project.
 *
 * It computes nothing. On a change it posts the form to /api/book, and Python
 * sends back formatted strings keyed by element id, which this assigns to the
 * page. Without JavaScript the same form submits normally and the server
 * re-renders — the desk works either way.
 */
(function () {
  'use strict';

  var form = document.getElementById('desk-form');
  if (!form) return;

  var savedFlag = document.getElementById('save-state');
  var caption = document.getElementById('chart-caption');
  var recalcTimer = null;
  var saveTimer = null;
  var inFlight = null;

  function post(url, body) {
    return fetch(url, { method: 'POST', body: body, headers: { 'X-Requested-With': 'fetch' } });
  }

  function apply(payload) {
    Object.keys(payload.fields).forEach(function (id) {
      var node = document.getElementById(id);
      if (!node) return;
      var field = payload.fields[id];
      node.textContent = field.text;
      if (Object.prototype.hasOwnProperty.call(field, 'cls')) {
        node.classList.remove('pos', 'neg');
        if (field.cls) node.classList.add(field.cls);
      }
    });

    payload.bars.forEach(function (bar) {
      var node = document.getElementById('bar-' + bar.key);
      if (node) node.style.width = bar.widthPct + '%';
    });

    if (caption) caption.innerHTML = payload.caption;
  }

  function recalculate() {
    if (inFlight) inFlight.abort();
    var controller = new AbortController();
    inFlight = controller;
    fetch('/api/book', {
      method: 'POST',
      body: new FormData(form),
      signal: controller.signal
    })
      .then(function (response) { return response.ok ? response.json() : null; })
      .then(function (payload) { if (payload) apply(payload); })
      .catch(function () { /* aborted by a newer keystroke, or the server is down */ })
      .finally(function () { if (inFlight === controller) inFlight = null; });
  }

  function persist() {
    post('/api/state', new FormData(form)).then(function (response) {
      if (response.ok && savedFlag) {
        savedFlag.hidden = false;
        clearTimeout(savedFlag.timer);
        savedFlag.timer = setTimeout(function () { savedFlag.hidden = true; }, 1200);
      }
    }).catch(function () { /* offline; the next submit will save */ });
  }

  form.addEventListener('input', schedule);
  form.addEventListener('change', schedule);

  function schedule() {
    clearTimeout(recalcTimer);
    clearTimeout(saveTimer);
    recalcTimer = setTimeout(recalculate, 180);
    saveTimer = setTimeout(persist, 700);
  }

  // Adding, deleting and clearing trades change the row set, so those go
  // through a full server round trip rather than a partial update.

  var toggle = document.getElementById('theme-toggle');
  if (toggle) {
    toggle.addEventListener('click', function () {
      var root = document.documentElement;
      var dark = root.getAttribute('data-theme') === 'dark' ||
        (!root.getAttribute('data-theme') &&
          window.matchMedia('(prefers-color-scheme: dark)').matches);
      root.setAttribute('data-theme', dark ? 'light' : 'dark');
      try { localStorage.setItem('macd-desk-theme', dark ? 'light' : 'dark'); } catch (e) { /* private mode */ }
    });
  }

  try {
    var stored = localStorage.getItem('macd-desk-theme');
    if (stored) document.documentElement.setAttribute('data-theme', stored);
  } catch (e) { /* private mode */ }
})();

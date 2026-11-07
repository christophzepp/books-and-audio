'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

// Exercise the shipped API wrapper without a browser, DOM or extra packages.
const source = fs.readFileSync(path.join(__dirname, 'admin_static/app.js'), 'utf8');
const start = source.indexOf('async function api(');
const end = source.indexOf('\nfunction showAuth(', start);
assert.ok(start >= 0 && end > start, 'The shipped API wrapper must be present');
const apiSource = source.slice(start, end);

function harness(fetch) {
  const calls = [];
  let authShown = 0;
  const context = vm.createContext({
    fetch: async (...args) => { calls.push(args); return fetch(...args); },
    cookie: name => { assert.equal(name, 'sync_admin_csrf'); return 'fixture-csrf'; },
    state: {authenticated: true},
    showAuth: () => { authShown += 1; },
  });
  vm.runInContext(apiSource, context);
  return {api: context.api, context, calls, authShown: () => authShown};
}

test('a rejected login fetch reports a network error without leaking details or retrying POST', async () => {
  const h = harness(async () => { throw new TypeError('Load failed PASSWORD_MUST_NOT_APPEAR'); });
  const payload = {username: 'fixture-user', password: 'fixture-password'};
  await assert.rejects(h.api('/api/login', payload), error => {
    assert.equal(error.network, true);
    assert.match(error.message, /Books & Audio.*nicht erreichbar/);
    assert.match(error.message, /Serveradresse.*Netzwerkverbindung/);
    assert.doesNotMatch(error.message, /PASSWORD_MUST_NOT_APPEAR|Load failed/);
    return true;
  });
  assert.equal(h.calls.length, 1);
  const [url, options] = h.calls[0];
  assert.equal(url, '/api/login');
  assert.equal(options.method, 'POST');
  assert.equal(options.credentials, 'same-origin');
  assert.equal(options.headers['X-CSRF-Token'], 'fixture-csrf');
  assert.deepEqual(JSON.parse(options.body), payload);
  assert.equal(h.authShown(), 0);
});

test('session requests stay compatible with the read-only HTTP endpoint', async () => {
  const body = {initialized: true, authenticated: false};
  const h = harness(async () => ({ok: true, json: async () => body}));
  assert.equal(await h.api('/api/session'), body);
  const [, options] = h.calls[0];
  assert.equal(options.method, undefined);
  assert.equal(options.body, undefined);
  assert.equal(options.headers['X-CSRF-Token'], undefined);
  assert.equal(options.credentials, 'same-origin');
});

test('wrong credentials keep the server authentication message and HTTP status', async () => {
  const message = 'Benutzername oder Passwort ist falsch.';
  const h = harness(async () => ({ok: false, status: 401, json: async () => ({error: message})}));
  await assert.rejects(h.api('/api/login', {}), error => {
    assert.equal(error.message, message);
    assert.equal(error.status, 401);
    assert.equal(error.network, undefined);
    return true;
  });
  assert.equal(h.authShown(), 0);
  assert.equal(h.calls.length, 1);
});

test('expired private sessions still return to the login screen', async () => {
  const h = harness(async () => ({ok: false, status: 401, json: async () => ({error: 'Bitte melde dich an.'})}));
  await assert.rejects(h.api('/api/status'), error => error.status === 401);
  assert.equal(h.context.state.authenticated, false);
  assert.equal(h.authShown(), 1);
});

test('CSRF and service errors remain distinct from network failures', async () => {
  for (const status of [403, 429, 503]) {
    const message = 'Servermeldung ' + status;
    const h = harness(async () => ({ok: false, status, json: async () => ({error: message})}));
    await assert.rejects(h.api('/api/login', {}), error => {
      assert.equal(error.message, message);
      assert.equal(error.status, status);
      assert.equal(error.network, undefined);
      return true;
    });
    assert.equal(h.calls.length, 1);
    assert.equal(h.context.state.authenticated, true);
  }
});

test('an unreadable response keeps its JSON error without leaking raw response details', async () => {
  const h = harness(async () => ({ok: true, json: async () => { throw new SyntaxError('PRIVATE_RESPONSE'); }}));
  await assert.rejects(h.api('/api/session'), error => {
    assert.equal(error.message, 'Der Server hat keine lesbare Antwort gesendet.');
    assert.equal(error.network, undefined);
    assert.doesNotMatch(error.message, /PRIVATE_RESPONSE/);
    return true;
  });
});

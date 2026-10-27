// Runs the REAL app/static/js/call.js against fakes for the DOM, WebRTC, getUserMedia and fetch.
// Run with:  node --test tests/frontend/
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const SRC = fs.readFileSync(new URL('../../app/static/js/call.js', import.meta.url), 'utf8');
const MENU = '/menu?restaurant_id=resto-a';
const flush = async (n = 12) => { for (let i = 0; i < n; i++) await new Promise((r) => setImmediate(r)); };
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };
const ok = (body) => ({ ok: true, status: 200, json: async () => body });
const bad = (status, body = {}) => ({ ok: false, status, json: async () => body });

function harness({ supported = true, gum, route, restaurantId = 'resto-a' } = {}) {
  const ids = ['btn-call', 'btn-end', 'btn-cancel', 'status', 'status-detail', 'error', 'transcript', 'transcript-empty', 'remote-audio',
    'order-banner', 'order-text', 'order-link', 'support-warning', 'call-root'];
  const els = {};
  for (const id of ids) {
    const l = {};
    els[id] = { id, hidden: false, disabled: false, textContent: '', className: '', dataset: {}, children: [], scrollTop: 0, scrollHeight: 0,
      addEventListener(t, f) { (l[t] ||= []).push(f); }, click() { (l.click || []).forEach((f) => f({})); },
      appendChild(c) { this.children.push(c); return c; }, pause() { this.paused = true; }, srcObject: null, muted: false };
  }
  els['call-root'].dataset = { restaurantId, menuUrl: MENU };

  const log = { fetch: [], locations: [], pcs: [], streams: [], gum: 0 };
  class FakePC {
    constructor() { this.iceGatheringState = 'complete'; this.closed = false; this.tracks = []; log.pcs.push(this); }
    addTrack(t) { this.tracks.push(t); }
    createDataChannel(label) { this.dc = { label, readyState: 'open', sent: [], send(m) { this.sent.push(JSON.parse(m)); }, close() { this.readyState = 'closed'; }, onmessage: null, onclose: null }; return this.dc; }
    async createOffer() { return { type: 'offer', sdp: 'v=0\r\nfake-offer' }; }
    async setLocalDescription(d) { this.localDescription = d; }
    async setRemoteDescription(d) { this.remote = d; }
    close() { this.closed = true; }
    addEventListener() {} removeEventListener() {}
  }
  const newStream = () => {
    const tracks = [{ stopped: false, stop() { this.stopped = true; } }];
    const s = { getTracks: () => tracks, getAudioTracks: () => tracks, tracks };
    log.streams.push(s); return s;
  };
  const sandbox = {
    console, URL, JSON, Uint8Array, Array, Error, Promise, encodeURIComponent,
    setTimeout: (f, ms) => { const t = setTimeout(f, ms); t.unref?.(); return t; }, clearTimeout,
    AbortController, MediaStream: class { constructor(t) { this.t = t; } },
    crypto: { getRandomValues: (b) => { for (let i = 0; i < b.length; i++) b[i] = Math.floor(Math.random() * 256); return b; } },
    document: { referrer: '', getElementById: (id) => els[id] || null,
      createElement: () => ({ className: '', textContent: '', children: [], appendChild(c) { this.children.push(c); return c; } }) },
    navigator: { mediaDevices: supported ? { getUserMedia: (c) => { log.gum++; return gum ? gum() : Promise.resolve(newStream()); } } : undefined },
    location: { assign: (u) => log.locations.push(u) }, history: { length: 1, back() { log.locations.push('BACK'); } },
    fetch: (path, init = {}) => {
      const entry = { path, method: init.method || 'GET', headers: init.headers || {}, body: init.body ? JSON.parse(init.body) : null, signal: init.signal };
      log.fetch.push(entry);
      const out = (route && route(entry)) ?? defaultRoute(entry);
      if (!out || typeof out.then !== 'function') return Promise.resolve(out);
      return new Promise((resolve, reject) => {   // honour AbortController like a real fetch
        if (init.signal) init.signal.addEventListener?.('abort', () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' })));
        out.then(resolve, reject);
      });
    },
  };
  const defaultRoute = (e) => {
    if (e.path === '/api/call/session') return ok({ call_secret: 'sec', transport: { type: 'webrtc', sdp: 'v=0 answer' } });
    if (e.path.startsWith('/api/call/status')) return ok({ state: 'active', order: null });
    return ok({ ok: true });
  };
  sandbox.window = sandbox; sandbox.isSecureContext = true;
  if (supported) sandbox.RTCPeerConnection = FakePC;
  const listeners = {};
  sandbox.addEventListener = (t, f) => { (listeners[t] ||= []).push(f); };
  let hook; sandbox.__CALL_TEST_HOOK__ = (h) => { hook = h; };
  vm.createContext(sandbox);
  vm.runInContext(SRC, sandbox);
  const started = (pc) => pc.dc.onmessage({ data: JSON.stringify({ type: 'session.started' }) });
  return { els, log, state: () => hook.getState(), started, pc: () => log.pcs.at(-1), pagehide: () => (listeners.pagehide || []).forEach((f) => f()),
    paths: () => log.fetch.map((f) => f.path.split('?')[0]), sessionCalls: () => log.fetch.filter((f) => f.path === '/api/call/session') };
}

test('initial state: idle, nothing created, nothing requested', () => {
  const h = harness();
  assert.equal(h.state(), 'idle');
  assert.equal(h.log.gum, 0); assert.equal(h.log.fetch.length, 0); assert.equal(h.log.pcs.length, 0);
  assert.equal(h.els['btn-call'].hidden, false); assert.equal(h.els['btn-cancel'].hidden, false); assert.equal(h.els['btn-end'].hidden, true);
  assert.equal(h.els.status.textContent, 'Idle');
});

test('Call: asks for mic, posts the SDP offer to the backend, and only shows Connected after session.started', async () => {
  const h = harness();
  h.els['btn-call'].click(); await flush();
  assert.equal(h.log.gum, 1);
  const [call] = h.sessionCalls();
  assert.equal(call.method, 'POST'); assert.equal(call.body.sdp, 'v=0\r\nfake-offer');
  assert.match(call.body.client_call_id, /^[0-9a-f]{32}$/); assert.equal(call.body.restaurant_id, 'resto-a');
  assert.equal(h.pc().dc.label, 'oai-events');
  assert.equal(h.pc().remote.sdp, 'v=0 answer');
  // API success + SDP answer applied is NOT a connection:
  assert.equal(h.state(), 'connecting'); assert.equal(h.els.status.textContent, 'Connecting');
  assert.equal(h.els['btn-end'].hidden, true);
  h.started(h.pc()); await flush();
  assert.equal(h.state(), 'connected'); assert.equal(h.els.status.textContent, 'Connected');
  assert.equal(h.els['btn-end'].hidden, false); assert.equal(h.els['btn-end'].disabled, false);
  assert.equal(h.els['btn-call'].hidden, true); assert.equal(h.els['btn-cancel'].hidden, true);
  const ready = h.log.fetch.find((f) => f.path === '/api/call/ready');
  assert.equal(ready.headers['X-Call-Secret'], 'sec');
});

test('Cancel while the session request is in flight: no late Connected, resources released, server told', async () => {
  const gate = deferred();
  const h = harness({ route: (e) => (e.path === '/api/call/session' ? gate.promise : undefined) });
  h.els['btn-call'].click(); await flush();
  assert.equal(h.state(), 'connecting');
  const pc = h.pc(), stream = h.log.streams[0], sessionReq = h.sessionCalls()[0];
  h.els['btn-cancel'].click(); await flush();
  assert.equal(h.state(), 'idle');
  assert.equal(sessionReq.signal.aborted, true);                 // frontend request aborted
  assert.equal(pc.closed, true); assert.equal(stream.tracks[0].stopped, true);
  const cancel = h.log.fetch.find((f) => f.path === '/api/call/cancel');
  assert.ok(cancel && cancel.body.client_call_id === sessionReq.body.client_call_id);   // server-side cleanup requested
  assert.deepEqual(h.log.locations, [MENU]);                      // returned to the menu
  // the server answers late, and even the data channel fires session.started late:
  gate.resolve(ok({ call_secret: 'sec', transport: { sdp: 'v=0 answer' } })); await flush();
  pc.dc.onmessage?.({ data: JSON.stringify({ type: 'session.started' }) }); await flush();
  assert.equal(h.state(), 'idle');
  assert.equal(h.log.fetch.some((f) => f.path === '/api/call/ready'), false);
  assert.equal(pc.remote, undefined);                             // late answer was never applied
});

test('Cancel while the mic prompt is open: late stream is released and no session is requested', async () => {
  const mic = deferred();
  const h = harness({ gum: () => mic.promise });
  h.els['btn-call'].click(); await flush();
  h.els['btn-cancel'].click(); await flush();
  const late = (() => { const tracks = [{ stopped: false, stop() { this.stopped = true; } }]; return { getTracks: () => tracks, getAudioTracks: () => tracks, tracks }; })();
  mic.resolve(late); await flush();
  assert.equal(late.tracks[0].stopped, true);
  assert.equal(h.sessionCalls().length, 0); assert.equal(h.log.pcs.length, 0); assert.equal(h.state(), 'idle');
});

test('Cancel in idle just leaves without creating anything', async () => {
  const h = harness();
  h.els['btn-cancel'].click(); await flush();
  assert.deepEqual(h.log.locations, [MENU]); assert.equal(h.log.fetch.length, 0); assert.equal(h.log.gum, 0);
});

test('repeated clicks create exactly one session', async () => {
  const h = harness();
  const b = h.els['btn-call'];
  b.click(); b.click(); b.click(); await flush(); b.click();
  assert.equal(h.log.gum, 1); assert.equal(h.sessionCalls().length, 1); assert.equal(h.log.pcs.length, 1);
  assert.equal(b.disabled, true);
});

test('failed request: error shown, partial resources cleaned up, Try again works with a fresh id', async () => {
  let fail = true;
  const h = harness({ route: (e) => (e.path === '/api/call/session' && fail ? bad(503, { detail: 'All lines are busy.' }) : undefined) });
  h.els['btn-call'].click(); await flush();
  assert.equal(h.state(), 'error'); assert.equal(h.els.status.textContent, 'Error');
  assert.match(h.els.error.textContent, /busy/); assert.equal(h.els.error.hidden, false);
  assert.equal(h.pc().closed, true); assert.equal(h.log.streams[0].tracks[0].stopped, true);
  assert.equal(h.els['btn-call'].disabled, false); assert.equal(h.els['btn-call'].textContent, 'Try again');
  const firstId = h.sessionCalls()[0].body.client_call_id;
  fail = false;
  h.els['btn-call'].click(); await flush();
  assert.equal(h.sessionCalls().length, 2); assert.notEqual(h.sessionCalls()[1].body.client_call_id, firstId);
  assert.equal(h.state(), 'connecting'); assert.equal(h.els.error.hidden, true);
});

test('network failure and rate limit give useful messages', async () => {
  const net = harness({ route: (e) => (e.path === '/api/call/session' ? Promise.reject(new TypeError('Failed to fetch')) : undefined) });
  net.els['btn-call'].click(); await flush();
  assert.match(net.els.error.textContent, /Network error/); assert.equal(net.state(), 'error');
  const rl = harness({ route: (e) => (e.path === '/api/call/session' ? bad(429) : undefined) });
  rl.els['btn-call'].click(); await flush();
  assert.match(rl.els.error.textContent, /Too many attempts/);
});

test('microphone denied: clear error, no session request', async () => {
  const h = harness({ gum: () => Promise.reject(Object.assign(new Error('x'), { name: 'NotAllowedError' })) });
  h.els['btn-call'].click(); await flush();
  assert.equal(h.state(), 'error'); assert.match(h.els.error.textContent, /Microphone access was denied/);
  assert.equal(h.sessionCalls().length, 0);
});

test('unsupported browser: Call disabled with a fallback message', async () => {
  const h = harness({ supported: false });
  assert.equal(h.els['btn-call'].disabled, true); assert.equal(h.els['support-warning'].hidden, false);
  h.els['btn-call'].click(); await flush();
  assert.equal(h.log.fetch.length, 0);
});

test('no restaurant id: Call stays disabled and never asks for the mic, builds a peer connection or sends a request', async () => {
  for (const restaurantId of ['', '   ', null]) {
    const h = harness({ restaurantId });
    assert.equal(h.els['btn-call'].disabled, true, JSON.stringify(restaurantId));
    h.els['btn-call'].click(); h.els['btn-call'].click(); await flush();   // the fake dispatches clicks even when disabled
    assert.equal(h.log.gum, 0); assert.equal(h.log.pcs.length, 0); assert.equal(h.log.fetch.length, 0);
    assert.equal(h.state(), 'idle'); assert.equal(h.els['btn-call'].disabled, true);
  }
});

test('End Call: audio and mic stop, session.close sent once, actions disabled until session.closed, then everything is released', async () => {
  const h = harness();
  h.els['btn-call'].click(); await flush(); h.started(h.pc()); await flush();
  const pc = h.pc(), stream = h.log.streams[0];
  h.els['btn-end'].click(); await flush();
  assert.equal(h.state(), 'ending');
  assert.equal(stream.tracks[0].stopped, true);                   // mic released immediately
  assert.equal(h.els['remote-audio'].muted, true);
  assert.equal(h.els['btn-end'].disabled, true); assert.equal(h.els['btn-call'].hidden, true);
  assert.deepEqual(pc.dc.sent, [{ type: 'session.close' }]);
  h.els['btn-end'].click(); h.els['btn-call'].click(); await flush();
  assert.equal(pc.dc.sent.length, 1); assert.equal(h.sessionCalls().length, 1);   // no duplicate close, no new session
  const end = h.log.fetch.find((f) => f.path === '/api/call/end');
  assert.equal(end.headers['X-Call-Secret'], 'sec');
  pc.dc.onmessage({ data: JSON.stringify({ type: 'session.closed', usage: {} }) }); await flush();
  assert.equal(h.state(), 'idle'); assert.equal(pc.closed, true); assert.equal(h.els['remote-audio'].srcObject, null);
  assert.equal(pc.dc.onmessage, null);                            // listeners removed
  assert.equal(h.log.locations.length, 0);                        // no order -> stays on the page
});

test('connection lost during a call: error state, resources released, server notified', async () => {
  const h = harness();
  h.els['btn-call'].click(); await flush(); h.started(h.pc()); await flush();
  const pc = h.pc(); pc.dc.onclose(); await flush();
  assert.equal(h.state(), 'error'); assert.equal(pc.closed, true); assert.equal(h.log.streams[0].tracks[0].stopped, true);
  assert.ok(h.log.fetch.some((f) => f.path === '/api/call/end'));
  assert.match(h.els.error.textContent, /connection was lost/i);
});

test('page close during a call releases everything (idempotent teardown)', async () => {
  const h = harness();
  h.els['btn-call'].click(); await flush(); h.started(h.pc()); await flush();
  h.pagehide(); h.pagehide(); await flush();
  assert.equal(h.pc().closed, true); assert.equal(h.log.fetch.filter((f) => f.path === '/api/call/end').length, 1);
});

test('transcript is rendered as text (never HTML) from data-channel events', async () => {
  const h = harness();
  h.els['btn-call'].click(); await flush(); h.started(h.pc()); await flush();
  const send = (type, delta) => h.pc().dc.onmessage({ data: JSON.stringify({ type, delta }) });
  send('session.input_transcript.delta', 'two '); send('session.input_transcript.delta', '<img src=x onerror=1>');
  send('session.output_transcript.delta', 'Sure.');
  assert.equal(h.els['transcript-empty'].hidden, true);
  const lines = h.els.transcript.children;
  assert.equal(lines.length, 2);
  assert.equal(lines[0].children[1].textContent, 'two <img src=x onerror=1>');
});

test('a confirmed order seen while ending sends the customer to the confirmation page; the browser only ever calls /api/call/*', async () => {
  const order = { reference: 'ORD-ABCDEFGH', url: '/confirmation/ORD-ABCDEFGH?t=tok' };
  const h = harness({ route: (e) => (e.path.startsWith('/api/call/status') ? ok({ state: 'active', order }) : undefined) });
  h.els['btn-call'].click(); await flush(); h.started(h.pc()); await flush();
  await new Promise((r) => setTimeout(r, 700)); await flush();     // first heartbeat poll
  assert.equal(h.els['order-banner'].hidden, false); assert.match(h.els['order-text'].textContent, /ORD-ABCDEFGH/);
  assert.equal(h.els['order-link'].href, order.url);
  h.els['btn-end'].click(); await flush();
  h.pc().dc.onmessage({ data: JSON.stringify({ type: 'session.closed' }) }); await flush();
  assert.deepEqual(h.log.locations, [order.url]);
  assert.ok(h.paths().every((p) => p.startsWith('/api/call/')), 'browser must not call any order-mutating endpoint: ' + h.paths());
});

test('ending a call with no confirmed order does not navigate and submits nothing', async () => {
  const h = harness();
  h.els['btn-call'].click(); await flush(); h.started(h.pc()); await flush();
  h.els['btn-end'].click(); await flush();
  h.pc().dc.onmessage({ data: JSON.stringify({ type: 'session.closed' }) }); await flush();
  assert.equal(h.state(), 'idle'); assert.deepEqual(h.log.locations, []);
  assert.ok(h.paths().every((p) => p.startsWith('/api/call/')));
});

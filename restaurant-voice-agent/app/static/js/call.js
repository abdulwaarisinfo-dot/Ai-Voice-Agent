/* Browser side of the GPT-Live call.
 *
 * States:  idle -> connecting -> connected -> ending -> idle     (error is recoverable: "Try again")
 *
 * Protocol (OpenAI GPT-Live over WebRTC, SDP relay through our backend):
 *   1. mic -> RTCPeerConnection, create the data channel "oai-events" BEFORE the offer
 *   2. send the SDP offer to POST /api/call/session (the server holds the API key)
 *   3. apply the SDP answer; wait for the `session.started` event on the data channel
 *   4. only THEN show "Connected"
 *   5. End = send {type:"session.close"}, wait for `session.closed`, then release everything
 *
 * Race safety: every Call click gets a "generation". Any async step that resumes after the user
 * cancelled (or after a failure) sees a stale generation and releases what it acquired instead of
 * continuing. teardown() is idempotent.
 */
(() => {
  'use strict';
  const root = document.getElementById('call-root');
  if (!root) return;
  const $ = (id) => document.getElementById(id);
  const el = {
    call: $('btn-call'), end: $('btn-end'), cancel: $('btn-cancel'), status: $('status'), detail: $('status-detail'),
    error: $('error'), transcript: $('transcript'), empty: $('transcript-empty'), audio: $('remote-audio'),
    banner: $('order-banner'), bannerText: $('order-text'), link: $('order-link'), warn: $('support-warning'),
  };
  const cfg = { restaurantId: root.dataset.restaurantId, menuUrl: root.dataset.menuUrl };
  // No restaurant id => never start a call. The server renders a setup page without this script in that case;
  // this is the second lock, so an empty id can never fall through to the server's default restaurant.
  const configured = () => typeof cfg.restaurantId === 'string' && cfg.restaurantId.trim() !== '';
  const LABELS = { idle: 'Idle', connecting: 'Connecting', connected: 'Connected', ending: 'Ending', error: 'Error' };
  const CONNECT_TIMEOUT_MS = 20000, END_TIMEOUT_MS = 15000, ICE_TIMEOUT_MS = 10000, POLL_MS = 2000;

  let state = 'idle';
  let gen = 0;
  let ctx = null;

  const randomId = () => {
    const b = new Uint8Array(16); crypto.getRandomValues(b);
    return Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');
  };
  const stale = (c) => c.disposed || c.gen !== gen;
  const later = (c, fn, ms) => { const t = setTimeout(fn, ms); c.timers.add(t); return t; };
  const stopTracks = (stream) => { if (stream) stream.getTracks().forEach((t) => t.stop()); };

  function supported() {
    return !!(window.RTCPeerConnection && navigator.mediaDevices && navigator.mediaDevices.getUserMedia &&
      (window.isSecureContext !== false));
  }

  function setState(next, detail = '') {
    state = next;
    el.status.textContent = LABELS[next];
    el.status.className = 'status status-' + next;
    el.detail.textContent = detail;
    el.call.hidden = next === 'connected' || next === 'ending';
    el.call.disabled = next === 'connecting' || next === 'ending';
    el.call.textContent = next === 'connecting' ? 'Connecting…' : next === 'error' ? 'Try again' : 'Call restaurant';
    el.end.hidden = !(next === 'connected' || next === 'ending');
    el.end.disabled = next !== 'connected';
    el.cancel.hidden = next === 'connected' || next === 'ending';
    if (next !== 'error') showError('');
  }

  function showError(msg) { el.error.textContent = msg; el.error.hidden = !msg; }

  function describe(err) {
    if (err && (err.name === 'NotAllowedError' || err.name === 'SecurityError'))
      return 'Microphone access was denied. Allow the microphone for this site in your browser settings, then try again.';
    if (err && (err.name === 'NotFoundError' || err.name === 'OverconstrainedError'))
      return 'No microphone was found. Connect one and try again.';
    if (err && err.status === 429) return 'Too many attempts. Please wait a few minutes and try again.';
    if (err && err.status === 503) return (err.detail || 'Voice ordering is unavailable right now.') + ' You can still browse the menu.';
    if (err && err.status) return 'Voice ordering is unavailable right now (error ' + err.status + '). You can still browse the menu.';
    if (err && err.message === 'ice-timeout') return 'Could not prepare the connection. Check your network and try again.';
    if (err && err.name === 'TypeError') return 'Network error. Check your connection and try again.';
    return 'Something went wrong starting the call. Please try again.';
  }

  async function api(path, { method = 'GET', body, secret, signal, keepalive } = {}) {
    const headers = {};
    if (body) headers['Content-Type'] = 'application/json';
    if (secret) headers['X-Call-Secret'] = secret;
    const res = await fetch(path, { method, headers, body: body ? JSON.stringify(body) : undefined, signal, keepalive, credentials: 'same-origin' });
    if (!res.ok) {
      const err = new Error('http'); err.status = res.status;
      try { err.detail = (await res.json()).detail; } catch (_) { /* ignore */ }
      throw err;
    }
    return res.json();
  }

  function waitForIce(pc) {
    return new Promise((resolve, reject) => {
      if (pc.iceGatheringState === 'complete') return resolve();
      const t = setTimeout(() => { done(); reject(new Error('ice-timeout')); }, ICE_TIMEOUT_MS);
      function on() { if (pc.iceGatheringState === 'complete') { done(); resolve(); } }
      function done() { clearTimeout(t); pc.removeEventListener('icegatheringstatechange', on); }
      pc.addEventListener('icegatheringstatechange', on);
    });
  }

  // ---------- release everything. Safe to call any number of times. ----------
  function teardown(c, { notify = false } = {}) {
    if (c.disposed) return;
    c.disposed = true;
    c.timers.forEach(clearTimeout); c.timers.clear();
    clearTimeout(c.poll);
    try { if (c.abort) c.abort.abort(); } catch (_) { /* ignore */ }
    if (c.dc) { c.dc.onmessage = c.dc.onclose = c.dc.onopen = null; try { c.dc.close(); } catch (_) { /* ignore */ } }
    if (c.pc) { c.pc.ontrack = c.pc.onconnectionstatechange = null; try { c.pc.close(); } catch (_) { /* ignore */ } }
    stopTracks(c.stream);
    if (el.audio.pause) el.audio.pause();
    el.audio.srcObject = null; el.audio.muted = false;
    if (notify) notifyServer(c);
  }

  // Aborting fetch does NOT cancel server-side work, so tell the server explicitly.
  // If the request cannot be sent (offline, unload), it is retried once when the network returns. The server's
  // own idle / max-duration / never-ready timers remain the fallback if the browser never comes back.
  let pendingNotify = null;
  function notifyServer(c) {
    const p = c.secret
      ? api('/api/call/end', { method: 'POST', body: { client_call_id: c.id }, secret: c.secret, keepalive: true })
      : api('/api/call/cancel', { method: 'POST', body: { client_call_id: c.id }, keepalive: true });
    p.then(() => { if (pendingNotify && pendingNotify.id === c.id) pendingNotify = null; })
      .catch((err) => { if (!err || !err.status) pendingNotify = c; });  // network failure only; HTTP errors are final
  }

  function fail(c, msg) {
    if (c.disposed) return;
    teardown(c, { notify: true });
    setState('error');
    showError(msg);
  }

  // ---------- start ----------
  async function startCall() {
    if (!configured()) return;                                   // no valid restaurant: no mic, no session request
    if (state !== 'idle' && state !== 'error') return;           // duplicate-click guard
    if (!supported()) { el.warn.hidden = false; return; }
    const c = { gen: ++gen, id: randomId(), secret: null, pc: null, dc: null, stream: null, abort: null,
      timers: new Set(), poll: null, disposed: false, finishing: false, orderUrl: null, pollErrors: 0,
      lastRole: null, lastLine: null };
    ctx = c;
    setState('connecting', 'Requesting microphone…');
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      if (stale(c)) { stopTracks(stream); return; }               // cancelled while the prompt was open
      c.stream = stream;

      const pc = c.pc = new RTCPeerConnection();
      pc.ontrack = (e) => {
        if (stale(c)) return;
        el.audio.srcObject = new MediaStream([e.track]);
        const p = el.audio.play && el.audio.play();
        if (p && p.catch) p.catch(() => showError('Press play on the audio control to hear the assistant.'));
      };
      pc.onconnectionstatechange = () => {
        if (stale(c)) return;
        const s = pc.connectionState;
        if (s === 'failed' || s === 'closed') onLost(c);
        else if (s === 'disconnected') later(c, () => { if (pc.connectionState === 'disconnected') onLost(c); }, 8000);
      };
      stream.getAudioTracks().forEach((t) => pc.addTrack(t, stream));

      const dc = c.dc = pc.createDataChannel('oai-events');       // before createOffer, per the docs
      dc.onmessage = (ev) => { if (!stale(c)) onEvent(c, ev.data); };
      dc.onclose = () => { if (!stale(c)) onLost(c); };

      setState('connecting', 'Preparing connection…');
      await pc.setLocalDescription(await pc.createOffer());
      await waitForIce(pc);
      if (stale(c)) return;

      setState('connecting', 'Contacting the restaurant…');
      c.abort = new AbortController();
      const res = await api('/api/call/session', { method: 'POST', signal: c.abort.signal,
        body: { sdp: pc.localDescription.sdp, client_call_id: c.id, restaurant_id: cfg.restaurantId } });
      if (stale(c)) return;                                       // late response after Cancel: never start a call
      c.secret = res.call_secret;
      await pc.setRemoteDescription({ type: 'answer', sdp: res.transport.sdp });
      if (stale(c)) return;
      setState('connecting', 'Waiting for the assistant…');       // HTTP success is NOT "connected"
      later(c, () => fail(c, 'The call did not connect in time. Please try again.'), CONNECT_TIMEOUT_MS);
    } catch (err) {
      if (stale(c)) return;
      fail(c, describe(err));
    }
  }

  function onLost(c) {
    if (c.disposed) return;
    if (state === 'ending') finishEnd(c);
    else fail(c, 'The connection was lost. If you did not see an order confirmation, your order was not placed.');
  }

  function onEvent(c, raw) {
    let msg; try { msg = JSON.parse(raw); } catch (_) { return; }
    switch (msg.type) {
      case 'session.started': onStarted(c); break;
      case 'session.closed': if (state === 'ending' || state === 'connected') finishEnd(c); break;
      case 'session.input_transcript.delta': addTranscript(c, 'you', 'You', msg.delta); break;
      case 'session.output_transcript.delta': addTranscript(c, 'assistant', 'Assistant', msg.delta); break;
      default: break;
    }
  }

  function onStarted(c) {
    if (state !== 'connecting') return;
    c.timers.forEach(clearTimeout); c.timers.clear();
    setState('connected', 'Speak whenever you are ready. You can interrupt the assistant.');
    api('/api/call/ready', { method: 'POST', body: { client_call_id: c.id }, secret: c.secret }).catch(() => { /* poll will surface problems */ });
    poll(c, 500);
  }

  function poll(c, delay) {
    c.poll = setTimeout(async () => {
      if (stale(c) || state !== 'connected') return;
      try {
        const s = await api('/api/call/status?client_call_id=' + encodeURIComponent(c.id), { secret: c.secret });
        if (stale(c)) return;
        c.pollErrors = 0;
        if (s.order && !c.orderUrl) showOrder(c, s.order);
        if (s.state === 'failed' || s.state === 'cancelled') return fail(c, 'The ordering system disconnected. If you did not see an order confirmation, your order was not placed.');
        if (s.state === 'ended') return finishEnd(c);
      } catch (err) {
        if (stale(c)) return;
        if (err.status === 404 || ++c.pollErrors >= 5) return fail(c, 'Lost contact with the server.');
      }
      poll(c, POLL_MS);
    }, delay);
  }

  function showOrder(c, order) {
    c.orderUrl = order.url;
    el.bannerText.textContent = 'Your order is confirmed. Reference: ' + order.reference;
    el.link.href = order.url;
    el.banner.hidden = false;
  }

  function addTranscript(c, role, label, text) {
    if (!text) return;
    if (el.empty) el.empty.hidden = true;
    if (c.lastRole !== role) {
      const line = document.createElement('p');
      line.className = 'line ' + role;
      const who = document.createElement('span'); who.className = 'who'; who.textContent = label + ':';
      const body = document.createElement('span'); body.textContent = '';
      line.appendChild(who); line.appendChild(body);
      el.transcript.appendChild(line);
      c.lastRole = role; c.lastLine = body;
    }
    c.lastLine.textContent += text;                                // textContent only: never innerHTML
    el.transcript.scrollTop = el.transcript.scrollHeight;
  }

  // ---------- end ----------
  function endCall() {
    if (state !== 'connected') return;
    const c = ctx;
    setState('ending', 'Finishing the conversation…');              // also disables End Call
    stopTracks(c.stream);                                           // stop capturing immediately
    el.audio.muted = true;
    api('/api/call/end', { method: 'POST', body: { client_call_id: c.id }, secret: c.secret, keepalive: true }).catch(() => {});
    if (c.dc && c.dc.readyState === 'open') {
      c.dc.send(JSON.stringify({ type: 'session.close' }));         // documented graceful close
      later(c, () => finishEnd(c, 'Call ended. The service did not send a final confirmation.'), END_TIMEOUT_MS);
    } else {
      finishEnd(c);
    }
  }

  async function finishEnd(c, note = 'Call ended.') {
    if (c.disposed || c.finishing) return;
    c.finishing = true;
    if (!c.orderUrl && c.secret) {                                  // last look: did an order get confirmed?
      try { const s = await api('/api/call/status?client_call_id=' + encodeURIComponent(c.id), { secret: c.secret }); if (s.order) showOrder(c, s.order); } catch (_) { /* ignore */ }
    }
    teardown(c);
    if (c.orderUrl) { location.assign(c.orderUrl); return; }       // ending never confirms anything by itself
    setState('idle', note);
  }

  // ---------- cancel ----------
  function leave() {
    let same = false;
    try { same = !!document.referrer && new URL(document.referrer).origin === location.origin; } catch (_) { /* ignore */ }
    if (same && history.length > 1) history.back(); else location.assign(cfg.menuUrl);
  }

  function cancelClicked() {
    if (state === 'connecting' && ctx) {
      const c = ctx;
      gen += 1;                                                    // every pending await now sees a stale generation
      teardown(c, { notify: true });
      setState('idle', '');
    }
    leave();                                                       // no order is created by leaving
  }

  el.call.addEventListener('click', startCall);
  el.end.addEventListener('click', endCall);
  el.cancel.addEventListener('click', cancelClicked);
  window.addEventListener('pagehide', () => { if (ctx && !ctx.disposed) teardown(ctx, { notify: true }); });
  // Network lost: release the microphone and connection now instead of waiting for ICE to time out.
  window.addEventListener('offline', () => {
    if (ctx && !ctx.disposed && (state === 'connecting' || state === 'connected')) {
      fail(ctx, 'You are offline, so the call was ended. If you did not see an order confirmation, your order was not placed.');
    }
  });
  window.addEventListener('online', () => { const c = pendingNotify; pendingNotify = null; if (c) notifyServer(c); });
  // Restored from the back/forward cache: the old call was torn down on pagehide, so never show it as live.
  window.addEventListener('pageshow', (ev) => {
    if (ev.persisted && (!ctx || ctx.disposed) && state !== 'idle') setState('idle', '');
  });
  setState('idle');
  if (!configured()) el.call.disabled = true;                              // after setState, which would re-enable it
  if (!supported()) { el.warn.hidden = false; el.call.disabled = true; }   // after setState, which would re-enable it
  if (window.__CALL_TEST_HOOK__) window.__CALL_TEST_HOOK__({ getState: () => state, getGen: () => gen });
})();

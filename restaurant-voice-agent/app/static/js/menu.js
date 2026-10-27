/* Menu page: loads the menu from the API; owners can add/edit items and mark them (un)available. */
(() => {
  'use strict';
  const root = document.getElementById('menu-root');
  if (!root) return;
  const $ = (id) => document.getElementById(id);
  const rid = root.dataset.restaurantId;
  let currency = 'PKR', items = [], owner = false;

  async function api(path, opts = {}) {
    const res = await fetch(path, { credentials: 'same-origin', ...opts,
      headers: { ...(opts.body ? { 'Content-Type': 'application/json' } : {}) } });
    if (!res.ok) {
      let detail = 'Request failed (' + res.status + ')';
      try { const j = await res.json(); detail = typeof j.detail === 'string' ? j.detail : j.detail.map((d) => d.msg).join('; '); } catch (_) { /* ignore */ }
      const e = new Error(detail); e.status = res.status; throw e;
    }
    return res.json();
  }
  const text = (tag, cls, t) => { const n = document.createElement(tag); if (cls) n.className = cls; n.textContent = t; return n; };

  function render() {
    const list = $('menu-list'); list.replaceChildren();
    if (!items.length) { $('menu-status').textContent = 'The menu is empty.'; return; }
    $('menu-status').textContent = '';
    const cats = [...new Set(items.map((i) => i.category))];
    cats.forEach((cat) => {
      list.appendChild(text('h2', '', cat));
      items.filter((i) => i.category === cat).forEach((i) => {
        const card = document.createElement('article'); card.className = 'card' + (i.available ? '' : ' off');
        const left = document.createElement('div');
        const h = text('h3', '', i.name); left.appendChild(h);
        if (i.description) left.appendChild(text('p', 'muted', i.description));
        if (i.options.length) left.appendChild(text('p', 'muted', 'Options: ' + i.options.join(', ')));
        left.appendChild(text('span', 'badge' + (i.available ? '' : ' off'), i.available ? 'Available' : 'Unavailable'));
        const right = document.createElement('div');
        right.appendChild(text('div', 'price', i.price));
        if (owner) {
          const row = document.createElement('div'); row.className = 'row';
          const edit = text('button', 'btn small', 'Edit'); edit.type = 'button'; edit.onclick = () => fill(i);
          const tog = text('button', 'btn small', i.available ? 'Mark unavailable' : 'Mark available'); tog.type = 'button';
          tog.onclick = () => toggle(i);
          row.append(edit, tog); right.appendChild(row);
        }
        card.append(left, right); list.appendChild(card);
      });
    });
  }

  async function load() {
    try {
      const d = await api('/api/menu?restaurant_id=' + encodeURIComponent(rid));
      currency = d.restaurant.currency; items = d.items; $('f-currency').textContent = currency; render();
    } catch (e) { $('menu-status').textContent = 'Could not load the menu: ' + e.message; }
  }

  function setOwner(on) { owner = on; $('login-form').hidden = on; $('owner-panel').hidden = !on; render(); }
  function fill(i) {
    $('item-form-title').textContent = 'Edit item'; $('f-id').value = i.id; $('f-name').value = i.name; $('f-desc').value = i.description;
    $('f-cat').value = i.category; $('f-price').value = (i.price_minor / 100).toFixed(2); $('f-opts').value = i.options.join(', '); $('f-avail').checked = i.available;
    $('f-name').focus();
  }
  function reset() { $('item-form').reset(); $('f-id').value = ''; $('item-form-title').textContent = 'Add item'; $('item-error').hidden = true; }
  async function toggle(i) {
    try { await api('/api/menu/items/' + encodeURIComponent(i.id) + '/availability', { method: 'PATCH', body: JSON.stringify({ available: !i.available }) }); await load(); }
    catch (e) { $('menu-status').textContent = e.message; }
  }

  $('login-form').addEventListener('submit', async (ev) => {
    ev.preventDefault(); const err = $('login-error'); err.hidden = true;
    try { await api('/api/owner/login', { method: 'POST', body: JSON.stringify({ restaurant_id: rid, password: $('login-password').value }) });
      $('login-password').value = ''; setOwner(true); }
    catch (e) { err.textContent = e.message; err.hidden = false; }
  });
  $('logout').addEventListener('click', async () => { await api('/api/owner/logout', { method: 'POST' }).catch(() => {}); reset(); setOwner(false); });
  $('f-reset').addEventListener('click', reset);
  $('item-form').addEventListener('submit', async (ev) => {
    ev.preventDefault(); const err = $('item-error'); err.hidden = true;
    const id = $('f-id').value;
    const body = JSON.stringify({ name: $('f-name').value, description: $('f-desc').value, category: $('f-cat').value,
      price_minor: Math.round(parseFloat($('f-price').value) * 100), available: $('f-avail').checked,
      options: $('f-opts').value.split(',').map((s) => s.trim()).filter(Boolean) });
    try { await api(id ? '/api/menu/items/' + encodeURIComponent(id) : '/api/menu/items', { method: id ? 'PUT' : 'POST', body }); reset(); await load(); }
    catch (e) { err.textContent = e.message; err.hidden = false; }
  });

  api('/api/owner/me').then((m) => { if (m.restaurant_id === rid) setOwner(true); }).catch(() => setOwner(false)).finally(load);
})();

// "System (Raspberry Pi OS)" card on the display's and the hub's Settings: updates waiting, install, restart.
// Needs an element with id "system-card" on the page; talks to /api/system on the same app.
(() => {
  const card = document.getElementById('system-card');
  if (!card) return;
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const ago = (t) => {
    if (!t) return 'not yet';
    const m = Math.floor((Date.now() / 1000 - t) / 60);
    return m < 1 ? 'just now' : m < 60 ? `${m} min ago` : m < 1440 ? `${Math.floor(m / 60)} h ago` : `${Math.floor(m / 1440)} days ago`;
  };
  let st = null, timer, fastUntil = 0;
  async function act(method, path, body) {
    try {
      const r = await fetch(path, { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
      if (!r.ok) { const d = await r.json().catch(() => ({})); alert(d.error || d.detail || 'That didn\'t work'); }
    } catch (e) { /* the Pi restarts */ }
    fastUntil = Date.now() + 120000;
    load();
  }
  function draw(away) {
    if (!st) return;
    let badge = 'up to date', tone = '', line = st.supported ? `Checked ${ago(st.checked_at)}` : 'Not set up yet: run sudo bash /opt/jplayer/deploy/install.sh once';
    if (away) badge = 'restarting…';
    else if (!st.supported) badge = 'not set up';
    else if (st.state === 'installing') { badge = 'installing…'; line = st.message; }
    else if (st.state === 'checking') badge = 'checking…';
    else if (st.state === 'failed') { badge = 'failed'; tone = 'bad'; line = st.message; }
    else if (st.updates) { badge = `${st.updates} update${st.updates === 1 ? '' : 's'}`; tone = 'new'; }
    else if (st.reboot_needed) { badge = 'restart needed'; tone = 'new'; line = 'An update is finished once the Pi restarts.'; }
    const busy = away || st.in_progress;
    card.innerHTML = `
      <h3 style="margin-top:0">System (Raspberry Pi OS)</h3>
      <div class="sys-line"><b>${esc(st.os || 'Raspberry Pi OS')}</b><span class="sys-badge ${tone}">${esc(badge)}</span></div>
      <div class="sys-muted">${esc(line)}</div>
      ${st.updates && !busy ? `<div class="sys-muted">Waiting: ${esc(st.packages.slice(0, 12).join(', '))}${st.packages.length > 12 ? '…' : ''}</div>` : ''}
      ${st.supported ? `<div class="sys-buttons">
        ${st.updates && !busy ? '<button type="button" class="sys-btn primary" data-act="install">Install updates</button>' : ''}
        ${st.reboot_needed && !st.updates && !busy ? '<button type="button" class="sys-btn primary" data-act="reboot">Restart now</button>' : ''}
        <button type="button" class="sys-btn" data-act="check" ${busy ? 'disabled' : ''}>Check now</button></div>` : ''}
      ${(st.state === 'failed' || st.in_progress) && st.log?.length ? `<pre class="sys-log">${esc(st.log.join('\n'))}</pre>` : ''}
      ${st.supported ? `<label class="sys-check"><input type="checkbox" data-act="auto" ${st.auto === 'night' ? 'checked' : ''}>
        Install system updates automatically at night (03:00–05:00, when nothing plays) and restart when needed</label>` : ''}`;
  }
  async function load() {
    clearTimeout(timer);
    let away = false;
    try {
      const r = await fetch('/api/system', { cache: 'no-store' });
      if (!r.ok) throw new Error();
      st = await r.json();
    } catch (e) { away = true; }
    draw(away);
    timer = setTimeout(load, away || st?.in_progress || Date.now() < fastUntil ? 3000 : 60000);
  }
  card.addEventListener('click', (e) => {
    const a = e.target.closest('[data-act]')?.dataset.act;
    if (a === 'install' && confirm(`Install ${st.updates} system updates? Music may stop for a moment; the Pi restarts afterwards if an update needs it (about a minute).`)) act('POST', '/api/system/install', { reboot: true });
    else if (a === 'reboot' && confirm('Restart the Pi now? It is back in about a minute.')) act('POST', '/api/system/reboot');
    else if (a === 'check') act('POST', '/api/system/check');
  });
  card.addEventListener('change', (e) => {
    if (e.target.dataset.act === 'auto') act('PUT', '/api/system/settings', { auto: e.target.checked ? 'night' : 'off' });
  });
  load();
})();

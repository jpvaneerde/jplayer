// Shared by the hub's pages.
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

async function api(path, opts = {}) {
  const init = { cache: 'no-store', ...opts };
  if (opts.body !== undefined && typeof opts.body !== 'string') {
    init.body = JSON.stringify(opts.body);
    init.headers = { 'Content-Type': 'application/json' };
  }
  const r = await fetch(path, init);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || data.error || `HTTP ${r.status}`);
  return data;
}

function toast(msg) {
  const t = document.createElement('div');
  t.className = 'toast';
  t.textContent = msg;
  document.body.append(t);
  setTimeout(() => t.remove(), 2600);
}

// A cover: one of your records goes through the hub (it has JPlay Records' token), anything else directly.
function coverUrl(u) {
  if (!u) return '/static/icon.svg';
  return /\/covers\//.test(u) ? `/api/cover?u=${encodeURIComponent(u)}` : u;
}

const time = (ms) => new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
function dayLabel(ms) {
  const d = new Date(ms), today = new Date();
  const y = new Date(); y.setDate(today.getDate() - 1);
  if (d.toDateString() === today.toDateString()) return 'Today';
  if (d.toDateString() === y.toDateString()) return 'Yesterday';
  return d.toLocaleDateString([], { weekday: 'long', day: 'numeric', month: 'long', year: d.getFullYear() === today.getFullYear() ? undefined : 'numeric' });
}
function ago(ms) {
  const s = Math.round((Date.now() - ms) / 1000);
  if (s < 90) return 'just now';
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 129600) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}

function header(page) {
  const links = [['/', 'Spins'], ['/lyrics', 'Lyrics'], ['/settings', 'Settings']];
  document.body.insertAdjacentHTML('afterbegin', `<header><a href="#" class="back" id="back-display" hidden>‹ Display</a>
    <div class="brand"><img src="/static/icon.svg" alt="">JPlay hub</div>
    <nav>${links.map(([h, t]) => `<a href="${h}" class="${h === page ? 'on' : ''}">${t}</a>`).join('')}</nav></header>`);
  // Back to the display: the one this page was opened from, else the one on this Pi (on the Pi's own screen
  // there is no browser Back button)
  const back = $('back-display');
  let from = null;
  try { from = sessionStorage.getItem('jplay-display'); } catch (e) { /* no storage: fine */ }
  const ref = document.referrer && /:5000\//.test(document.referrer) ? new URL(document.referrer).origin + '/' : null;
  if (ref) { from = ref; try { sessionStorage.setItem('jplay-display', ref); } catch (e) { /* ignore */ } }
  if (from) { back.href = from; back.hidden = false; }
  else {
    api('/api/info').then((i) => {
      if ((i.roles || []).includes('display')) { back.href = `${location.protocol}//${location.hostname}:5000/`; back.hidden = false; }
    }).catch(() => {});
  }
}

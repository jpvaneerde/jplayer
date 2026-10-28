// Shared by the hub's pages.

// Theme before anything is drawn (this script loads at the end of <body>, the <head> snippet sets it earlier)
const THEMES = [['dark', 'Dark'], ['light', 'Light'], ['retro', 'Retro']];
function currentTheme() {
  let t = null;
  try { t = localStorage.getItem('jplay.theme'); } catch (e) { /* private mode */ }
  return THEMES.some(([id]) => id === t) ? t : 'dark';
}
document.documentElement.className = currentTheme();
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
  const links = [['/', 'Home'], ['/spins', 'Spins'], ['/lyrics', 'Lyrics'], ['/settings', 'Settings']];
  document.body.insertAdjacentHTML('afterbegin', `<header><a href="#" class="back" id="back-display" hidden>‹ Display</a>
    <div class="brand"><img src="/static/icon.svg" alt=""><span>JPlay hub</span></div>
    <nav>${links.map(([h, t]) => `<a href="${h}" class="${h === page ? 'on' : ''}">${t}</a>`).join('')}</nav>
    <button class="theme-btn" id="theme-btn" aria-label="Theme"><svg viewBox="0 0 24 24"><path d="M12 3a9 9 0 1 0 0 18zm0 2v14a7 7 0 0 0 0-14z" fill-rule="evenodd"/><circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="2"/></svg></button></header>`);
  const themeBtn = $('theme-btn');
  const label = () => {
    const i = THEMES.findIndex(([id]) => id === document.documentElement.className);
    themeBtn.title = `Theme: ${THEMES[i][1]} (tap for ${THEMES[(i + 1) % THEMES.length][1].toLowerCase()})`;
  };
  label();
  themeBtn.addEventListener('click', () => {
    const i = THEMES.findIndex(([id]) => id === document.documentElement.className);
    const t = THEMES[(i + 1) % THEMES.length][0];
    document.documentElement.className = t;
    try { localStorage.setItem('jplay.theme', t); } catch (e) { /* private mode */ }
    label();
  });
  // Back to the display: the one this page was opened from, else the one on this Pi (on the Pi's own screen
  // there is no browser Back button)
  const back = $('back-display');
  let from = null;
  try { from = sessionStorage.getItem('jplay-display'); } catch (e) { /* no storage: fine */ }
  // Came from a display (http://<pi>:5000 or https://<room>-display.<domain>): back to that one
  const ref = document.referrer && (/:5000\//.test(document.referrer) || /^https:\/\/[^/]*display\./.test(document.referrer))
    ? new URL(document.referrer).origin + '/' : null;
  if (ref) { from = ref; try { sessionStorage.setItem('jplay-display', ref); } catch (e) { /* ignore */ } }
  if (from) { back.href = from; back.hidden = false; }
  else {
    api('/api/info').then((i) => {
      // The display on this Pi: its HTTPS address when HTTPS is on, else this Pi's own address
      if (i.display_url) { back.href = i.display_url; back.hidden = false; }
      else if ((i.roles || []).includes('display') && location.protocol === 'http:') {
        back.href = `${location.protocol}//${location.hostname}:5000/`; back.hidden = false;
      }
    }).catch(() => {});
  }
  // An installable app (Add to Home Screen / Install app): the service worker (only over HTTPS or on localhost)
  if ('serviceWorker' in navigator && (location.protocol === 'https:' || location.hostname === 'localhost')) {
    navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(() => {});
  }
}

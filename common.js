async function api(path, opts = {}, headers = {}) {
  const r = await fetch(path, {
    method: opts.method || 'GET',
    headers: { 'Content-Type': 'application/json', ...headers },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(typeof d.detail === 'string' ? d.detail : 'Request failed');
  return d;
}
function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function waLink(phone, text) {
  let p = String(phone).replace(/\D/g, '');
  if (p.startsWith('0')) p = '92' + p.slice(1);
  return 'https://wa.me/' + p + '?text=' + encodeURIComponent(text);
}
function money(n) { return 'Rs ' + Number(n || 0).toLocaleString(); }
function badge(s) { return `<span class="badge ${esc(s)}">${esc(String(s).replace('_', ' '))}</span>`; }
function flash(el, text, ok) {
  el.innerHTML = text ? `<div class="msg ${ok ? 'ok' : 'err'}">${esc(text)}</div>` : '';
}

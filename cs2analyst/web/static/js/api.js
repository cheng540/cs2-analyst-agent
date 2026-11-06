/* API 客户端 + 格式化工具。所有请求都走同源的 /api/*。 */

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

async function request(path, { method = 'GET', body, signal } = {}) {
  const options = { method, headers: {}, signal, credentials: 'same-origin' };
  if (body !== undefined) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  const resp = await fetch(path, options);
  let payload = null;
  const text = await resp.text();
  try {
    payload = text ? JSON.parse(text) : {};
  } catch (e) {
    throw new ApiError(text.slice(0, 200) || '返回内容不是 JSON', resp.status);
  }
  if (!resp.ok) {
    throw new ApiError((payload && payload.error) || `请求失败（${resp.status}）`, resp.status);
  }
  return payload;
}

export const api = {
  health: () => request('/api/health'),
  summary: () => request('/api/summary'),
  holdings: () => request('/api/holdings'),
  history: (days = 30) => request(`/api/history?days=${days}`),
  kline: (name, days = 180) => request(`/api/kline?name=${encodeURIComponent(name)}&days=${days}`),
  reports: () => request('/api/reports'),
  sim: () => request('/api/sim'),
  ask: (question, history) => request('/api/ask', { method: 'POST', body: { question, history } }),
  knowledgeSearch: (q, k = 5) => request(`/api/knowledge/search?q=${encodeURIComponent(q)}&k=${k}`),
  knowledgeStats: () => request('/api/knowledge/stats'),
  setup: () => request('/api/setup'),
  setupApply: (payload) => request('/api/setup', { method: 'POST', body: payload }),
  config: () => request('/api/config'),
  configWrite: (updates) => request('/api/config', { method: 'POST', body: { updates } }),
  inventory: () => request('/api/inventory'),
  addHolding: (item) => request('/api/inventory', { method: 'POST', body: { item } }),
  updateHolding: (index, item) => request('/api/inventory', { method: 'PUT', body: { index, item } }),
  deleteHolding: (index) => request(`/api/inventory?index=${index}`, { method: 'DELETE' }),
  parseInventory: (text) => request('/api/inventory/parse', { method: 'POST', body: { text } }),
  bulkImport: (items, mode) => request('/api/inventory/bulk', { method: 'POST', body: { items, mode } }),
  visionImport: (images) => request('/api/inventory/vision', { method: 'POST', body: { images } }),
  plan: () => request('/api/plan'),
  watchlist: () => request('/api/watchlist'),
  addWatch: (name, note) => request('/api/watchlist', { method: 'POST', body: { market_hash_name: name, note } }),
  delWatch: (name) => request(`/api/watchlist?name=${encodeURIComponent(name)}`, { method: 'DELETE' }),
};

/* ---------------- 格式化 ---------------- */
const nf = (digits = 2) => new Intl.NumberFormat('zh-CN', {
  minimumFractionDigits: digits, maximumFractionDigits: digits,
});

export const fmt = {
  money(value, digits = 2) {
    const n = Number(value);
    if (!Number.isFinite(n)) return '—';
    return `¥${nf(digits).format(n)}`;
  },
  num(value, digits = 2) {
    const n = Number(value);
    if (!Number.isFinite(n)) return '—';
    return nf(digits).format(n);
  },
  pct(value, digits = 2, withSign = true) {
    const n = Number(value);
    if (!Number.isFinite(n)) return '—';
    const sign = withSign && n > 0 ? '+' : '';
    return `${sign}${nf(digits).format(n)}%`;
  },
  qty(value) {
    const n = Number(value);
    return Number.isFinite(n) ? String(n) : '—';
  },
  date(value) {
    return value ? String(value) : '—';
  },
};

/** 涨跌配色：国内习惯，涨红跌绿。 */
export function trendClass(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n === 0) return 'flat';
  return n > 0 ? 'up' : 'down';
}

export function escapeHtml(text) {
  return String(text ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

/** 极简 Markdown：**粗体** / 换行 / 列表，够用就行，不引第三方库。 */
export function miniMarkdown(text) {
  return escapeHtml(text)
    .replace(/\*\*(.+?)\*\*/g, '<b>$1</b>')
    .replace(/^[-*]\s+/gm, '· ')
    .replace(/\n/g, '<br>');
}

export function toast(message, kind = '') {
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.textContent = message;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 2600);
}

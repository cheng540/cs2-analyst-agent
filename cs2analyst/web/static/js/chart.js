/* 自绘 SVG 折线图。
   为什么不用 ECharts：一份构建产物要同时挂本地和线上（2C2G 服务器），
   CDN 在弱网/离线时是单点故障。这里只需要折线 + 面积 + 网格，手写 100 行足够，
   还省掉一个 1MB 的依赖。 */

import { fmt } from './api.js';

const W = 680;

function niceTicks(min, max, count = 4) {
  if (!Number.isFinite(min) || !Number.isFinite(max)) return [0];
  if (min === max) return [min];
  const step = (max - min) / count;
  return Array.from({ length: count + 1 }, (_, i) => min + step * i);
}

function shortDate(text) {
  const s = String(text || '');
  return s.length >= 10 ? s.slice(5) : s;
}

/**
 * 画一条/多条折线。
 * @param {HTMLElement} container
 * @param {{dates: string[], series: {label: string, values: number[], color: string, fill?: boolean}[], height?: number, valueFormat?: (n:number)=>string}} opts
 */
export function lineChart(container, opts) {
  const { dates = [], series = [], height = 200, valueFormat = (n) => fmt.num(n, 2) } = opts || {};
  container.innerHTML = '';

  const usable = series.filter((s) => Array.isArray(s.values) && s.values.length);
  if (!dates.length || !usable.length) {
    container.innerHTML = '<div class="empty">暂无数据（等云端流水线跑出快照后就有了）</div>';
    return;
  }

  const all = usable.flatMap((s) => s.values.map(Number)).filter(Number.isFinite);
  let min = Math.min(...all);
  let max = Math.max(...all);
  if (!Number.isFinite(min) || !Number.isFinite(max)) {
    container.innerHTML = '<div class="empty">数据不可用</div>';
    return;
  }
  const pad = (max - min) * 0.12 || Math.max(1, max * 0.05);
  min -= pad;
  max += pad;

  const padL = 52;
  const padR = 14;
  const padT = 14;
  const padB = 24;
  const innerW = W - padL - padR;
  const innerH = height - padT - padB;
  const n = dates.length;
  const x = (i) => padL + (n === 1 ? innerW / 2 : (innerW * i) / (n - 1));
  const y = (v) => padT + innerH - ((Number(v) - min) / (max - min)) * innerH;

  const ticks = niceTicks(min, max, 4);
  const grid = ticks.map((t) => `
    <line x1="${padL}" x2="${W - padR}" y1="${y(t).toFixed(1)}" y2="${y(t).toFixed(1)}"
          stroke="#eef1f6" stroke-width="1"/>
    <text x="${padL - 8}" y="${(y(t) + 3.5).toFixed(1)}" text-anchor="end"
          font-size="10" fill="#8b95a6">${valueFormat(t)}</text>`).join('');

  const xTickCount = Math.min(4, n);
  const xTicks = Array.from({ length: xTickCount }, (_, k) => {
    const i = xTickCount === 1 ? 0 : Math.round(((n - 1) * k) / (xTickCount - 1));
    return `<text x="${x(i).toFixed(1)}" y="${height - 6}" text-anchor="${k === 0 ? 'start' : k === xTickCount - 1 ? 'end' : 'middle'}"
                  font-size="10" fill="#8b95a6">${shortDate(dates[i])}</text>`;
  }).join('');

  const paths = usable.map((s) => {
    const pts = s.values.map((v, i) => [x(i), y(v)]);
    const d = pts.map((p, i) => `${i ? 'L' : 'M'}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(' ');
    const area = s.fill
      ? `<path d="${d} L${pts[pts.length - 1][0].toFixed(1)},${(padT + innerH).toFixed(1)} L${pts[0][0].toFixed(1)},${(padT + innerH).toFixed(1)} Z"
              fill="${s.color}" opacity=".08"/>`
      : '';
    const dots = n <= 90 ? pts.map((p, i) => `<circle cx="${p[0].toFixed(1)}" cy="${p[1].toFixed(1)}" r="2.1"
        fill="#fff" stroke="${s.color}" stroke-width="1.6"><title>${dates[i]}　${valueFormat(s.values[i])}</title></circle>`).join('') : '';
    const last = pts[pts.length - 1];
    return `${area}<path d="${d}" fill="none" stroke="${s.color}" stroke-width="1.9"
              stroke-linejoin="round" stroke-linecap="round"/>
            ${dots}
            <circle cx="${last[0].toFixed(1)}" cy="${last[1].toFixed(1)}" r="3" fill="${s.color}"/>`;
  }).join('');

  container.innerHTML = `
    <svg class="chart" viewBox="0 0 ${W} ${height}" role="img">
      ${grid}${xTicks}${paths}
    </svg>
    <div class="chart-legend">
      ${usable.map((s) => `<span><i style="background:${s.color}"></i>${s.label}
        <span class="muted">最新 ${valueFormat(s.values[s.values.length - 1])}</span></span>`).join('')}
    </div>`;
}

/** 迷你走势（用于表格行内），只画线，不带坐标。 */
export function sparkline(values, { width = 90, height = 24, color = '#1a6ee0' } = {}) {
  const nums = (values || []).map(Number).filter(Number.isFinite);
  if (nums.length < 2) return '';
  const min = Math.min(...nums);
  const max = Math.max(...nums);
  const span = max - min || 1;
  const d = nums.map((v, i) => {
    const px = (width * i) / (nums.length - 1);
    const py = height - 2 - ((v - min) / span) * (height - 4);
    return `${i ? 'L' : 'M'}${px.toFixed(1)},${py.toFixed(1)}`;
  }).join(' ');
  return `<svg width="${width}" height="${height}" viewBox="0 0 ${width} ${height}">
    <path d="${d}" fill="none" stroke="${color}" stroke-width="1.5" stroke-linejoin="round"/></svg>`;
}

/* 四个页面：大盘 / 持仓 / 问一问 / 设置。
   约定：每个 render* 函数接收容器，自己负责加载态与错误态；不写全局状态。 */

import { api, escapeHtml, fmt, miniMarkdown, toast, trendClass } from './api.js';
import { lineChart } from './chart.js';

const COLORS = { index: '#1a6ee0', value: '#d92b2b', cost: '#8b95a6' };

/* 配置项的中文标签（不然页面上全是 api_key / stop_loss_pct 这种字段名）。 */
const KEY_LABELS = {
  'steamdt.api_key': 'SteamDT Key', 'steamdt.platform': '平台过滤', 'steamdt.timeout': '接口超时（秒）',
  'llm.api_key': 'DeepSeek Key', 'llm.model': '模型', 'llm.enabled': '启用大模型',
  'notify.enabled': '启用推送', 'notify.wecom_webhook': '企业微信 webhook',
  'notify.only_if_actions': '仅在有操作时推送',
  'alerts.enabled': '启用盘中巡检', 'alerts.times': '巡检时间点', 'alerts.summary_times': '盘中概览时间点',
  'alerts.urgent_drop_pct': '紧急下跌阈值（%）', 'alerts.urgent_rise_pct': '紧急上涨阈值（%）',
  'alerts.ai_commentary': '附加 AI 点评', 'alerts.interval_seconds': '取价间隔（秒）',
  'decision.stop_loss_pct': '止损线（小数，0.3=30%）',
  'decision.hold_loss_pct': '持有告警亏损线（小数）',
  'decision.take_profit_pct': '止盈线（小数）',
  'sim.initial_capital': '模拟盘初始资金', 'sim.max_position_pct': '单票上限（%）',
  'chat.max_questions_per_day': '每日提问上限',
  'schedule.enabled': '启用本地定时', 'schedule.hour': '小时', 'schedule.minute': '分钟',
};

/* ---------------- 通用小组件 ---------------- */
function loading(container, text = '加载中…') {
  container.innerHTML = `<div class="card"><span class="spin"></span> <span class="muted">${text}</span></div>`;
}

function errorCard(container, err) {
  container.innerHTML = `<div class="card">
    <div class="notice err">加载失败：${escapeHtml(err.message || err)}</div>
    <div class="small muted">检查服务是否在运行：<code>python tools/report_server.py</code>；
    或者数据还没生成：<code>python main.py run</code></div></div>`;
}

function metric(label, value, hint = '', cls = '') {
  return `<div class="metric"><div class="label">${escapeHtml(label)}</div>
    <div class="value ${cls}">${value}</div>
    ${hint ? `<div class="hint">${hint}</div>` : ''}</div>`;
}

function actionPill(action) {
  const text = String(action || '—');
  let cls = 'gray';
  if (/买入|加仓|试仓|建仓/.test(text)) cls = 'red';
  else if (/清仓|卖出|减仓|止损/.test(text)) cls = 'green';
  else if (/持有|观望/.test(text)) cls = 'amber';
  return `<span class="pill ${cls}">${escapeHtml(text)}</span>`;
}

function freshnessNotice(fresh) {
  if (!fresh) return '';
  if (fresh.stale) {
    return `<div class="notice warn">⚠️ 最新数据是 <b>${escapeHtml(fresh.day || '未知')}</b>
      （${escapeHtml(fresh.hint)}）。下面的数字都是那天的，不是现在的。</div>`;
  }
  return '';
}

/* ================= 大盘 ================= */
export async function renderDashboard(root) {
  loading(root, '读取最新快照…');
  try {
    const [summary, history] = await Promise.all([api.summary(), api.history(90)]);
    const m = summary.market || {};
    const p = summary.portfolio || {};
    const chg = Number(m.chg_pct);
    const pnlCls = trendClass(p.pnl);

    root.innerHTML = `
      ${freshnessNotice(summary.freshness)}
      <div class="grid c4">
        ${metric('大盘指数', fmt.num(m.index), `${fmt.pct(chg)} 较昨日`, trendClass(chg))}
        ${metric('趋势 / 风险', `${escapeHtml(m.trend || '—')}`, `${escapeHtml(m.risk || '')} · ${escapeHtml(m.volume || '')}`)}
        ${metric('持仓市值', fmt.money(p.value), `成本 ${fmt.money(p.cost)}`)}
        ${metric('浮盈亏', fmt.money(p.pnl), fmt.pct(p.pnl_pct), pnlCls)}
      </div>

      <div class="card">
        <h3>大盘指数走势 <span class="sub">近 90 天收盘</span></h3>
        <div id="mchart"></div>
      </div>

      <div class="grid c2">
        <div class="card">
          <h3>持仓应对 <span class="sub">按系统建议排序</span></h3>
          <div class="table-wrap">
            <table><thead><tr>
              <th>标的</th><th>建议</th><th class="num">信心</th><th class="num">盈亏</th>
            </tr></thead><tbody>
            ${(summary.signals || []).map((s) => `
              <tr>
                <td><span class="name" title="${escapeHtml(s.name)}">${escapeHtml(s.name)}</span>
                    ${s.here ? '' : '<span class="pill gray">观察池</span>'}</td>
                <td>${actionPill(s.action)}</td>
                <td class="num">${s.conviction ?? '—'}</td>
                <td class="num ${trendClass(s.pnl_pct)}">${s.pnl_pct === undefined || s.pnl_pct === null ? '—' : fmt.pct(s.pnl_pct)}</td>
              </tr>`).join('') || '<tr><td colspan="4" class="empty">没有信号</td></tr>'}
            </tbody></table>
          </div>
        </div>

        <div class="card">
          <h3>大盘基调</h3>
          <pre class="plain">${escapeHtml(m.stance || '—')}</pre>
          <div class="kv" style="margin-top:10px">
            <span>情绪分</span><span>${fmt.num(m.sentiment, 1)}</span>
          </div>
          <div class="kv"><span>快照日期</span><span>${escapeHtml(summary.date || '—')}</span></div>
        </div>
      </div>

      <div class="card">
        <h3>AI 研判 <span class="sub">${escapeHtml(summary.judgement?.model || '')}</span></h3>
        ${summary.judgement?.error ? `<div class="notice warn">研判未完成：${escapeHtml(summary.judgement.error)}</div>` : ''}
        <div class="small"><b>大盘</b></div>
        <pre class="plain">${escapeHtml(summary.judgement?.market_view || '（无）')}</pre>
        <div class="small" style="margin-top:10px"><b>持仓</b></div>
        <pre class="plain">${escapeHtml(summary.judgement?.portfolio_advice || '（无）')}</pre>
        ${(summary.judgement?.risk_warnings || []).length ? `
          <div class="notice warn" style="margin-top:10px">风险提示：${(summary.judgement.risk_warnings || []).map(escapeHtml).join('；')}</div>` : ''}
      </div>

      <div class="card">
        <h3>最近的报告 <span class="sub">Word，可下载</span></h3>
        ${(summary.reports || []).map((r) => `
          <div class="kv"><span><a href="${r.url}">${escapeHtml(r.name)}</a></span>
          <span class="muted small">${escapeHtml(r.mtime)} · ${(r.size / 1024).toFixed(0)} KB</span></div>`).join('')
          || '<div class="empty">还没有生成报告</div>'}
      </div>`;

    lineChart(root.querySelector('#mchart'), {
      dates: history.market.dates,
      series: [{ label: '大盘指数', values: history.market.index, color: COLORS.index, fill: true }],
      height: 210,
      valueFormat: (n) => fmt.num(n, 0),
    });
  } catch (err) {
    errorCard(root, err);
  }
}

/* ================= 持仓 ================= */
export async function renderHoldings(root) {
  loading(root, '读取持仓…');
  try {
    const [data, inv] = await Promise.all([api.holdings(), api.inventory()]);
    const readonly = !!inv.readonly;
    const rows = data.rows || [];
    const p = data.portfolio || {};

    root.innerHTML = `
      ${freshnessNotice(data.freshness)}
      <div class="grid c4">
        ${metric('条目 / 件数', `${p['条目数'] ?? rows.length} / ${p['件数'] ?? '—'}`)}
        ${metric('总成本', fmt.money(p['总成本']))}
        ${metric('总市值', fmt.money(p['总市值']))}
        ${metric('盈亏', fmt.money(p['总盈亏']), fmt.pct(p['盈亏%']), trendClass(p['盈亏%']))}
      </div>

      <div class="card">
        <h3>我的持仓 <span class="sub">点任意一行看明细与K线</span></h3>
        <div class="table-wrap">
          <table><thead><tr>
            <th>标的</th><th class="num hide-xs">数量</th><th class="num hide-xs">成本</th><th class="num">现价</th>
            <th class="num">盈亏</th><th class="hide-xs">位置</th>
            <th class="num hide-sm">支撑</th><th class="num hide-sm">压力</th><th>建议</th>
          </tr></thead><tbody>
          ${rows.map((r, i) => `
            <tr class="clickable" data-row="${i}">
              <td><span class="name" title="${escapeHtml(r.name_cn)}">${escapeHtml(r.name_cn)}</span>
                  <span class="name-en" title="${escapeHtml(r.name)}">${escapeHtml(r.name)}</span></td>
              <td class="num hide-xs">${fmt.qty(r.qty)}</td>
              <td class="num hide-xs">${fmt.money(r.cost)}</td>
              <td class="num">${fmt.money(r.price)}</td>
              <td class="num ${trendClass(r.pnl_pct)}">${fmt.pct(r.pnl_pct)}</td>
              <td class="small muted hide-xs">${escapeHtml(r.position || '—')}</td>
              <td class="num hide-sm">${fmt.money(r.support)}</td>
              <td class="num hide-sm">${fmt.money(r.resistance)}</td>
              <td>${actionPill(r.action)}</td>
            </tr>`).join('') || '<tr><td colspan="9" class="empty">没有持仓</td></tr>'}
          </tbody></table>
        </div>
      </div>

      <div class="card">
        <h3>观察池 <span class="sub">${(data.watchlist || []).length} 个候选</span></h3>
        <div class="table-wrap">
          <table><thead><tr>
            <th>标的</th><th class="num">现价</th><th class="hide-xs">趋势</th>
            <th class="hide-sm">位置</th><th class="num hide-sm">评分</th><th>建议</th>
          </tr></thead><tbody>
          ${(data.watchlist || []).map((w) => `
            <tr>
              <td><span class="name" title="${escapeHtml(w['中文名'] || w['饰品'])}">${escapeHtml(w['中文名'] || w['饰品'])}</span></td>
              <td class="num">${fmt.money(w['现价'])}</td>
              <td class="small hide-xs">${escapeHtml(w['趋势'] || '—')}</td>
              <td class="small muted hide-sm">${escapeHtml(w['位置'] || '—')}</td>
              <td class="num hide-sm">${w['评分'] ?? '—'}</td>
              <td>${actionPill(w['系统建议'])}</td>
            </tr>`).join('') || '<tr><td colspan="6" class="empty">观察池为空</td></tr>'}
          </tbody></table>
        </div>
      </div>

      <div class="card">
        <h3>持仓原始记录 <span class="sub">config/inventory.json${readonly ? '（只读）' : '，可增删改'}</span></h3>
        ${readonly ? '<div class="notice info">线上演示环境为只读，持仓只能在本地改。</div>' : ''}
        <div class="table-wrap">
          <table><thead><tr>
            <th>英文标准名</th><th class="num">数量</th><th class="num">买入均价</th><th>买入日</th>
            <th>解锁</th><th>备注</th><th></th>
          </tr></thead><tbody>
          ${(inv.items || []).map((it, i) => `
            <tr>
              <td><span class="name-en" style="display:inline">${escapeHtml(it.market_hash_name)}</span></td>
              <td class="num">${fmt.qty(it.quantity)}</td>
              <td class="num">${fmt.money(it.buy_price)}</td>
              <td class="small">${escapeHtml(it.buy_date || '—')}</td>
              <td>${it.unlocked ? '<span class="pill green">已解锁</span>' : '<span class="pill amber">锁定期</span>'}</td>
              <td class="small muted">${escapeHtml((it.note || '').slice(0, 40))}</td>
              <td class="num">${readonly ? '' : `
                <button class="ghost" data-edit="${i}">改</button>
                <button class="ghost" data-del="${i}">删</button>`}</td>
            </tr>`).join('') || '<tr><td colspan="7" class="empty">还没有持仓记录</td></tr>'}
          </tbody></table>
        </div>
        ${readonly ? '' : `<div class="row" style="margin-top:12px">
          <button class="primary" id="add-holding">＋ 新增持仓</button>
          <button id="paste-import">从文本批量导入</button>
          <span class="small muted">粘贴平台库存里复制的几行，系统先解析成表格，你核对后再导入。</span>
        </div>`}
      </div>`;

    // ---- 交互 ----
    root.querySelectorAll('tr[data-row]').forEach((tr) => {
      tr.addEventListener('click', () => openHoldingDrawer(rows[Number(tr.dataset.row)]));
    });
    if (!readonly) {
      root.querySelector('#add-holding')?.addEventListener('click', () => openHoldingForm(null, () => renderHoldings(root)));
      root.querySelector('#paste-import')?.addEventListener('click', () => openPasteImport(() => renderHoldings(root)));
      root.querySelectorAll('[data-edit]').forEach((btn) => {
        btn.addEventListener('click', (ev) => {
          ev.stopPropagation();
          const idx = Number(btn.dataset.edit);
          openHoldingForm({ index: idx, item: inv.items[idx] }, () => renderHoldings(root));
        });
      });
      root.querySelectorAll('[data-del]').forEach((btn) => {
        btn.addEventListener('click', async (ev) => {
          ev.stopPropagation();
          const idx = Number(btn.dataset.del);
          const name = inv.items[idx]?.market_hash_name || '';
          if (!confirm(`删除这一条持仓？\n\n${name}\n\n（会先自动备份 config/inventory.json）`)) return;
          try {
            await api.deleteHolding(idx);
            toast('已删除');
            renderHoldings(root);
          } catch (err) {
            toast(err.message, 'err');
          }
        });
      });
    }
  } catch (err) {
    errorCard(root, err);
  }
}

function openHoldingDrawer(row) {
  const mask = document.createElement('div');
  mask.className = 'drawer-mask';
  const drawer = document.createElement('aside');
  drawer.className = 'drawer';
  drawer.innerHTML = `
    <header>
      <h3>${escapeHtml(row.name_cn)}</h3>
      <button class="ghost" id="close">关闭</button>
    </header>
    <div class="body">
      <div class="grid c2" style="margin-bottom:12px">
        ${metric('现价', fmt.money(row.price), `成本 ${fmt.money(row.cost)}`)}
        ${metric('盈亏', fmt.pct(row.pnl_pct), `${fmt.qty(row.qty)} 件`, trendClass(row.pnl_pct))}
      </div>
      <div class="kv"><span>建议</span><span>${actionPill(row.action)} 信心 ${row.conviction ?? '—'}</span></div>
      <div class="kv"><span>趋势 / 量价</span><span>${escapeHtml(row.trend || '—')} · ${escapeHtml(row.volume_price || '—')}</span></div>
      <div class="kv"><span>位置 / 流动性</span><span>${escapeHtml(row.position || '—')} · ${escapeHtml(row.liquidity || '—')}</span></div>
      <div class="kv"><span>支撑 / 压力</span><span>${fmt.money(row.support)} / ${fmt.money(row.resistance)}</span></div>
      <div class="kv"><span>T+7</span><span>${row.unlocked ? '已解锁' : '锁定期（买入日 ' + escapeHtml(row.buy_date || '未记录') + '）'}</span></div>
      <h3 style="margin:16px 0 8px;font-size:13px">最近 120 天走势</h3>
      <div id="kline"></div>
      <h3 style="margin:16px 0 8px;font-size:13px">当时为什么这么建议</h3>
      <pre class="plain">${(row.reasons || []).map((r) => '· ' + escapeHtml(r)).join('\n') || '—'}</pre>
      ${row.note ? `<h3 style="margin:16px 0 8px;font-size:13px">我的备注</h3>
        <pre class="plain">${escapeHtml(row.note)}</pre>` : ''}
    </div>`;
  document.body.append(mask, drawer);
  const close = () => { mask.remove(); drawer.remove(); };
  mask.addEventListener('click', close);
  drawer.querySelector('#close').addEventListener('click', close);

  api.kline(row.name, 120).then((data) => {
    lineChart(drawer.querySelector('#kline'), {
      dates: data.dates,
      series: [{ label: '收盘', values: data.close, color: COLORS.index, fill: true }],
      height: 170,
      valueFormat: (n) => fmt.money(n, 0),
    });
  }).catch(() => {
    drawer.querySelector('#kline').innerHTML = '<div class="empty">K线读取失败</div>';
  });
}

function openHoldingForm({ index = null, item = {} } = {}, onDone) {
  const isNew = index === null;
  const mask = document.createElement('div');
  mask.className = 'drawer-mask';
  const drawer = document.createElement('aside');
  drawer.className = 'drawer';
  drawer.innerHTML = `
    <header><h3>${isNew ? '新增持仓' : '修改持仓'}</h3><button class="ghost" id="close">关闭</button></header>
    <div class="body">
      <div class="notice info">英文标准名必须和平台一致（用 market hash name）。
        写错会让系统按另一个物品报价——纪念品 / 暗金 / ★ 刀尤其容易搞错。</div>
      <label class="field"><span>英文标准名 *</span>
        <input id="f-name" value="${escapeHtml(item.market_hash_name || '')}" placeholder="AK-47 | Redline (Field-Tested)"></label>
      <div class="row">
        <label class="field grow"><span>数量</span><input id="f-qty" type="number" min="0" step="1" value="${item.quantity ?? 1}"></label>
        <label class="field grow"><span>买入均价（¥）</span><input id="f-price" type="number" min="0" step="0.01" value="${item.buy_price ?? 0}"></label>
      </div>
      <div class="row">
        <label class="field grow"><span>买入日期</span><input id="f-date" type="date" value="${escapeHtml(item.buy_date || '')}"></label>
        <label class="field grow"><span>磨损档</span>
          <select id="f-wear">
            ${['', 'FN', 'MW', 'FT', 'WW', 'BS'].map((w) => `<option value="${w}" ${item.wear === w ? 'selected' : ''}>${w || '（不填）'}</option>`).join('')}
          </select></label>
      </div>
      <label class="field"><span>分件成本（逗号分隔，可留空）</span>
        <input id="f-lots" value="${escapeHtml((item.lots || []).join(','))}" placeholder="164,234">
        <div class="tip">同一物品分批买入且价格不同时填，用于「先卖哪一件」的分析。</div></label>
      <label class="field"><span>备注</span>
        <input id="f-note" value="${escapeHtml(item.note || '')}" placeholder="中文名：法玛斯 | ZX81 彩色"></label>
      <label class="field"><span>是否已过 T+7 锁定期</span>
        <select id="f-unlocked">
          <option value="true" ${item.unlocked ? 'selected' : ''}>已解锁（可卖）</option>
          <option value="false" ${item.unlocked ? '' : 'selected'}>锁定期内</option>
        </select></label>
    </div>
    <footer>
      <button id="cancel">取消</button>
      <button class="primary" id="save">保存</button>
    </footer>`;
  document.body.append(mask, drawer);
  const close = () => { mask.remove(); drawer.remove(); };
  mask.addEventListener('click', close);
  drawer.querySelector('#close').addEventListener('click', close);
  drawer.querySelector('#cancel').addEventListener('click', close);

  drawer.querySelector('#save').addEventListener('click', async (ev) => {
    const btn = ev.currentTarget;
    const payload = {
      market_hash_name: drawer.querySelector('#f-name').value.trim(),
      quantity: Number(drawer.querySelector('#f-qty').value),
      buy_price: Number(drawer.querySelector('#f-price').value),
      buy_date: drawer.querySelector('#f-date').value,
      wear: drawer.querySelector('#f-wear').value,
      lots: drawer.querySelector('#f-lots').value.split(',').map((s) => s.trim()).filter(Boolean).map(Number),
      note: drawer.querySelector('#f-note').value.trim(),
      unlocked: drawer.querySelector('#f-unlocked').value === 'true',
    };
    if (!payload.market_hash_name) { toast('英文标准名不能为空', 'err'); return; }
    btn.disabled = true;
    try {
      if (isNew) await api.addHolding(payload);
      else await api.updateHolding(index, { ...item, ...payload });
      toast('已保存（配置已备份）');
      close();
      onDone?.();
    } catch (err) {
      toast(err.message, 'err');
    } finally {
      btn.disabled = false;
    }
  });
}

/* ================= 持仓：从文本批量导入 ================= */
function openPasteImport(onDone) {
  const mask = document.createElement('div');
  mask.className = 'drawer-mask';
  const drawer = document.createElement('aside');
  drawer.className = 'drawer';
  drawer.style.width = 'min(820px, 96vw)';
  drawer.innerHTML = `
    <header>
      <h3>从文本批量导入持仓</h3>
      <button class="ghost" id="pi-close">关闭</button>
    </header>
    <div class="body">
      <div class="notice info">
        把平台库存页的内容复制粘贴进来，<b>一行一件</b>。支持这些写法：<br>
        <code>AK-47 | Redline (Field-Tested) 1 169</code>　
        <code>手套武器箱 2 92</code>　
        <code>AWP | Asiimov (Battle-Scarred) x2 ¥128.5</code><br>
        中文名会自动反查官方英文名（用你本地的名称映射表），但<b>解析结果必须过目</b>：
        名称少写磨损档、把总价当单价，都会让盈亏算错而且不报错。
      </div>
      <label class="field"><span>粘贴内容</span>
        <textarea id="pi-text" rows="9" placeholder="AK-47 | Redline (Field-Tested) 1 169
手套武器箱 2 92
法玛斯 | ZX81 彩色（崭新出厂） 164"></textarea>
      </label>
      <div class="row">
        <button class="primary" id="pi-parse">解析</button>
        <label class="btn" style="cursor:pointer;margin:0">
          上传截图识别
          <input type="file" id="pi-files" accept="image/png,image/jpeg,image/webp,image/gif"
                 multiple style="display:none">
        </label>
        <span class="small muted" id="pi-status"></span>
      </div>
      <div class="small muted" style="margin-top:6px">
        截图走 DeepSeek 多模态（同一把 Key，按量计费）· 最多 3 张 · 单张 ≤6MB ·
        识别结果和粘贴一样要人核对
      </div>
      <div id="pi-result" style="margin-top:12px"></div>
    </div>
    <footer>
      <label class="small muted" style="margin-right:auto;display:flex;align-items:center;gap:6px">
        <input type="checkbox" id="pi-unlocked" checked style="width:auto"> 这批都已过 T+7 锁定期（可卖）
      </label>
      <button id="pi-cancel">取消</button>
      <button class="primary" id="pi-save" disabled>确认导入</button>
    </footer>`;
  document.body.append(mask, drawer);

  const close = () => { mask.remove(); drawer.remove(); };
  mask.addEventListener('click', close);
  drawer.querySelector('#pi-close').addEventListener('click', close);
  drawer.querySelector('#pi-cancel').addEventListener('click', close);

  const status = drawer.querySelector('#pi-status');
  const resultBox = drawer.querySelector('#pi-result');
  const saveBtn = drawer.querySelector('#pi-save');
  let parsed = [];

  const CONF_PILL = { high: 'green', medium: 'amber', low: 'red' };
  const CONF_TEXT = { high: '可导入', medium: '要核对', low: '需补全' };

  function renderPreview(data) {
    parsed = data.items || [];
    const s = data.summary || {};
    status.textContent = `识别 ${s.total} 条 · 需核对 ${s.need_review} 条` +
      (s.already_exists ? ` · 已存在 ${s.already_exists} 条` : '') +
      (s.missing_price ? ` · 缺单价 ${s.missing_price} 条` : '');

    if (!parsed.length) {
      resultBox.innerHTML = '<div class="empty">没有可导入的条目</div>';
      saveBtn.disabled = true;
      return;
    }

    const sourceTag = data.source === 'vision'
      ? '<span class="pill blue">来自截图识别</span>'
      : '<span class="pill gray">来自粘贴文本</span>';
    resultBox.innerHTML = `
      <div class="row" style="margin-bottom:8px">${sourceTag}
        <span class="small muted">${escapeHtml((data.note || '').slice(0, 60))}</span></div>
      <div class="table-wrap">
        <table><thead><tr>
          <th style="width:34px"></th><th>英文名（可改）</th>
          <th class="num" style="width:76px">数量</th>
          <th class="num" style="width:96px">单价</th>
          <th>提示</th>
        </tr></thead><tbody>
        ${parsed.map((it, i) => `
          <tr>
            <td><input type="checkbox" data-ok="${i}" checked style="width:auto"></td>
            <td>
              <input data-name="${i}" value="${escapeHtml(it.market_hash_name || '')}"
                     style="min-width:260px">
              ${it.exists ? '<span class="pill gray">已存在</span>' : ''}
            </td>
            <td class="num"><input data-qty="${i}" type="number" min="1" step="1"
                 value="${it.quantity || 1}" style="width:64px;text-align:right"></td>
            <td class="num"><input data-price="${i}" type="number" min="0" step="0.01"
                 value="${it.buy_price ?? ''}" style="width:84px;text-align:right"
                 placeholder="必填"></td>
            <td class="small">
              <span class="pill ${CONF_PILL[it.confidence] || 'gray'}">${CONF_TEXT[it.confidence] || it.confidence}</span>
              <span class="muted">${escapeHtml(it.matched_by || '')}</span>
              ${(it.warnings || []).map((w) => `<div class="muted">· ${escapeHtml(w)}</div>`).join('')}
            </td>
          </tr>`).join('')}
        </tbody></table>
      </div>
      <div class="row" style="margin-top:12px">
        <label class="small muted" style="display:flex;align-items:center;gap:6px">
          <input type="radio" name="pi-mode" value="append" checked style="width:auto">
          追加到现有持仓
        </label>
        <label class="small muted" style="display:flex;align-items:center;gap:6px">
          <input type="radio" name="pi-mode" value="replace" style="width:auto">
          替换全部（清空原有）
        </label>
      </div>
      <div class="small muted" style="margin-top:6px">${escapeHtml(data.note || '')}</div>
      ${data.vision_raw ? `<details style="margin-top:8px"><summary class="small muted">
        看模型从图里读出的原文</summary><pre class="plain">${escapeHtml(data.vision_raw)}</pre></details>` : ''}`;

    saveBtn.disabled = false;
  }

  drawer.querySelector('#pi-parse').addEventListener('click', async (ev) => {
    const btn = ev.currentTarget;
    const text = drawer.querySelector('#pi-text').value;
    if (!text.trim()) { toast('先粘贴点内容', 'err'); return; }
    btn.disabled = true;
    status.textContent = '解析中…';
    try {
      const data = await api.parseInventory(text);
      renderPreview(data);
    } catch (err) {
      status.textContent = '';
      resultBox.innerHTML = `<div class="notice err">${escapeHtml(err.message)}</div>`;
      saveBtn.disabled = true;
    } finally {
      btn.disabled = false;
    }
  });

  const readAsDataURL = (file) => new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(new Error('读取图片失败'));
    reader.readAsDataURL(file);
  });

  drawer.querySelector('#pi-files').addEventListener('change', async (ev) => {
    const files = Array.from(ev.target.files || []).slice(0, 3);
    if (!files.length) return;
    status.textContent = '读取图片…';
    const images = [];
    for (const file of files) {
      if (file.size > 6 * 1024 * 1024) { toast(`${file.name} 超过 6MB，已跳过`, 'err'); continue; }
      try {
        images.push(await readAsDataURL(file));
      } catch (err) {
        toast(err.message, 'err');
      }
    }
    if (!images.length) { status.textContent = ''; ev.target.value = ''; return; }
    status.textContent = `识别中（${images.length} 张，通常 5–15 秒）…`;
    resultBox.innerHTML = '<div class="empty"><span class="spin"></span> 正在读图…</div>';
    try {
      const data = await api.visionImport(images);
      renderPreview(data);
    } catch (err) {
      status.textContent = '';
      resultBox.innerHTML = `<div class="notice err">${escapeHtml(err.message)}</div>
        <div class="small muted">可以换张更清晰的图，或直接用上面的「粘贴文本」。</div>`;
      saveBtn.disabled = true;
    } finally {
      ev.target.value = '';
    }
  });

  saveBtn.addEventListener('click', async (ev) => {
    const btn = ev.currentTarget;
    const unlocked = drawer.querySelector('#pi-unlocked').checked;
    const mode = (drawer.querySelector('input[name="pi-mode"]:checked') || {}).value || 'append';

    const items = [];
    parsed.forEach((it, i) => {
      const ok = drawer.querySelector(`[data-ok="${i}"]`);
      if (ok && !ok.checked) return;
      const name = drawer.querySelector(`[data-name="${i}"]`).value.trim();
      const qty = Number(drawer.querySelector(`[data-qty="${i}"]`).value);
      const priceRaw = drawer.querySelector(`[data-price="${i}"]`).value;
      if (!name) return;
      items.push({
        market_hash_name: name,
        quantity: Number.isFinite(qty) && qty > 0 ? qty : 1,
        buy_price: priceRaw === '' ? 0 : Number(priceRaw),
        lots: it.lots && it.lots.length ? it.lots : [],
        note: '',
        unlocked,
      });
    });

    if (!items.length) { toast('没有勾选任何条目', 'err'); return; }
    const missing = items.filter((i) => !i.buy_price).length;
    if (missing && !confirm(`有 ${missing} 条没有单价，导入后盈亏会不准。仍然导入？`)) return;
    if (mode === 'replace' && !confirm('「替换全部」会清空当前持仓列表，只保留这次导入的条目。确认？')) return;

    btn.disabled = true;
    try {
      const res = await api.bulkImport(items, mode);
      toast(res.note || '已导入');
      close();
      onDone?.();
    } catch (err) {
      toast(err.message, 'err');
    } finally {
      btn.disabled = false;
    }
  });
}

/* ================= 问一问 ================= */
export function renderAsk(root) {
  const history = [];
  root.innerHTML = `
    <div class="card">
      <h3>问一问 <span class="sub">事实只走工具，数字会做交叉校验</span></h3>
      <div class="notice info">
        问答会先调工具取实时数据再回答；回答里的金额/百分比对不上的，会被标成「未核对到」。
        经验库只回答「以前类似情况怎么判断、后来怎么走」，<b>价格永远走工具</b>。
      </div>
      <div class="chips" id="chips"></div>
      <div class="chat">
        <div class="chat-log" id="log"></div>
        <div class="chat-input">
          <textarea id="q" rows="1" placeholder="例如：大盘现在能不能加仓？我的持仓整体怎么样？"></textarea>
          <button class="primary" id="send">发送</button>
        </div>
      </div>
    </div>`;

  const log = root.querySelector('#log');
  const input = root.querySelector('#q');
  const send = root.querySelector('#send');
  const chips = root.querySelector('#chips');

  const EXAMPLES = ['现在大盘什么情况？', '我的持仓整体怎么样？', '为什么弱市不补仓？',
    '纪念品和普通版有什么区别？', '卖出和提现的手续费怎么算？', '模拟盘跑得怎么样？'];

  function addUser(text) {
    const el = document.createElement('div');
    el.className = 'msg user';
    el.textContent = text;
    log.appendChild(el);
    log.scrollTop = log.scrollHeight;
  }

  function addAI(html) {
    const el = document.createElement('div');
    el.className = 'msg ai';
    el.innerHTML = html;
    log.appendChild(el);
    log.scrollTop = log.scrollHeight;
    return el;
  }

  function renderAnswer(r) {
    let html = miniMarkdown(r.answer || r.error || '（无内容）');
    if (r.tools_used?.length) {
      html += `<div class="tools">🔧 ${escapeHtml(r.tools_used.join('、'))}</div>`;
    }
    if (r.unverified_numbers?.length) {
      html += `<div class="notice warn" style="margin:8px 0 0">⚠️ 这些数字没在数据源里核对到，别当真：${escapeHtml(r.unverified_numbers.join('、'))}</div>`;
    }
    if (r.fallback_note) {
      html += `<div class="notice info" style="margin:8px 0 0">${escapeHtml(r.fallback_note)}</div>`;
    }
    if (r.evidence?.length) {
      html += `<details><summary>查看依据（工具原始返回）</summary>${
        r.evidence.map((e) => `<pre>【${escapeHtml(e.tool)}】${escapeHtml(e.result)}</pre>`).join('')}</details>`;
    }
    const tokens = r.usage?.total_tokens ? `　tokens ${r.usage.total_tokens}` : '';
    const quota = r.quota_left !== undefined ? `　今日剩余 ${r.quota_left} 次` : '';
    html += `<div class="meta">${escapeHtml(r.time || '')}${tokens}${quota}</div>`;
    return html;
  }

  async function ask(text) {
    const question = text.trim();
    if (!question) return;
    addUser(question);
    input.value = '';
    input.style.height = 'auto';
    send.disabled = true;
    const wait = addAI('<span class="spin"></span> 正在取实时数据…');
    try {
      const result = await api.ask(question, history.slice(-8));
      wait.innerHTML = renderAnswer(result);
      history.push({ role: 'user', content: question });
      history.push({ role: 'assistant', content: (result.answer || '').slice(0, 1200) });
    } catch (err) {
      wait.innerHTML = `<span class="down">${escapeHtml(err.message)}</span>`;
    } finally {
      send.disabled = false;
      input.focus();
    }
  }

  EXAMPLES.forEach((ex) => {
    const chip = document.createElement('div');
    chip.className = 'chip';
    chip.textContent = ex;
    chip.addEventListener('click', () => ask(ex));
    chips.appendChild(chip);
  });
  send.addEventListener('click', () => ask(input.value));
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); ask(input.value); }
  });
  input.addEventListener('input', () => {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 120) + 'px';
  });

  addAI('你好。我能查实时行情、做技术分析、看你的持仓与模拟盘，也能翻经验库讲清「以前类似情况怎么判断、后来怎么走」。<br>问具体一点，我会先拉真实数据再回答。');
}

/* ================= 设置 ================= */
export async function renderSettings(root) {
  loading(root, '读取配置…');
  let config;
  let setup;
  try {
    [config, setup] = await Promise.all([api.config(), api.setup()]);
  } catch (err) {
    errorCard(root, err);
    return;
  }
  const readonly = !!config.readonly;
  const envKeys = new Set(config.env_keys || []);

  const groupCards = Object.entries(config.groups).map(([title, values], gi) => `
    <div class="card">
      <h3>${escapeHtml(title)}</h3>
      ${Object.entries(values).map(([key, info]) => {
        const disabled = readonly || info.env;
        const label = KEY_LABELS[key] || key.split('.')[1];
        let control;
        if (info.secret) {
          // 密钥框永远留空：掩码值只放在 placeholder 里展示。
          // 否则用户点"保存这一组"会把 ****abcd 提交上去，后端只能拒绝。
          control = `<input data-key="${key}" ${disabled ? 'disabled' : ''}
            placeholder="${info.set ? `已配置（${escapeHtml(info.value)}），留空表示不修改` : '尚未配置，粘贴真实密钥'}"
            value="" autocomplete="off">`;
        } else if (typeof info.value === 'boolean') {
          control = `<select data-key="${key}" ${disabled ? 'disabled' : ''}>
            <option value="true" ${info.value ? 'selected' : ''}>开启</option>
            <option value="false" ${info.value ? '' : 'selected'}>关闭</option></select>`;
        } else if (Array.isArray(info.value)) {
          control = `<input data-key="${key}" value="${info.value.join(',')}" ${disabled ? 'disabled' : ''}
            placeholder="逗号分隔，如 9,12,22">`;
        } else {
          control = `<input data-key="${key}" value="${escapeHtml(info.value ?? '')}" ${disabled ? 'disabled' : ''}>`;
        }
        return `<label class="field"><span>${escapeHtml(label)}
          ${info.env ? '<span class="pill blue">环境变量</span>' : ''}
          ${info.secret && !info.set ? '<span class="pill amber">未配置</span>' : ''}</span>
          ${control}</label>`;
      }).join('')}
      ${readonly ? '' : `<button class="primary" data-save-group="${gi}">保存这一组</button>`}
    </div>`).join('');

  root.innerHTML = `
    ${readonly ? '<div class="notice info">线上演示环境：<b>只读</b>。配置与持仓只能在本机修改。</div>' : ''}
    ${setup.missing?.length ? `<div class="notice warn">
      还没配置完：${setup.missing.map((k) => `「${escapeHtml(k)}」`).join('、')}。
      填完下面的向导就能跑 <code>python main.py run</code>。</div>`
      : '<div class="notice ok">配置齐全：数据源 Key、大模型 Key、持仓都有了。</div>'}

    <div class="card">
      <h3>首次配置向导 <span class="sub">三分钟填完，之后不再碰配置文件</span></h3>
      <div class="notice info">密钥只写到本机 <code>config/config.yaml</code>，不回显明文，也不进 git。
        也可以用环境变量 <code>STEAMDT_API_KEY</code> / <code>DEEPSEEK_API_KEY</code> / <code>WECOM_WEBHOOK</code>。</div>

      <label class="field"><span>① SteamDT 开放平台 Key</span>
        <input id="w-steamdt" placeholder="${setup.checks.steamdt_key ? '已配置，留空表示不修改' : '从 open.steamdt.com 获取'}"
          ${readonly || envKeys.has('steamdt.api_key') ? 'disabled' : ''}></label>

      <label class="field"><span>② DeepSeek API Key</span>
        <input id="w-llm" placeholder="${setup.checks.llm_key ? '已配置，留空表示不修改' : 'sk-…'}"
          ${readonly || envKeys.has('llm.api_key') ? 'disabled' : ''}></label>

      <label class="field"><span>③ 企业微信机器人 webhook（可选，不填就不推送）</span>
        <input id="w-webhook" placeholder="${setup.checks.webhook ? '已配置，留空表示不修改' : 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=…'}"
          ${readonly || envKeys.has('notify.wecom_webhook') ? 'disabled' : ''}></label>

      <div class="row">
        <label class="field grow"><span>④ 巡检时间点（逗号分隔）</span>
          <input id="w-times" value="9,10,11,12,13,14,15,16,17,18,19,20,21,22,23" ${readonly ? 'disabled' : ''}></label>
        <label class="field grow"><span>概览时间点</span>
          <input id="w-summary" value="12,22" ${readonly ? 'disabled' : ''}></label>
      </div>

      <label class="field"><span>⑤ 导入持仓（JSON 数组，可留空）</span>
        <textarea id="w-inventory" rows="6" ${readonly ? 'disabled' : ''}
          placeholder='[{"market_hash_name":"AK-47 | Redline (Field-Tested)","quantity":1,"buy_price":169,"unlocked":true}]'></textarea>
        <div class="tip">字段：market_hash_name / quantity / buy_price / buy_date / wear / lots / note / unlocked。
          之后可以在「持仓」页逐条改。</div></label>

      ${readonly ? '' : '<button class="primary" id="w-save">保存向导</button>'}
    </div>

    <div id="plan-card"></div>
    <div id="watch-card"></div>

    <div class="grid c2">${groupCards}</div>

    <div class="card">
      <h3>经验库 <span class="sub">RAG</span></h3>
      <div id="kb-stats" class="small muted">读取中…</div>
      <div class="row" style="margin-top:10px">
        <input id="kb-q" class="grow" placeholder="试检索，例如：纪念品 普通版 区别">
        <button id="kb-go">检索</button>
      </div>
      <div id="kb-result" style="margin-top:10px"></div>
    </div>`;

  // 报告开关 / 监控标的：只读模式也要能看（只是没有保存与增删按钮）
  renderPlanCard(root, config);
  renderWatchCard(root, readonly);

  if (readonly) return;
  // 保存向导
  root.querySelector('#w-save')?.addEventListener('click', async (ev) => {
    const btn = ev.currentTarget;
    const body = {
      'steamdt.api_key': root.querySelector('#w-steamdt').value.trim(),
      'llm.api_key': root.querySelector('#w-llm').value.trim(),
      'notify.wecom_webhook': root.querySelector('#w-webhook').value.trim(),
      'alerts.times': root.querySelector('#w-times').value,
      'alerts.summary_times': root.querySelector('#w-summary').value,
    };
    const invText = root.querySelector('#w-inventory').value.trim();
    if (invText) {
      try {
        body.inventory = JSON.parse(invText);
      } catch (e) {
        toast('持仓 JSON 解析失败：' + e.message, 'err');
        return;
      }
    }
    btn.disabled = true;
    try {
      const res = await api.setupApply(body);
      toast(`已保存：${res.written.length} 项配置${res.inventory_count ? `、${res.inventory_count} 条持仓` : ''}`);
      renderSettings(root);
    } catch (err) {
      toast(err.message, 'err');
    } finally {
      btn.disabled = false;
    }
  });

  // 分组保存
  root.querySelectorAll('[data-save-group]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const updates = {};
      const seen = new Set();
      root.querySelectorAll('[data-key]').forEach((input) => {
        const key = input.dataset.key;
        if (seen.has(key) || input.disabled) return;
        seen.add(key);
        const value = input.value.trim();
        if (value === '' || value.startsWith('*')) return;   // 空/掩码 = 不修改
        updates[key] = value;
      });
      if (!Object.keys(updates).length) { toast('这一组没有要保存的改动'); return; }
      btn.disabled = true;
      try {
        await api.configWrite(updates);
        toast('已保存到 config/config.yaml');
        renderSettings(root);
      } catch (err) {
        toast(err.message, 'err');
      } finally {
        btn.disabled = false;
      }
    });
  });

  // 经验库
  api.knowledgeStats().then((s) => {
    const byKind = Object.entries(s.by_kind || {}).map(([k, v]) => `${k} ${v}`).join(' · ');
    root.querySelector('#kb-stats').textContent =
      s.documents ? `索引里 ${s.documents} 篇文档（${byKind}）` :
        '还没有索引。运行 python tools/build_rag_index.py 生成。';
  }).catch(() => {});

  root.querySelector('#kb-go').addEventListener('click', async () => {
    const q = root.querySelector('#kb-q').value.trim();
    if (!q) return;
    const box = root.querySelector('#kb-result');
    box.innerHTML = '<span class="spin"></span>';
    try {
      const res = await api.knowledgeSearch(q, 5);
      if (!res.results?.length) {
        box.innerHTML = `<div class="empty">${escapeHtml(res.note || '没有命中')}</div>`;
        return;
      }
      box.innerHTML = res.results.map((r) => `
        <div class="kv" style="align-items:flex-start;flex-direction:column;gap:2px">
          <div><span class="pill gray">${escapeHtml(r.kind)}</span>
            <b>${escapeHtml(r.title)}</b>
            <span class="muted small">${escapeHtml(r.date || '')} · ${escapeHtml(r.source)}</span></div>
          <div class="small muted">${escapeHtml(r.snippet)}</div>
        </div>`).join('') + `<div class="notice info" style="margin-top:10px">${escapeHtml(res.note || '')}</div>`;
    } catch (err) {
      box.innerHTML = `<div class="notice err">${escapeHtml(err.message)}</div>`;
    }
  });
}

/* ================= 设置：报告与巡检开关 ================= */
const WEEKDAYS = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];

async function renderPlanCard(root, config) {
  const box = root.querySelector('#plan-card');
  if (!box) return;
  const readonly = !!config.readonly;
  const group = config.groups['报告与巡检（可自选开关）'] || {};
  const pick = (key, fallback) => (group[key] ? group[key].value : fallback);

  let plan = null;
  try {
    plan = await api.plan();
  } catch (e) {
    /* 计划接口失败不影响编辑，只是「下次」显示为空 */
  }
  const nextOf = (kind) => (plan?.plans || []).find((p) => p.kind === kind)?.next || '—';

  const toggle = (key, value) => `<select data-plan="${key}" ${readonly ? 'disabled' : ''}>
      <option value="true" ${value ? 'selected' : ''}>开</option>
      <option value="false" ${value ? '' : 'selected'}>关</option></select>`;

  const timeInput = (key, value) => `<input data-plan="${key}" value="${escapeHtml(value || '')}"
      placeholder="17:30" style="width:88px;display:inline-block" ${readonly ? 'disabled' : ''}>`;

  const daySelect = (key, value) => `<select data-plan="${key}" style="width:96px;display:inline-block"
      ${readonly ? 'disabled' : ''}>
      ${WEEKDAYS.map((w, i) => `<option value="${i}" ${Number(value) === i ? 'selected' : ''}>${w}</option>`).join('')}
    </select>`;

  box.innerHTML = `
    <div class="card">
      <h3>报告与巡检 <span class="sub">三个报告互不影响；关掉就不生成、不推送</span></h3>
      <div class="notice info">这里改的是「计划」，改完要让它生效：
        服务器执行 <code>python tools/install_cron.py --install</code>；
        本地直接 <code>python main.py schedule</code> 常驻即可（不用碰 crontab）。</div>
      <div class="table-wrap">
        <table><thead><tr>
          <th>报告</th><th>开关</th><th>时间</th><th>下次</th>
        </tr></thead><tbody>
          <tr>
            <td><b>日报</b><div class="small muted">持仓分析 + Word + 推送</div></td>
            <td>${toggle('reports.daily_enabled', pick('reports.daily_enabled', true))}</td>
            <td>${timeInput('reports.daily_time', pick('reports.daily_time', '17:30'))}</td>
            <td class="small muted">${escapeHtml(nextOf('daily'))}</td>
          </tr>
          <tr>
            <td><b>午报</b><div class="small muted">盘中概览 + 体检 Word</div></td>
            <td>${toggle('reports.noon_enabled', pick('reports.noon_enabled', true))}</td>
            <td>${timeInput('reports.noon_time', pick('reports.noon_time', '12:10'))}</td>
            <td class="small muted">${escapeHtml(nextOf('noon'))}</td>
          </tr>
          <tr>
            <td><b>周报</b><div class="small muted">复盘 + 运行健康</div></td>
            <td>${toggle('reports.weekly_enabled', pick('reports.weekly_enabled', true))}</td>
            <td>${daySelect('reports.weekly_day', pick('reports.weekly_day', 6))}
                ${timeInput('reports.weekly_time', pick('reports.weekly_time', '20:00'))}</td>
            <td class="small muted">${escapeHtml(nextOf('weekly'))}</td>
          </tr>
          <tr>
            <td><b>盘中巡检</b><div class="small muted">异动提醒（分级推送）</div></td>
            <td colspan="2">
              <label class="small muted" style="display:inline-block;margin-right:8px">间隔</label>
              <input data-plan="alerts.interval_minutes" type="number" min="15" max="1440" step="5"
                value="${escapeHtml(pick('alerts.interval_minutes', 60))}"
                style="width:92px;display:inline-block" ${readonly ? 'disabled' : ''}>
              <span class="small muted">分钟（≥15；批量取价接口 1 次/分钟，别设太密）</span>
            </td>
            <td class="small muted">${plan?.alerts?.enabled ? `每 ${plan.alerts.interval_minutes} 分钟` : '已关闭'}</td>
          </tr>
        </tbody></table>
      </div>
      ${readonly ? '' : '<div class="row" style="margin-top:12px"><button class="primary" id="plan-save">保存计划</button></div>'}
    </div>`;

  if (readonly) return;
  box.querySelector('#plan-save')?.addEventListener('click', async (ev) => {
    const btn = ev.currentTarget;
    const updates = {};
    box.querySelectorAll('[data-plan]').forEach((input) => {
      updates[input.dataset.plan] = input.value;
    });
    btn.disabled = true;
    try {
      await api.configWrite(updates);
      toast('计划已保存');
      renderSettings(root);          // 重新读配置，让「下次」与生效计划同步
    } catch (err) {
      toast(err.message, 'err');
    } finally {
      btn.disabled = false;
    }
  });
}

/* ================= 设置：监控标的 ================= */
async function renderWatchCard(root, readonly) {
  const box = root.querySelector('#watch-card');
  if (!box) return;
  let data;
  try {
    data = await api.watchlist();
  } catch (err) {
    box.innerHTML = `<div class="card"><div class="notice err">监控列表读取失败：${escapeHtml(err.message)}</div></div>`;
    return;
  }

  const rows = (data.items || []).map((it, i) => `
    <tr>
      <td><span class="name" title="${escapeHtml(it.market_hash_name)}">${escapeHtml(it.market_hash_name)}</span></td>
      <td class="small muted">${escapeHtml(it.note || '—')}</td>
      <td class="num">${readonly ? '' : `<button class="ghost" data-wdel="${i}">删除</button>`}</td>
    </tr>`).join('');

  box.innerHTML = `
    <div class="card">
      <h3>监控标的 <span class="sub">${data.count}/${data.max} · 持仓自动监控，这里加额外想盯的</span></h3>
      <div class="notice info">${escapeHtml(data.hint || '')}</div>
      ${data.items.length ? `
        <div class="table-wrap"><table><thead><tr>
          <th>饰品（Steam 官方英文名）</th><th>备注</th><th></th>
        </tr></thead><tbody>${rows}</tbody></table></div>`
        : '<div class="empty">还没有额外监控的标的。持仓里的饰品不需要加，已经自动监控。</div>'}
      ${readonly ? '' : `
        <div class="row" style="margin-top:12px">
          <input id="wl-name" class="grow" placeholder="AK-47 | Redline (Field-Tested)">
          <button class="primary" id="wl-add">添加</button>
        </div>
        <div class="small muted" style="margin-top:6px">
          名称要带磨损档，例如 <code>AK-47 | Redline (Field-Tested)</code>；
          写错会让系统按另一个物品报价（纪念品 / 暗金 / ★ 刀最容易写错）。
        </div>`}
    </div>`;

  if (readonly) return;
  const input = box.querySelector('#wl-name');

  const add = async () => {
    const name = (input.value || '').trim();
    if (!name) return;
    const btn = box.querySelector('#wl-add');
    btn.disabled = true;
    try {
      await api.addWatch(name);
      toast('已加入监控');
      renderWatchCard(root, readonly);
    } catch (err) {
      toast(err.message, 'err');
    } finally {
      btn.disabled = false;
    }
  };

  box.querySelector('#wl-add')?.addEventListener('click', add);
  input?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); add(); }
  });
  box.querySelectorAll('[data-wdel]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const name = data.items[Number(btn.dataset.wdel)]?.market_hash_name;
      if (!name || !confirm(`从监控列表移除？\n\n${name}`)) return;
      try {
        await api.delWatch(name);
        toast('已移除');
        renderWatchCard(root, readonly);
      } catch (err) {
        toast(err.message, 'err');
      }
    });
  });
}

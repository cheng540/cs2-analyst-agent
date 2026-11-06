/* 路由与外壳：hash 路由（#/dash 等），零依赖。 */

import { api } from './api.js';
import { renderAsk, renderDashboard, renderHoldings, renderSettings } from './pages.js';

const ROUTES = {
  dash: { title: '大盘', render: renderDashboard },
  holdings: { title: '持仓', render: renderHoldings },
  ask: { title: '问一问', render: renderAsk },
  settings: { title: '设置', render: renderSettings },
};

const view = document.getElementById('view');

function currentRoute() {
  const key = (location.hash || '#/dash').replace(/^#\/?/, '').split('?')[0] || 'dash';
  return ROUTES[key] ? key : 'dash';
}

function markNav(key) {
  document.querySelectorAll('.nav a').forEach((a) => {
    a.classList.toggle('active', a.dataset.route === key);
  });
}

async function route() {
  const key = currentRoute();
  markNav(key);
  document.title = `${ROUTES[key].title} · CS2 持仓助手`;
  view.innerHTML = '';
  try {
    await ROUTES[key].render(view);
  } catch (err) {
    view.innerHTML = `<div class="card"><div class="notice err">页面出错：${err.message}</div></div>`;
  }
}

async function boot() {
  const badge = document.getElementById('mode');
  try {
    const health = await api.health();
    if (health.readonly) {
      badge.textContent = '线上只读';
      badge.className = 'badge-mode ro';
      badge.title = '演示环境：不能改配置与持仓';
    } else {
      badge.textContent = `本地 · ${health.latest_analysis || '无数据'}`;
      badge.className = 'badge-mode';
      badge.title = health.stale_days > 1
        ? `数据已过期 ${health.stale_days} 天，云端流水线可能没跑`
        : '数据是新的';
      if (health.stale_days > 1) badge.classList.add('ro');
    }
    if (!health.has_steamdt_key || !health.has_llm_key) {
      const warn = document.createElement('div');
      warn.className = 'notice warn';
      warn.innerHTML = `还没配置 ${!health.has_steamdt_key ? 'SteamDT Key' : ''}${!health.has_steamdt_key && !health.has_llm_key ? ' 和 ' : ''}${!health.has_llm_key ? 'DeepSeek Key' : ''}，
        去 <a href="#/settings">设置</a> 里填一下。`;
      view.prepend(warn);
    }
  } catch (err) {
    badge.textContent = '服务未响应';
    badge.className = 'badge-mode ro';
  }
  await route();
}

window.addEventListener('hashchange', route);
boot();

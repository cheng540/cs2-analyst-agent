#!/usr/bin/env python3
"""报告浏览服务（只读，带 HTTP Basic 认证）。

用途：手机/电脑浏览器打开即可查看每日 Word 报告、当日简报与情报。
只用 Python 标准库，独立进程，不依赖 nginx，不影响服务器上其他服务。

配置：config/report_server.json
    {"port": 8899, "username": "cs2", "password": "xxxx", "root": "/root/cs2-analyst"}
启动：venv/bin/python tools/report_server.py
"""
from __future__ import annotations

import base64
import html
import json
import mimetypes
import os
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

# 允许导入项目包（本脚本位于 tools/ 下）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CONF_PATH = Path(__file__).resolve().parent.parent / "config" / "report_server.json"

DEFAULT_CONF = {"port": 8899, "username": "cs2", "password": "change-me",
                "root": str(Path(__file__).resolve().parent.parent),
                # readonly=true 时所有写接口返回 403（线上 demo 用）
                "readonly": False,
                # local_no_auth=true 时，本机 127.0.0.1 访问免 Basic 认证（自用控制台更顺手）
                "local_no_auth": False}


def load_conf() -> dict:
    conf = dict(DEFAULT_CONF)
    if CONF_PATH.exists():
        try:
            # utf-8-sig：兼容带 BOM 的配置文件
            conf.update(json.loads(CONF_PATH.read_text(encoding="utf-8-sig")))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"配置读取失败，使用默认值: {exc}")
    return conf


CONF = load_conf()
# 根目录解析顺序：环境变量 CS2_ROOT → 配置里的 root → 项目目录。
# 为什么要有兜底：这份配置常从服务器原样拷到本地，里面的 root 是 /root/cs2-analyst，
# 本地打开会指向一个不存在的目录，页面全是"暂无数据"，很容易被误判成程序坏了。
_ENV_ROOT = os.environ.get("CS2_ROOT", "").strip()
ROOT = Path(_ENV_ROOT or CONF["root"]).resolve()
if not ROOT.exists():
    _fallback = Path(__file__).resolve().parent.parent
    print(f"[提示] 配置的 root 不存在（{ROOT}），已改用项目目录 {_fallback}")
    ROOT = _fallback


def _resolve_dir(env_name: str, default_rel: str) -> Path:
    """数据/报告目录可用环境变量覆盖，用于「只想看演示数据」的场景：
    CS2_DATA_DIR=data/demo_run CS2_REPORT_DIR=data/demo_run/reports python tools/report_server.py
    """
    raw = os.environ.get(env_name, "").strip() or default_rel
    path = Path(raw)
    return (path if path.is_absolute() else ROOT / path).resolve()


DATA_DIR = _resolve_dir("CS2_DATA_DIR", "data")
REPORT_DIR = _resolve_dir("CS2_REPORT_DIR", "reports")
STATIC_DIR = (ROOT / "cs2analyst" / "web" / "static").resolve()

# 控制台 API（读写权限在 ConsoleApi 内部判定：写接口只对 127.0.0.1 开放）
from cs2analyst.web.api import ConsoleApi  # noqa: E402

API = ConsoleApi(ROOT, readonly=bool(CONF.get("readonly", False)),
                 data_dir=DATA_DIR, report_dir=REPORT_DIR)

PAGE_CSS = """
<style>
 body{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;margin:0;padding:16px;
      background:#f5f6f8;color:#222;line-height:1.7}
 h1{font-size:19px;margin:0 0 4px}
 .sub{color:#888;font-size:12px;margin-bottom:14px}
 .card{background:#fff;border-radius:10px;padding:12px 14px;margin-bottom:12px;
       box-shadow:0 1px 3px rgba(0,0,0,.06)}
 a{color:#1a6ee0;text-decoration:none}
 .item{padding:9px 0;border-bottom:1px solid #eee;font-size:14px}
 .item:last-child{border-bottom:none}
 .meta{color:#999;font-size:12px}
 pre{white-space:pre-wrap;word-break:break-word;font-size:13px;
     font-family:ui-monospace,Menlo,Consolas,monospace}
 .tag{display:inline-block;background:#eef3ff;color:#1a6ee0;border-radius:5px;
      padding:1px 6px;font-size:11px;margin-left:6px}
</style>
"""


def _auth_ok(header: str | None) -> bool:
    if not header or not header.startswith("Basic "):
        return False
    try:
        raw = base64.b64decode(header[6:]).decode("utf-8")
        user, _, pwd = raw.partition(":")
    except Exception:
        return False
    return user == CONF["username"] and pwd == CONF["password"]


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


_ASSISTANT = None  # 问答助手（懒加载，进程内复用缓存）

# ------------------------------------------------------------
# 代码热更新检测
#
# 为什么需要：本服务是常驻进程，Python 不会自动重载已导入的模块。
# 改完代码如果不重启服务，它会一直用内存里的旧代码——
# 2026-09-25 实际踩到：平台费率从「2.5%」改成「卖出 1% + 提现 1%」后，
# 网页问答仍然回答「每笔约 2.5% 手续费」，因为进程是两天前启动的。
#
# 做法：每次问答请求前比对被依赖文件的 mtime 指纹；一旦变化，
# 先回一个明确提示，再退出进程，由 systemd（Restart=always）用新代码拉起（约 5 秒）。
# ------------------------------------------------------------
_WATCHED = (
    "cs2analyst/rules.py",
    "cs2analyst/chat/assistant.py",
    "cs2analyst/chat/tools.py",
    "cs2analyst/llm/client.py",
    "cs2analyst/config.py",
    "cs2analyst/web/api.py",
    "cs2analyst/web/store.py",
    "cs2analyst/rag/store.py",
    "cs2analyst/rag/corpus.py",
    "config/config.yaml",
)


def _fingerprint() -> str:
    parts = []
    for rel in _WATCHED:
        try:
            parts.append(f"{rel}:{(ROOT / rel).stat().st_mtime_ns}")
        except OSError:
            parts.append(f"{rel}:-")
    return "|".join(parts)


_START_FINGERPRINT = _fingerprint()


CHAT_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>问一问 · CS2 持仓分析</title>
<style>
 *{box-sizing:border-box}
 body{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;margin:0;
      background:#eef0f4;color:#1c1c1e;display:flex;flex-direction:column;height:100vh}
 header{padding:12px 14px;background:#fff;border-bottom:1px solid #e2e5ea;
        display:flex;justify-content:space-between;align-items:center;flex-shrink:0}
 header b{font-size:15px}
 header a{font-size:12px;color:#1a6ee0;text-decoration:none;margin-left:10px}
 #list{flex:1;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:10px}
 .msg{max-width:88%;padding:10px 13px;border-radius:12px;font-size:14px;line-height:1.65;
      white-space:pre-wrap;word-break:break-word}
 .user{align-self:flex-end;background:#1a6ee0;color:#fff;border-bottom-right-radius:4px}
 .ai{align-self:flex-start;background:#fff;border-bottom-left-radius:4px;
     box-shadow:0 1px 3px rgba(0,0,0,.07)}
 .ai b{color:#0b3d91}
 .meta{font-size:11px;color:#8b8f98;margin-top:6px}
 .warn{background:#fff6e5;border-left:3px solid #f0a020;padding:6px 8px;border-radius:4px;
       font-size:11.5px;color:#8a5a00;margin-top:8px}
 .tools{font-size:11.5px;color:#4a6fa5;margin-top:6px}
 details{margin-top:6px;font-size:11.5px;color:#6b7280}
 details pre{background:#f7f8fa;border-radius:6px;padding:8px;overflow-x:auto;
             white-space:pre-wrap;word-break:break-word;font-size:11px;max-height:240px}
 .chips{display:flex;gap:6px;flex-wrap:wrap;padding:0 12px 6px}
 .chip{background:#fff;border:1px solid #d9dee6;border-radius:14px;padding:5px 11px;
       font-size:12px;color:#3b4a5f;cursor:pointer}
 footer{padding:9px 12px 14px;background:#fff;border-top:1px solid #e2e5ea;flex-shrink:0}
 .row{display:flex;gap:8px;align-items:flex-end}
 textarea{flex:1;resize:none;border:1px solid #d9dee6;border-radius:10px;padding:10px;
          font-size:14px;font-family:inherit;max-height:110px;min-height:42px}
 button{background:#1a6ee0;color:#fff;border:none;border-radius:10px;padding:11px 16px;
        font-size:14px;font-weight:600}
 button:disabled{background:#b9c4d4}
 .loading{align-self:flex-start;color:#6b7280;font-size:13px;padding:6px 4px}
</style></head><body>
<header>
  <b>💬 问一问（CS2 持仓分析）</b>
  <span><a href="/app/">控制台</a><a href="/">报告</a><a href="/brief">简报</a><a href="/sim">模拟盘</a></span>
</header>
<div id="list"></div>
<div class="chips" id="chips"></div>
<footer>
  <div class="row">
    <textarea id="q" rows="1" placeholder="问点什么…例如：AWP 二西莫夫现在值得买吗？"></textarea>
    <button id="send">发送</button>
  </div>
  <div class="meta" id="hint">回答基于实时行情与系统指标计算；每个数字都可展开核对来源。</div>
</footer>
<script>
const list=document.getElementById('list'),q=document.getElementById('q'),
      send=document.getElementById('send'),chips=document.getElementById('chips');
const history=[];
const EXAMPLES=['现在大盘什么情况？','我的持仓整体怎么样？','AWP 二西莫夫现在值得买吗？',
                '模拟盘跑得怎么样？','今天的更新对行情有什么影响？','帮我看看蝴蝶刀多普勒的价格'];
function addUser(t){const d=document.createElement('div');d.className='msg user';d.textContent=t;
  list.appendChild(d);list.scrollTop=list.scrollHeight;}
function addAI(html){const d=document.createElement('div');d.className='msg ai';d.innerHTML=html;
  list.appendChild(d);list.scrollTop=list.scrollHeight;return d;}
function esc(s){return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function render(r){
  let h=esc(r.answer||r.error||'（无内容）');
  h=h.replace(/\\*\\*(.+?)\\*\\*/g,'<b>$1</b>');
  if(r.tools_used&&r.tools_used.length){h+='<div class="tools">🔧 调用工具：'+esc(r.tools_used.join('、'))+'</div>';}
  if(r.unverified_numbers&&r.unverified_numbers.length){
    h+='<div class="warn">⚠️ 以下数字未能在数据源中核对到，请谨慎参考：'+esc(r.unverified_numbers.join('、'))+'</div>';}
  if(r.evidence&&r.evidence.length){
    h+='<details><summary>查看依据（工具返回的原始数据）</summary>';
    r.evidence.forEach(e=>{h+='<pre>【'+esc(e.tool)+'】'+esc(e.result)+'</pre>';});
    h+='</details>';}
  const u=r.usage&&r.usage.total_tokens?('　tokens '+r.usage.total_tokens):'';
  h+='<div class="meta">'+esc(r.time||'')+u+(r.quota_left!==undefined?('　今日剩余 '+r.quota_left+' 次'):'')+'</div>';
  return h;
}
async function ask(text){
  const t=text.trim();if(!t)return;
  addUser(t);q.value='';q.style.height='auto';
  send.disabled=true;
  const load=document.createElement('div');load.className='loading';load.textContent='🔍 正在查询实时数据…';
  list.appendChild(load);list.scrollTop=list.scrollHeight;
  try{
    const resp=await fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({message:t,history:history.slice(-8)})});
    const r=await resp.json();
    load.remove();
    addAI(render(r));
    history.push({role:'user',content:t});
    history.push({role:'assistant',content:(r.answer||'').slice(0,1200)});
  }catch(e){
    load.remove();addAI('<span style="color:#c00">请求失败：'+esc(String(e))+'</span>');
  }finally{send.disabled=false;q.focus();}
}
EXAMPLES.forEach(x=>{const b=document.createElement('div');b.className='chip';b.textContent=x;
  b.onclick=()=>ask(x);chips.appendChild(b);});
send.onclick=()=>ask(q.value);
q.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();ask(q.value);}});
q.addEventListener('input',()=>{q.style.height='auto';q.style.height=Math.min(q.scrollHeight,110)+'px';});
addAI('你好，我可以查实时行情、做技术分析、看你的持仓和模拟盘。<br>问我具体点的问题，我会先拉真实数据再回答。');
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "cs2-reports/1.0"

    def log_message(self, fmt, *args):  # 精简日志
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {self.address_string()} {fmt % args}")

    # ---------- 响应工具 ----------
    def _client_ip(self) -> str:
        host = (self.client_address[0] if self.client_address else "") or ""
        if host.startswith("::ffff:"):
            host = host[len("::ffff:"):]
        return host

    def _authed(self) -> bool:
        """Basic 认证；`local_no_auth` 打开时允许本机免密（自用控制台）。"""
        if _auth_ok(self.headers.get("Authorization")):
            return True
        if CONF.get("local_no_auth"):
            return self._client_ip() in ("127.0.0.1", "::1")
        return False

    def _deny(self, json_mode: bool = False):
        if json_mode:
            return self._json({"error": "需要登录（HTTP Basic 认证）"}, 401)
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="CS2 Reports"')
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write("<h3>需要登录</h3>".encode("utf-8"))

    def _json(self, payload: dict, code: int = 200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(body, "application/json; charset=utf-8", code,
                   {"Cache-Control": "no-store"})

    def _send(self, body: bytes, ctype: str, code: int = 200, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _body_json(self) -> dict:
        """读 POST body；解析失败抛 ValueError，由调用方转 400。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        if length > 16 * 1024 * 1024:
            # 16MB：识图导入要传 base64 图片（单图 6MB → base64 约 8MB），
            # 旧的 2MB 上限会把截图请求直接拒掉。
            raise ValueError("请求体过大")
        raw = self.rfile.read(length)
        data = json.loads(raw or b"{}")
        return data if isinstance(data, dict) else {}

    def _api(self, method: str, path: str, query: dict, body: dict | None):
        status, payload = API.dispatch(method, path, query, body, self._client_ip())
        return self._json(payload, status)

    def _static(self, rel: str):
        rel = (rel or "index.html").split("?")[0]
        if rel in ("", "/"):
            rel = "index.html"
        target = (STATIC_DIR / rel).resolve()
        # 防目录穿越：必须落在静态目录内
        if not str(target).startswith(str(STATIC_DIR)) or not target.is_file():
            return self._page("404", "<div class='card'>静态文件不存在</div>")
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if target.suffix == ".js":
            ctype = "text/javascript; charset=utf-8"
        elif target.suffix == ".css":
            ctype = "text/css; charset=utf-8"
        elif target.suffix == ".html":
            ctype = "text/html; charset=utf-8"
        self._send(target.read_bytes(), ctype, extra={"Cache-Control": "no-cache"})

    def _page(self, title: str, inner: str):
        body = (f"<!doctype html><html lang='zh'><head><meta charset='utf-8'>"
                f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
                f"<title>{html.escape(title)}</title>{PAGE_CSS}</head><body>"
                f"<h1>{html.escape(title)}</h1>"
                f"<div class='sub'>CS2 持仓分析 · {datetime.now():%Y-%m-%d %H:%M}</div>"
                f"{inner}"
                f"<div class='sub' style='margin-top:18px'>"
                f"<a href='/app/'>🖥 控制台</a> · <a href='/chat'>💬 问一问</a> · "
                f"<a href='/'>报告列表</a> · "
                f"<a href='/brief'>今日简报</a> · <a href='/sim'>模拟盘</a></div></body></html>")
        self._send(body.encode("utf-8"), "text/html; charset=utf-8")

    # ---------- 路由 ----------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        json_mode = path.startswith("/api/")
        if not self._authed():
            return self._deny(json_mode)

        if json_mode:
            return self._api("GET", path, query, None)
        if path == "/app":                     # 补斜杠，否则相对路径的 js/css 会 404
            return self._send(b"", "text/html; charset=utf-8", 302, {"Location": "/app/"})
        if path.startswith("/app/"):
            return self._static(path[len("/app/"):])

        if path in ("/", "/index.html"):
            return self._reports()
        if path == "/brief":
            return self._brief()
        if path == "/sim":
            return self._sim()
        if path == "/chat":
            return self._chat_page()
        if path.startswith("/file/"):
            return self._file(path[len("/file/"):])
        return self._page("404", "<div class='card'>页面不存在</div>")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        json_mode = path.startswith("/api/")
        if not self._authed():
            return self._deny(json_mode)
        if json_mode:
            try:
                body = self._body_json()
            except (ValueError, json.JSONDecodeError):
                return self._json({"error": "请求体不是合法 JSON"}, 400)
            query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            return self._api("POST", path, query, body)
        if path == "/chat":
            return self._chat_api()
        return self._page("404", "<div class='card'>接口不存在</div>")

    def do_PUT(self):
        self._write_api("PUT")

    def do_DELETE(self):
        self._write_api("DELETE")

    def _write_api(self, method: str):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if not path.startswith("/api/"):
            return self._page("404", "<div class='card'>接口不存在</div>")
        if not self._authed():
            return self._deny(True)
        try:
            body = self._body_json()
        except (ValueError, json.JSONDecodeError):
            return self._json({"error": "请求体不是合法 JSON"}, 400)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        return self._api(method, path, query, body)

    # ---------- 问答 ----------
    def _chat_api(self):
        global _ASSISTANT
        # 代码/配置有更新 → 让 systemd 用新代码重启本进程，避免继续用旧提示词回答
        if _fingerprint() != _START_FINGERPRINT:
            print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 检测到代码或配置已更新，"
                  "退出进程以便以新代码重启（约 5 秒后恢复）")
            try:
                self._send(json.dumps(
                    {"error": "服务正在更新配置，请 5 秒后重新提问"},
                    ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8", 503)
            except Exception:
                pass
            import os
            os._exit(0)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._send(json.dumps({"error": "请求格式错误"}, ensure_ascii=False)
                              .encode("utf-8"), "application/json; charset=utf-8", 400)
        question = str(body.get("message") or "").strip()
        history = body.get("history") or []
        if not question:
            return self._send(json.dumps({"error": "问题为空"}, ensure_ascii=False)
                              .encode("utf-8"), "application/json; charset=utf-8", 400)
        if _ASSISTANT is None:
            try:
                from cs2analyst.chat.assistant import ChatAssistant
                from cs2analyst.config import load_config
                _ASSISTANT = ChatAssistant(load_config())
                print("问答助手已初始化")
            except Exception as exc:
                return self._send(json.dumps({"error": f"助手初始化失败：{exc}"},
                                             ensure_ascii=False).encode("utf-8"),
                                  "application/json; charset=utf-8", 500)
        try:
            result = _ASSISTANT.ask(question, history)
            payload = {k: v for k, v in result.items() if k != "evidence"}
            payload["evidence"] = [
                {"tool": t["tool"], "args": t.get("args"),
                 "result": json.dumps(t["result"], ensure_ascii=False)[:2500]}
                for t in (result.get("evidence") or [])]
            payload["quota_left"] = _ASSISTANT.quota_left()
            return self._send(json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                              "application/json; charset=utf-8")
        except Exception as exc:
            return self._send(json.dumps({"error": f"问答失败：{exc}"}, ensure_ascii=False)
                              .encode("utf-8"), "application/json; charset=utf-8", 500)

    def _chat_page(self):
        body = CHAT_HTML
        self._send(body.encode("utf-8"), "text/html; charset=utf-8")

    # ---------- 页面 ----------
    def _reports(self):
        items = []
        if REPORT_DIR.exists():
            files = sorted(REPORT_DIR.glob("*.docx"),
                           key=lambda p: p.stat().st_mtime, reverse=True)[:40]
            for p in files:
                st = p.stat()
                items.append(
                    f"<div class='item'><a href='/file/reports/{html.escape(p.name)}'>"
                    f"{html.escape(p.name)}</a>"
                    f"<span class='tag'>{_fmt_size(st.st_size)}</span>"
                    f"<div class='meta'>{datetime.fromtimestamp(st.st_mtime):%Y-%m-%d %H:%M}</div></div>")
        inner = "<div class='card'><b>每日报告（Word）</b><div class='meta'>点击可直接下载/打开</div>" \
                + ("".join(items) or "<div class='item'>暂无报告</div>") + "</div>"
        self._page("CS2 持仓分析 · 报告", inner)

    def _brief(self):
        text = "暂无简报"
        files = sorted(DATA_DIR.glob("brief_*.md")) if DATA_DIR.exists() else []
        if files:
            try:
                text = files[-1].read_text(encoding="utf-8")
            except OSError as exc:
                text = f"读取失败: {exc}"
        inner = f"<div class='card'><pre>{html.escape(text)}</pre></div>"
        self._page("今日简报", inner)

    def _sim(self):
        rows = []
        for label, fname in (("A 轨（纯规则）", "state_a.json"), ("B 轨（规则+AI）", "state_b.json")):
            p = DATA_DIR / "sim" / fname
            if not p.exists():
                rows.append(f"<div class='item'>{label}：暂无数据</div>")
                continue
            try:
                st = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                rows.append(f"<div class='item'>{label}：读取失败 {html.escape(str(exc))}</div>")
                continue
            journal = st.get("journal") or []
            init = float(st.get("initial_capital") or 0)
            cash = float(st.get("cash") or 0)
            net = float(journal[-1].get("net_value")) if journal else cash
            ret = (net / init - 1) * 100 if init else 0
            color = "#0a8f3c" if ret >= 0 else "#d0021b"
            rows.append(
                f"<div class='item'><b>{label}</b>"
                f"<div>净值 ¥{net:,.2f} <span style='color:{color}'>({ret:+.2f}%)</span>"
                f" · 现金 ¥{cash:,.2f} · 运行 {len(journal)} 天</div>")
            for name, pos in (st.get("positions") or {}).items():
                rows.append(f"<div class='meta'>　持仓：{html.escape(name)} ×{pos.get('qty')}"
                            f"（成本 ¥{pos.get('avg_cost')}，锁定期至 {pos.get('locked_until') or '—'}）</div>")
            if journal:
                rows.append(f"<div class='meta'>　最近手记：{html.escape(str(journal[-1].get('thoughts'))[:120])}</div>")
            rows.append("</div>")
        self._page("模拟盘（双轨）", "<div class='card'>" + "".join(rows) + "</div>")

    def _file(self, rel: str):
        target = (ROOT / rel).resolve()
        # 防目录穿越：必须位于项目目录内，且只允许 reports/ 与 data/ 下的文件
        if not str(target).startswith(str(ROOT)) or not target.exists() or not target.is_file():
            return self._page("404", "<div class='card'>文件不存在</div>")
        allowed = (REPORT_DIR.resolve(), DATA_DIR.resolve())
        if not any(str(target).startswith(str(a)) for a in allowed):
            return self._page("403", "<div class='card'>仅允许访问报告与数据目录</div>")
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        extra = {}
        if target.suffix.lower() == ".docx":
            # HTTP 头只能是 latin-1，中文文件名必须按 RFC 5987 转义，否则连接会被断开
            ascii_fallback = target.name.encode("ascii", "ignore").decode("ascii") or "report.docx"
            extra["Content-Disposition"] = (
                f"attachment; filename=\"{ascii_fallback}\"; "
                f"filename*=UTF-8''{quote(target.name)}")
        self._send(target.read_bytes(), ctype, extra=extra)


def main():
    port = int(CONF.get("port", 8899))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"报告服务已启动：http://0.0.0.0:{port}  用户 {CONF['username']}  根目录 {ROOT}")
    print(f"  控制台：http://127.0.0.1:{port}/app/    "
          f"只读模式：{'是' if CONF.get('readonly') else '否'}   "
          f"本机免认证：{'是' if CONF.get('local_no_auth') else '否'}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()

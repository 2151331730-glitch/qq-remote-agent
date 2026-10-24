# -*- coding: utf-8 -*-
"""本地任务看板 + 网页入口 + Prometheus 指标（只监听 127.0.0.1）。

接口：
- GET  /              看板页面
- GET  /api/state     运行状态（含 p50/p95、成功率、近 7 天）
- GET  /api/tasks     任务列表（SQLite 里最近 N 条）
- GET  /api/events    审计事件（audit.jsonl 尾部）
- GET  /metrics       Prometheus 文本格式
- POST /api/task      提交任务，需请求头 X-Panel-Token

线程模型：HTTP 线程里只读状态；提交任务时通过 run_coroutine_threadsafe 回到事件循环，
所以队列/白名单逻辑和 QQ 入口完全一致。
"""
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HTML = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>QQ Agent 控制台</title>
<style>
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:#0d1117;color:#e6edf3;font:14px/1.55 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:1040px;margin:0 auto;padding:20px}
h1{font-size:17px;margin:0 0 4px;font-weight:600;letter-spacing:.3px}
.sub{color:#7d8590;font-size:12px;margin-bottom:16px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(132px,1fr));gap:8px;margin-bottom:16px}
.card{background:#161b22;border:1px solid #21262d;border-radius:10px;padding:10px 12px}
.card b{display:block;font-size:11px;color:#7d8590;font-weight:500;margin-bottom:3px;letter-spacing:.4px}
.card span{font-size:15px;word-break:break-all}
.card em{font-style:normal;color:#7d8590;font-size:11px;margin-left:4px}
form{display:flex;gap:8px;margin:16px 0;flex-wrap:wrap}
textarea{flex:1;min-width:260px;min-height:56px;background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:8px;padding:9px;resize:vertical;font:inherit}
input{background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:8px;padding:9px;width:190px;font:inherit}
button{background:#238636;color:#fff;border:0;border-radius:8px;padding:9px 16px;cursor:pointer;font:inherit}
button.ghost{background:#21262d}
#msg{min-height:20px;color:#7d8590;margin-bottom:10px;font-size:13px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid #21262d;vertical-align:top}
th{color:#7d8590;font-weight:500;font-size:12px}
.ok{color:#3fb950}.bad{color:#f85149}.warn{color:#d29922}
.tag{display:inline-block;padding:0 6px;border-radius:6px;background:#21262d;color:#8b949e;font-size:11px}
pre{white-space:pre-wrap;word-break:break-word;margin:0;color:#c9d1d9;font-size:12px;max-height:90px;overflow:auto}
.bars{display:flex;align-items:flex-end;gap:6px;height:56px;margin:4px 0 18px}
.bar{flex:1;background:#1f6feb;border-radius:3px 3px 0 0;min-height:2px;position:relative}
.bar i{position:absolute;bottom:-16px;left:0;right:0;text-align:center;font-size:10px;color:#7d8590;font-style:normal}
</style></head><body><div class="wrap">
<h1>QQ Agent 控制台 <span class="tag" id="panel"></span></h1>
<div class="sub">同一套队列 · 同一套白名单 · 危险操作仍需在 QQ 里 /确认</div>
<div class="grid" id="cards"></div>
<div class="bars" id="bars"></div>
<form onsubmit="submitTask(event)">
<textarea id="task" placeholder="输入任务，和手机 QQ 发的内容等价（危险操作仍需在 QQ 里 /确认）"></textarea>
<input id="token" placeholder="面板 token" autocomplete="off">
<button type="submit">提交任务</button>
<button type="button" class="ghost" onclick="load()">刷新</button>
</form>
<div id="msg"></div>
<table><thead><tr><th>时间</th><th>ID</th><th>来源</th><th>状态</th><th>耗时</th><th>花费</th><th>内容</th></tr></thead>
<tbody id="rows"></tbody></table>
</div>
<script>
const $ = s => document.querySelector(s);
$('#token').value = localStorage.getItem('panel_token') || '';
const esc = s => (s ?? '').toString().replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const ms = v => !v ? '-' : v < 1000 ? v + 'ms' : v < 60000 ? (v/1000).toFixed(1) + 's' : (v/60000).toFixed(1) + 'min';
const ICON = {succeeded:'✅',failed:'❌',cancelled:'🛑',interrupted:'⚠️',running:'▶️',queued:'⏳'};
const CLS = {succeeded:'ok',failed:'bad',cancelled:'warn',interrupted:'warn',running:'',queued:''};
async function load(){
  try{
    const st = await (await fetch('/api/state')).json();
    $('#panel').textContent = st.panel || 'localhost';
    const cards = [
      ['当前任务', st.current || '空闲'],
      ['排队', (st.queue ?? 0) + ' / 10'],
      ['完成 · 失败', (st.done ?? 0) + ' · ' + (st.failed ?? 0)],
      ['24h 成功率', (st.success_rate ?? 0).toFixed(0) + '%'],
      ['时延 p50', ms(st.p50_ms)],
      ['时延 p95', ms(st.p95_ms)],
      ['累计花费', '$' + (st.cost ?? 0).toFixed(3)],
      ['在线', (st.uptime_hours ?? 0).toFixed(1) + 'h'],
      ['worker', st.workers ?? 1],
      ['工作目录', st.cwd || ''],
    ];
    $('#cards').innerHTML = cards.map(([k,v])=>`<div class="card"><b>${esc(k)}</b><span>${esc(v)}</span></div>`).join('');
    const daily = st.daily || [];
    const max = Math.max(1, ...daily.map(d => d.n));
    $('#bars').innerHTML = daily.map(d =>
      `<div class="bar" style="height:${Math.round(d.n/max*100)}%" title="${d.d}: ${d.n} 个，成功 ${d.ok}，平均 ${ms(Math.round(d.avg_ms))}"><i>${esc((d.d||'').slice(5))}</i></div>`).join('')
      || '<div class="sub">还没有历史数据</div>';
    const t = await (await fetch('/api/tasks')).json();
    $('#rows').innerHTML = (t.tasks||[]).map(r => {
      const ts = r.created_at ? new Date(r.created_at*1000).toLocaleString('zh-CN',{hour12:false}).slice(5) : '';
      return `<tr><td>${esc(ts)}</td><td><span class="tag">${esc(r.id)}</span></td><td>${esc(r.source)}</td>
        <td class="${CLS[r.state]||''}">${ICON[r.state]||''} ${esc(r.state)}</td>
        <td>${ms(r.duration_ms)}</td><td>$${(r.cost_usd||0).toFixed(3)}</td>
        <td><pre>${esc((r.result || r.text || '').slice(0,300))}</pre></td></tr>`;
    }).join('') || '<tr><td colspan="7" style="color:#7d8590">暂无任务</td></tr>';
  }catch(err){ $('#msg').textContent = '读取失败：' + err; }
}
async function submitTask(ev){
  ev.preventDefault();
  const text = $('#task').value.trim(); if(!text) return;
  const token = $('#token').value.trim(); localStorage.setItem('panel_token', token);
  $('#msg').textContent = '提交中…';
  try{
    const r = await fetch('/api/task', {method:'POST', headers:{'Content-Type':'application/json','X-Panel-Token':token}, body:JSON.stringify({text})});
    const d = await r.json();
    $('#msg').textContent = r.ok ? ('已提交：' + JSON.stringify(d)) : ('失败：' + (d.error || r.status));
    if(r.ok && d.status === 'queued'){ $('#task').value=''; }
  }catch(err){ $('#msg').textContent = '提交失败：' + err; }
  load();
}
load(); setInterval(load, 3000);
</script></body></html>
"""


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        p = self.server.panel
        try:
            if self.path in ("/", "/index.html"):
                self._send(200, HTML, "text/html; charset=utf-8")
            elif self.path.startswith("/api/state"):
                self._send(200, json.dumps(p.state_fn(), ensure_ascii=False))
            elif self.path.startswith("/api/tasks"):
                self._send(200, json.dumps({"tasks": p.tasks_fn()}, ensure_ascii=False))
            elif self.path.startswith("/api/events"):
                self._send(200, json.dumps({"events": p.events_fn()}, ensure_ascii=False))
            elif self.path.startswith("/metrics"):
                self._send(200, p.metrics_fn(), "text/plain; version=0.0.4; charset=utf-8")
            else:
                self._send(404, '{"error":"not found"}')
        except Exception as e:
            self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False))

    def do_POST(self):
        p = self.server.panel
        if not self.path.startswith("/api/task"):
            return self._send(404, '{"error":"not found"}')
        if self.headers.get("X-Panel-Token", "") != p.token:
            return self._send(403, '{"error":"bad token"}')
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception:
            return self._send(400, '{"error":"bad json"}')
        text = (body.get("text") or "").strip()
        if not text:
            return self._send(400, '{"error":"empty"}')
        try:
            res = p.submit_sync(text)
        except Exception as e:
            return self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False))
        self._send(200, json.dumps(res, ensure_ascii=False))


class Panel:
    def __init__(self, port, token, loop, state_fn, tasks_fn, submit_coro,
                 events_fn=None, metrics_fn=None):
        self.port = int(port)
        self.token = token or ""
        self.loop = loop
        self.state_fn = state_fn
        self.tasks_fn = tasks_fn
        self.events_fn = events_fn or (lambda: [])
        self.metrics_fn = metrics_fn or (lambda: "")
        self.submit_coro = submit_coro
        self.url = ""

    def start(self):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", self.port), _Handler)
        except OSError as e:
            return f"面板启动失败（端口 {self.port}）：{e}"
        srv.daemon_threads = True
        srv.panel = self
        threading.Thread(target=srv.serve_forever, name="panel", daemon=True).start()
        self.url = f"http://127.0.0.1:{self.port}/"
        return self.url

    def submit_sync(self, text):
        fut = asyncio.run_coroutine_threadsafe(self.submit_coro(text, "web"), self.loop)
        return fut.result(timeout=15)

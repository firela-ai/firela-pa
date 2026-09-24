#!/usr/bin/env python3
"""本地 chat API + 兜底 web 页 — P5（UI 分层裁定的「文本接收面」）。

stdlib-only、无构建链：GET / 内嵌单文件页面（vanilla），POST /api/chat 复用
orchestrator.handle。默认绑 127.0.0.1；config server_host="0.0.0.0" 可暴露 LAN
（机器人形态用——注意：服务进程持有全部凭证，暴露即授权局域网内提问）。
协议 v1 = POST/JSON（l/c 分支尚无 token 流，SSE 待分支流式化后一并做——
见 pc-package-plan P5 修正注记）。v1 无会话态（每请求独立 resolver）。
OpenAI 兼容面：GET /v1/models、POST /v1/chat/completions——Raycast 等
custom provider 即插即用（代理五分支全在本进程，端点只是协议适配）。

用法：python3 pc/orchestrator.py --serve [port]   # 默认 8765
"""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from orchestrator import RouterUnavailable, _maybe_speak, handle, load_config  # noqa: E402
from session_resolver import SessionResolver  # noqa: E402

PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>firela-pa</title><style>
 body{font-family:-apple-system,sans-serif;max-width:640px;margin:40px auto;padding:0 16px;background:#faf9f7;color:#1c1b1a}
 h1{font-size:18px;font-weight:600} h1 span{color:#8a8a85;font-weight:400;font-size:13px;margin-left:8px}
 #log{display:flex;flex-direction:column;gap:10px;margin:16px 0}
 .m{padding:10px 14px;border-radius:12px;white-space:pre-wrap;line-height:1.5}
 .q{background:#e8e4de;align-self:flex-end;max-width:80%}
 .a{background:#fff;border:1px solid #e5e1db;max-width:95%}
 .a .r{display:inline-block;font-size:11px;color:#8a8a85;border:1px solid #ddd;padding:0 6px;border-radius:8px;margin-bottom:6px}
 form{display:flex;gap:8px} input{flex:1;padding:12px 14px;border:1px solid #ddd9d3;border-radius:10px;font-size:15px;outline:none}
 button{padding:12px 18px;border:0;border-radius:10px;background:#1c1b1a;color:#fff;font-size:15px}
 button:disabled{opacity:.4}
 .hint{color:#8a8a85;font-size:12px;margin-top:10px}
</style></head><body>
<h1>firela-pa <span>本地财务顾问 · 全部推理在本机</span></h1>
<div id="log"></div>
<form id="f"><input id="q" placeholder="问点什么…（支持系统听写 / superwhisper 注入）" autocomplete="off"><button>问</button></form>
<div class="hint">五分支：闲聊(l) 本地答 · 查账(tx) / 全景(pf) 走 vlt · 行情(mkt) 走 openbb · 分析(c) 涂黑上云</div>
<script>
const log=document.getElementById('log'),f=document.getElementById('f'),q=document.getElementById('q'),b=f.querySelector('button');
function add(cls){const d=document.createElement('div');d.className='m '+cls;log.appendChild(d);return d}
f.onsubmit=async e=>{e.preventDefault();const text=q.value.trim();if(!text)return;
 const md=add('q');md.textContent=text;q.value='';b.disabled=true;
 const wait=add('a');wait.textContent='…';
 try{const r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text})});
   const d=await r.json();
   wait.textContent='';                                   // textContent 全程承载不可信内容（无 innerHTML）
   if(d.route){const chip=document.createElement('span');chip.className='r';chip.textContent=d.route;wait.appendChild(chip);wait.appendChild(document.createElement('br'));}
   wait.appendChild(document.createTextNode(d.answer||d.error||'?'));}
 catch(err){wait.textContent=String(err);wait.style.color='#b00'}
 b.disabled=false;q.focus();};
(async()=>{try{const r=await fetch('/api/audit');const d=await r.json();   // P10 审计 banner：onload 惰性拉取
 if(d.answer){const a=add('a');a.textContent='📋 '+d.answer;}}catch(e){}})();   // textContent 承载（无 innerHTML）
q.focus();
</script></body></html>"""


MODEL_ID = "firela-pa"


def _speak_async(out, cfg):
    """HTTP 面播报不阻塞响应——播报时长随答案长度线性涨，答案文本早就绪。
    ponytail: 并发请求会重叠播放（单用户可接受），要排队再加播放队列。"""
    threading.Thread(target=_maybe_speak, args=(out, cfg), daemon=True).start()


def _user_text(content):
    """content 兼容 str 与 OpenAI 多部件 list（拼接 text 部件）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content
                       if isinstance(p, dict) and p.get("type") == "text")
    return ""


def make_handler(cfg):
    # ponytail: OpenAI 端点共享单 resolver（REPL 同款，追问「它呢」可消解）——
    # 单共享态假设单活跃会话；多窗口交错/重启丢 prev 可接受，需隔离再按会话分键
    oa_resolver = SessionResolver()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/":
                self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            elif self.path == "/api/health":
                self._send(200, {"ok": True})
            elif self.path == "/api/audit":
                # P10 惰性审计面：只读=无 POST/聊天面变更；DB 写（shown 记账/存档）有意。
                # 30 天内存档重显（防刷新烧静默）；过期重跑一批。
                import audit
                import orchestrator as _orch
                today = _orch._TODAY()
                rep = audit.stored_report(today)
                if rep is None:
                    try:
                        rep, _f = audit.audit_report(_orch.Vlt(cfg), today, cfg)
                    except Exception as e:
                        rep = f"审计暂不可用（{e}）——顺延下次访问。"
                self._send(200, {"answer": rep})
            elif self.path == "/v1/models":
                self._send(200, {"object": "list",
                                 "data": [{"id": MODEL_ID, "object": "model"}]})
            else:
                self._send(404, {"error": "not found"})

        def _origin_ok(self):
            """#16：Origin 门（DNS-rebinding/跨站 POST 防线）。缺席=放行（curl/Raycast 原生客户端
            不发 Origin）；loopback/同 Host/配置 extra_allowed_origins 放行；其余 403。"""
            origin = self.headers.get("Origin")
            if not origin:
                return True
            host = self.headers.get("Host", "")
            allowed = {"http://localhost", "http://127.0.0.1", "http://[::1]"}
            for a in cfg.get("extra_allowed_origins", []):
                allowed.add(str(a))
            if origin.rstrip("/") in allowed:
                return True
            try:                                           # 同 host（端口随变）= 放行
                from urllib.parse import urlsplit
                if urlsplit(origin).hostname in ("localhost", "127.0.0.1", "::1"):
                    return True
                if host and urlsplit(origin).hostname == host.rsplit(":", 1)[0].strip("[]"):
                    return True
            except ValueError:
                pass
            return False

        def do_POST(self):
            if not self._origin_ok():
                return self._send(403, {"error": "origin not allowed"})
            if self.path == "/v1/chat/completions":
                return self._openai_chat()
            if self.path != "/api/chat":
                return self._send(404, {"error": "not found"})
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            except (ValueError, json.JSONDecodeError):
                return self._send(400, {"error": "bad json"})
            if not isinstance(body, dict):
                return self._send(400, {"error": "bad json"})
            text = str(body.get("text", "")).strip()
            if not text:
                return self._send(400, {"error": "empty text"})
            resolver = SessionResolver()
            try:
                out = handle(text, resolver, cfg, entry="web")
            except RouterUnavailable as e:
                return self._send(503, {"error": str(e)})
            except SystemExit as e:                       # need() 缺配置等
                return self._send(400, {"error": str(e)})
            _speak_async(out, cfg)
            self._send(200, {"answer": out, "route": (resolver.prev or {}).get("t")})

        def _oai_err(self, code, message, etype="invalid_request_error"):
            self._send(code, {"error": {"message": message, "type": etype}})

        def _openai_chat(self):
            """OpenAI 兼容适配：取最后一条 user 消息进既有管线，答案包回 chat 形。
            tools/temperature 等一律忽略——工具是 pa 内部执行器的事，对外伪装成普通模型。"""
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            except (ValueError, json.JSONDecodeError):
                body = None
            if not isinstance(body, dict):
                return self._oai_err(400, "bad json")
            question = ""
            for m in reversed(body.get("messages") or []):   # 历史只作展示，单轮契约不变
                if isinstance(m, dict) and m.get("role") == "user":
                    question = _user_text(m.get("content")).strip()
                    break
            if not question:
                return self._oai_err(400, "no user message")
            try:
                out = handle(question, oa_resolver, cfg, entry="openai")
            except RouterUnavailable as e:
                return self._oai_err(503, str(e), "server_error")
            except SystemExit as e:                          # need() 缺配置等
                return self._oai_err(400, str(e))
            _speak_async(out, cfg)
            rid, created = f"chatcmpl-{time.time_ns()}", int(time.time())
            if body.get("stream"):
                return self._send_sse(out, rid, created)
            self._send(200, {
                "id": rid, "object": "chat.completion", "created": created, "model": MODEL_ID,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": out},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            })

        def _send_sse(self, out, rid, created):
            """单内容块流式：两帧 + [DONE]——分支 token 流式化前 chunk 粒度=整段答案。"""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            for delta, finish in (({"role": "assistant", "content": out}, None), ({}, "stop")):
                chunk = {"id": rid, "object": "chat.completion.chunk", "created": created,
                         "model": MODEL_ID,
                         "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")

        def log_message(self, fmt, *args):                # 访问日志静默（问题文本不进日志；
            pass                                          # 有意收集走 telemetry 本地 0600 库）

    return Handler


def serve(cfg, port=8765):
    host = cfg.get("server_host", "127.0.0.1")
    httpd = ThreadingHTTPServer((host, port), make_handler(cfg))
    print(f"firela-pa web: http://{host}:{port} （Ctrl-C 停止）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass

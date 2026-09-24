#!/usr/bin/env python3
"""server 端点测试 — stub 注入 handle/_maybe_speak（不碰 Ollama/真配置）。

覆盖：OpenAI 兼容面（/v1/chat/completions 非流式+流式、/v1/models、
历史取最后一条 user、多部件 content、错误形）+ /api/chat 回归。
跑法：python3 pc/test_server.py
"""
import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import server  # noqa: E402

CAPTURED = []


def _fake_handle(question, resolver, cfg, entry="repl"):
    CAPTURED.append(question)
    return f"stub:{question}"


server.handle = _fake_handle                       # Handler 运行时查 server 模块全局，可注入
server._maybe_speak = lambda out, cfg: None

httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler({}))
PORT = httpd.server_address[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()

FAILS = []
CHECKS = 0


def check(name, ok, detail=""):
    global CHECKS
    CHECKS += 1
    if not ok:
        FAILS.append((name, detail))


def call(path, payload=None, raw_body=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}",
        data=raw_body if raw_body is not None else (
            json.dumps(payload).encode() if payload is not None else None),
        headers={"Content-Type": "application/json"},
        method="POST" if (payload is not None or raw_body is not None) else "GET")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read().decode()


# ---- 非流式 ----
code, _, raw = call("/v1/chat/completions",
                    {"model": "whatever", "messages": [{"role": "user", "content": "本月餐饮花了多少"}]})
d = json.loads(raw)
check("nonstream-200", code == 200, raw)
check("nonstream-content",
      d["choices"][0]["message"] == {"role": "assistant", "content": "stub:本月餐饮花了多少"}, raw)
check("nonstream-shape",
      d["object"] == "chat.completion" and d["model"] == "firela-pa"
      and d["choices"][0]["finish_reason"] == "stop" and d["usage"]["total_tokens"] == 0, raw)

# ---- 流式：SSE 两帧 + [DONE] ----
code, ctype, raw = call("/v1/chat/completions",
                        {"stream": True, "messages": [{"role": "user", "content": "hi"}]})
frames = [ln[len("data: "):] for ln in raw.splitlines() if ln.startswith("data: ")]
check("sse-ctype", ctype.startswith("text/event-stream"), ctype)
check("sse-frames", len(frames) == 3 and frames[-1] == "[DONE]", raw)
first, second = json.loads(frames[0]), json.loads(frames[1])
check("sse-first-delta",
      first["choices"][0]["delta"] == {"role": "assistant", "content": "stub:hi"}
      and first["object"] == "chat.completion.chunk", raw)
check("sse-second-finish",
      second["choices"][0]["delta"] == {} and second["choices"][0]["finish_reason"] == "stop", raw)

# ---- 历史多消息 → 取最后一条 user（system/assistant 忽略）----
call("/v1/chat/completions", {"messages": [
    {"role": "system", "content": "you are helpful"},
    {"role": "user", "content": "苹果股价多少"},
    {"role": "assistant", "content": "102.5"},
    {"role": "user", "content": "它呢"},
]})
check("last-user", CAPTURED[-1] == "它呢", repr(CAPTURED[-1]))

# ---- content 多部件 list → 拼 text 部件 ----
call("/v1/chat/completions", {"messages": [{"role": "user", "content": [
    {"type": "text", "text": "查"}, {"type": "image_url", "image_url": {}}, {"type": "text", "text": "餐饮"},
]}]})
check("multipart-content", CAPTURED[-1] == "查餐饮", repr(CAPTURED[-1]))

# ---- 错误形：无 user 消息 / 坏 JSON → 400 OpenAI error 形 ----
code, _, raw = call("/v1/chat/completions", {"messages": [{"role": "system", "content": "x"}]})
check("no-user-400", code == 400 and json.loads(raw)["error"]["type"] == "invalid_request_error", raw)
code, _, raw = call("/v1/chat/completions", None, raw_body=b"{oops")
check("bad-json-400", code == 400 and "error" in json.loads(raw), raw)

# ---- 回归：/api/chat 与 /v1/models ----
code, _, raw = call("/api/chat", {"text": "hi"})
check("api-chat-regress", code == 200 and json.loads(raw)["answer"] == "stub:hi", raw)
code, _, raw = call("/v1/models")
check("models", code == 200 and any(m["id"] == "firela-pa"
                                    for m in json.loads(raw)["data"]), raw)

# ---- #16 Origin 门：缺席=放行（本测试全部无 Origin——上方用例即证）；本地=放行；外来=403 ----
def call_with_headers(path, payload, extra_headers):
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **extra_headers}, method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


code, raw = call_with_headers("/api/chat", {"text": "hi"},
                              {"Origin": f"http://127.0.0.1:{PORT}"})
check("origin-local-ok", code == 200 and json.loads(raw)["answer"] == "stub:hi", raw)
code, raw = call_with_headers("/v1/chat/completions",
                              {"messages": [{"role": "user", "content": "hi"}]},
                              {"Origin": "https://evil.example.com"})
check("origin-evil-403", code == 403, raw)
code, raw = call_with_headers("/api/chat", {"text": "hi"},
                              {"Origin": "null"})
check("origin-null-403", code == 403, raw)

httpd.shutdown()

if FAILS:
    for name, detail in FAILS:
        print(f"[FAIL] {name}: {detail}")
    sys.exit(1)
print(f"test_server: all {CHECKS} checks passed")

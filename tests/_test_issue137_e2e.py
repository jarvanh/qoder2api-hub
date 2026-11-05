# -*- coding: utf-8 -*-
"""#137 端到端：伪 XML 工具调用经真实 HTTP 路由后变成结构化 tool_calls。

    python tests/_test_issue137_e2e.py

与 _test_issue137_tool_call_tags.py 互补：那份测解析器与回读链，这份起真实
Handler + 真实 socket，桩掉**上游连接**，断言客户端（ZCode / Codex 等）
看到的响应里是标准的 tool_calls，而不是一段 `<tool_call>` 正文。

隔离约定：
  · ACCOUNTS_DIR / QD_PROXY_USAGE_DIR 指向临时目录；只用假凭证；
  · open_upstream 用桩替换 —— 不访问真实 Qoder 上游，不产生积分消耗。
"""
import http.client
import http.server
import io
import json
import os
import sys
import tempfile
import threading
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *          # noqa: F401,F403

_TMP = tempfile.mkdtemp(prefix="i137e2e-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["QD_PROXY_USAGE_DIR"] = os.path.join(_TMP, "usage")
os.environ["QD_NATIVE_IDENTITY"] = "0"
for _v in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy",
           "https_proxy", "all_proxy"):
    os.environ.pop(_v, None)

P, A = bootstrap()
declare_suite(__file__, ["qoder_proxy.py"])
import qoder_settings as ST        # noqa: E402

ACC = os.environ["ACCOUNTS_DIR"]
P.ACCOUNTS_DIR = ACC
P.POOL = A.AccountPool(ACC, log=lambda *_a, **_k: None)
P.API_KEY = "sk-i137"
P.PANEL = ST.PanelSessions(lambda: P.ACCOUNTS_DIR)
P.POOL.add(A.Account(dict(uid="fake-acc-137", realm="cn",
                          accessToken="FAKE-ACCESS-TOKEN",
                          refreshToken="FAKE-REFRESH-TOKEN")))

NAME = "Bash"
AUTH = {"Authorization": "Bearer sk-i137"}

SAMPLE = ('[assistant 请求调用工具] <tool_call> Bash {"command":"ls -la",'
          '"description":"list files"} </tool_call>')


def inner_chunk(delta, finish=None):
    return json.dumps({"id": "i137", "object": "chat.completion.chunk",
                       "model": "fake-model",
                       "choices": [{"index": 0, "delta": delta,
                                    "finish_reason": finish}]},
                      ensure_ascii=False)


def envelope(body):
    """包成上游信封格式（iter_inner_sse 认这个）。"""
    outer = {"headers": {}, "body": body, "statusCodeValue": 200}
    return ("data:" + json.dumps(outer, ensure_ascii=False) + "\n\n").encode()


def upstream_stream(text, n=7):
    """把一段正文按 n 字符切片做成上游 SSE 流。"""
    lines = [envelope(inner_chunk({"content": text[i:i + n]}))
             for i in range(0, len(text), n)]
    lines.append(envelope(inner_chunk({}, "stop")))
    lines.append(envelope("[DONE]"))
    return io.BytesIO(b"".join(lines))


CURRENT = {"stream": None}


def _fake_open_upstream(payload, **_kw):
    return CURRENT["stream"], P.POOL.accounts[0], ""


class _Quiet(object):
    def log_message(self, *_):
        pass


class Gateway(_Quiet, P.Handler):
    pass


_srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
_srv.daemon_threads = True
threading.Thread(target=_srv.serve_forever, daemon=True).start()
PORT = _srv.server_address[1]


def post(path, body):
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=20)
    hdr = dict(AUTH)
    hdr["Content-Type"] = "application/json"
    conn.request("POST", path, body=json.dumps(body).encode("utf-8"),
                 headers=hdr)
    r = conn.getresponse()
    raw = r.read()
    conn.close()
    return r.status, raw


def sse_events(raw):
    out = []
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        body = line[5:].strip()
        if not body or body == "[DONE]":
            continue
        try:
            out.append(json.loads(body))
        except Exception:
            continue
    return out


def tool_calls_of(events):
    """按 OpenAI 流式协议把分帧的 tool_calls 拼回去。"""
    acc = {}
    text = []
    finish = []
    for ev in events:
        ch = (ev.get("choices") or [{}])[0]
        d = ch.get("delta") or {}
        if isinstance(d.get("content"), str):
            text.append(d["content"])
        for tc in d.get("tool_calls") or []:
            i = tc.get("index", 0)
            slot = acc.setdefault(i, {"id": "", "name": "", "args": ""})
            fn = tc.get("function") or {}
            if tc.get("id"):
                slot["id"] = tc["id"]
            if fn.get("name"):
                slot["name"] += fn["name"]
            if fn.get("arguments"):
                slot["args"] += fn["arguments"]
        if ch.get("finish_reason"):
            finish.append(ch["finish_reason"])
    return [acc[k] for k in sorted(acc)], "".join(text), finish


with mock.patch.object(P, "open_upstream", _fake_open_upstream):
    # ---- 1) chat 流式：正文里的伪 XML 变成结构化 tool_calls
    CURRENT["stream"] = upstream_stream(SAMPLE)
    st, raw = post("/v1/chat/completions", {
        "model": "fake-model", "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function", "function": {"name": NAME,
                   "parameters": {"type": "object", "properties": {}}}}],
    })
    calls, txt, fins = tool_calls_of(sse_events(raw))
    check("[1] 流式 200", st == 200, st)
    check("[1b] 恢复出一个 tool_call", len(calls) == 1, calls)
    check("[1c] 工具名正确", calls and calls[0]["name"] == NAME, calls)
    check("[1d] id 非空（issue #47）", calls and bool(calls[0]["id"]), calls)
    check("[1e] 参数是合法 JSON",
          calls and isinstance(json.loads(calls[0]["args"]), dict), calls)
    check("[1f] 正文不含协议残片", txt == "", repr(txt))
    check("[1g] finish_reason=tool_calls", "tool_calls" in fins, fins)

    # ---- 2) chat 非流式
    CURRENT["stream"] = upstream_stream(SAMPLE, 4096)
    st, raw = post("/v1/chat/completions", {
        "model": "fake-model", "stream": False,
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function", "function": {"name": NAME,
                   "parameters": {"type": "object", "properties": {}}}}],
    })
    obj = json.loads(raw.decode("utf-8"))
    msg = (obj.get("choices") or [{}])[0].get("message") or {}
    tc = msg.get("tool_calls") or []
    check("[2] 非流式 200", st == 200, st)
    check("[2b] 非流式恢复出 tool_calls", len(tc) == 1, tc)
    check("[2c] 非流式工具名正确",
          tc and tc[0]["function"]["name"] == NAME, tc)
    check("[2d] 非流式正文已剥离", (msg.get("content") or "") == "",
          repr(msg.get("content")))
    check("[2e] 非流式 finish=tool_calls",
          (obj.get("choices") or [{}])[0].get("finish_reason") == "tool_calls",
          obj.get("choices"))

    # ---- 3) Responses 流式（Codex 形态）
    CURRENT["stream"] = upstream_stream(SAMPLE)
    st, raw = post("/v1/responses", {
        "model": "fake-model", "stream": True, "input": "hi",
        "tools": [{"type": "function", "name": NAME,
                   "parameters": {"type": "object", "properties": {}}}],
    })
    evs = []
    for line in raw.decode("utf-8", "replace").splitlines():
        if line.startswith("event: "):
            evs.append(line[7:].strip())
    check("[3] Responses 流式 200", st == 200, st)
    check("[3b] 出现 function_call_arguments.done",
          "response.function_call_arguments.done" in evs, evs)
    check("[3c] 没有正文 delta 泄漏",
          "response.output_text.delta" not in evs, evs)
    check("[3d] 正常完成", evs and evs[-1] == "response.completed", evs[-1:])

    # ---- 4) Anthropic 流式
    CURRENT["stream"] = upstream_stream(SAMPLE)
    st, raw = post("/v1/messages", {
        "model": "fake-model", "stream": True, "max_tokens": 256,
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"name": NAME, "description": "run",
                   "input_schema": {"type": "object", "properties": {}}}],
    })
    aevs = []
    for line in raw.decode("utf-8", "replace").splitlines():
        if line.startswith("event: "):
            aevs.append(line[7:].strip())
    check("[4] Anthropic 流式 200", st == 200, st)
    check("[4b] 出现 tool_use 块", "content_block_start" in aevs, aevs)
    check("[4c] 出现 input_json_delta",
          "content_block_delta" in aevs, aevs)
    check("[4d] stop_reason 是 tool_use（message_delta 已发）",
          "message_delta" in aevs, aevs)

    # ---- 5) 普通正文不受影响
    plain = "这是一段普通回复，没有任何工具调用。"
    CURRENT["stream"] = upstream_stream(plain, 5)
    st, raw = post("/v1/chat/completions", {
        "model": "fake-model", "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function", "function": {"name": NAME,
                   "parameters": {"type": "object", "properties": {}}}}],
    })
    calls, txt, _ = tool_calls_of(sse_events(raw))
    check("[5] 普通正文无 tool_calls", calls == [], calls)
    check("[5b] 普通正文逐字节守恒", txt == plain, repr(txt))

_srv.shutdown()
finish()

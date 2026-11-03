# -*- coding: utf-8 -*-
"""#131 六项缺陷的行为级回归（起真实 Handler + 本机桩，断言问题不再复现）。

    python tests/_test_issue131_e2e.py

与 _test_issue131_audit_fixes.py 互补：那份钉源码落点，这份走真实 HTTP 路由，
复现报告里的攻击路径，断言「不再命中」。每组都带一条正向对照（合法请求照常通过），
避免「环境本身就连不上」被误读成「修复生效」。

隔离约定：
  · ACCOUNTS_DIR / QD_PROXY_USAGE_DIR 指向临时目录；只用假凭证；
  · socket.connect 全程只放行环回地址 —— 任何外连（含真实 Qoder 上游）直接抛错；
  · Qoder 上游由 open_upstream / aggregate_stream 桩代替；
  · SSRF 组里的「公网主机」是 DNS 桩 + 连接重定向到本机服务，不访问公网；
  · 「启动时已配代理」用子进程复现（代理变量须在 import 之前就在环境里）。
"""
import copy
import http.client
import http.server
import ipaddress
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *          # noqa: F401,F403

_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy",
               "https_proxy", "all_proxy", "NO_PROXY", "no_proxy",
               "QD_WEB_TOOLS_PROXY")

# ---------------------------------------------------------------- 网络隔离 / 桩
_real_connect = socket.socket.connect
_real_getaddrinfo = socket.getaddrinfo
_real_create_connection = socket.create_connection
FAKE_PUBLIC_IP = "93.184.216.34"    # 只出现在 DNS 桩里，连接被改道到本机 Public 服务
PUBLIC_PORT = int(os.environ.get("I131_PUBLIC_PORT") or 0)
_rebind_calls = {}


def _loopback_only(sock, address):
    if not isinstance(address, tuple) or address[0] not in ("127.0.0.1", "::1"):
        raise RuntimeError("suite blocked non-loopback connection: %r" % (address,))
    return _real_connect(sock, address)


def fake_dns(host, port, *a, **k):
    h = str(host or "").lower()
    if h == "pub.i131.invalid":
        return _real_getaddrinfo(FAKE_PUBLIC_IP, port, *a, **k)
    if h == "inner.i131.invalid":
        return _real_getaddrinfo("127.0.0.1", port, *a, **k)
    if h == "multi.i131.invalid":
        return (_real_getaddrinfo(FAKE_PUBLIC_IP, port, *a, **k)
                + _real_getaddrinfo("127.0.0.1", port, *a, **k))
    if h == "rebind.i131.invalid":
        # 报告里的形态：校验阶段（port=None）给公网，建连阶段（带端口）给环回
        return _real_getaddrinfo(FAKE_PUBLIC_IP if port is None else "127.0.0.1",
                                 port, *a, **k)
    if h == "flip.i131.invalid":
        # 另一种形态：第一次公网，之后全部环回（TTL=0 的重绑定）
        n = _rebind_calls[h] = _rebind_calls.get(h, 0) + 1
        return _real_getaddrinfo(FAKE_PUBLIC_IP if n == 1 else "127.0.0.1",
                                 port, *a, **k)
    if h.endswith(".invalid"):
        raise socket.gaierror(socket.EAI_NONAME, "suite: no such host")
    return _real_getaddrinfo(host, port, *a, **k)


def fake_create_connection(address, *a, **k):
    """按（打桩的）DNS 解析后再连；「公网 IP」上只跑着 Public 服务。

    不论实现传进来的是域名还是已钉死的 IP 都按同一规则处理，这样桩不偏袒
    任何一种实现；端口一律落到 Public，重绑定用例里 URL 写内网端口也不会被
    桩误送进 Internal。
    """
    host, port = address[0], address[1]
    try:
        ip = str(ipaddress.ip_address(host))
    except ValueError:
        ip = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)[0][4][0]
    if ip == FAKE_PUBLIC_IP:
        return _real_create_connection(("127.0.0.1", PUBLIC_PORT), *a, **k)
    return _real_create_connection((ip, port), *a, **k)


def env_proxies_only():
    """Windows / macOS 上 urllib 在没有代理变量时会读系统代理（注册表 / scutil）。
    开发机常开着 Clash 之类的系统代理，会让结果随机器而变；这里只认环境变量，
    套件才在哪台机器上都一样。必须在 import 被测模块之前调用。"""
    urllib.request.getproxies = urllib.request.getproxies_environment


def install_net_stubs():
    env_proxies_only()
    for p in (mock.patch.object(socket.socket, "connect", _loopback_only),
              mock.patch.object(socket, "getaddrinfo", fake_dns),
              mock.patch.object(socket, "create_connection", fake_create_connection)):
        p.start()


if os.environ.get("I131_PROXY_CHILD"):
    # 子进程：代理变量已在 import 之前写进环境（模拟「网关启动时就配了代理」）
    install_net_stubs()
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    import qoder_webtools as _wt
    _out = {}
    for _tag, _url in json.loads(os.environ["I131_PROXY_CHILD"]).items():
        _r = _wt.fetch(_url)
        _out[_tag] = {"ok": bool(_r.get("ok")), "text": _r.get("text") or "",
                      "error": _r.get("error") or ""}
    print("I131_CHILD_RESULT " + json.dumps(_out))
    sys.exit(0)

_TMP = tempfile.mkdtemp(prefix="i131e2e-")
for _name in _PROXY_VARS:
    os.environ.pop(_name, None)
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["QD_PROXY_USAGE_DIR"] = os.path.join(_TMP, "usage")
os.environ["QD_NATIVE_IDENTITY"] = "0"
env_proxies_only()

P, A = bootstrap()
declare_suite(__file__, ["qoder_proxy.py", "qoder_accounts.py",
                         "qoder_webtools.py", "qoder_settings.py"])
import qoder_settings as ST        # noqa: E402
import qoder_webtools as WT        # noqa: E402

_guard = mock.patch.object(socket.socket, "connect", _loopback_only)
_guard.start()
_servers = []


def serve(handler):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _servers.append(srv)
    return srv.server_address[1]


class _Quiet(object):
    def log_message(self, *_):
        pass


class Gateway(_Quiet, P.Handler):
    pass


ACC = os.environ["ACCOUNTS_DIR"]
P.ACCOUNTS_DIR = ACC
P.POOL = A.AccountPool(ACC, log=lambda *_a, **_k: None)
P.API_KEY = "launcher-key-131"
P.PANEL = ST.PanelSessions(lambda: P.ACCOUNTS_DIR)
P.POOL.add(A.Account(dict(uid="fake-acc-131", realm="cn",
                          accessToken="FAKE-ACCESS-TOKEN",
                          refreshToken="FAKE-REFRESH-TOKEN")))
UID = "fake-acc-131"
PORT = serve(Gateway)


def req(path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    hdr = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdr["Content-Type"] = "application/json"
    conn.request("POST" if body is not None else "GET", path, body=data,
                 headers=hdr)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    try:
        return resp.status, json.loads(raw or b"{}")
    except ValueError:
        return resp.status, {"_raw": raw[:200]}


def panel(token):
    return {"X-Panel-Token": token}


def limited_key(**extra):
    row = dict(id="limited", name="limited", key="sk-limited-131",
               models=["allowed-model"], note="备注A", enabled=True)
    row.update(extra)
    return row


# ---------------------------------------------------------------- 1
print("[#131-1] 面板读→存往返不再清空 Key 白名单")
ST.set_api_keys(ACC, [limited_key()])
sess = P.PANEL.create()
st, view = req("/settings", headers=panel(sess))
row = ((view.get("api_keys") or [{}])[0]) if st == 200 else {}
check("GET /settings 200", st == 200, st)
check("视图回显 models", row.get("models") == ["allowed-model"], row)
check("视图回显 note", row.get("note") == "备注A", row)

st, _ = req("/settings/save", {"api_keys": view.get("api_keys")}, panel(sess))
saved = ST.api_keys(ACC)[0]
check("原样回存：200", st == 200, st)
check("原样回存：白名单保留", saved.get("models") == ["allowed-model"], saved)
check("原样回存：备注保留", saved.get("note") == "备注A", saved)
check("原样回存：Key 明文未被视图掩码覆盖",
      saved.get("key") == "sk-limited-131", saved.get("key"))

# 老前端 / 只改名的载荷：不带 models、note 字段 → 必须沿用旧值
_rows = [{k: v for k, v in r.items() if k not in ("models", "note")}
         for r in view.get("api_keys") or []]
for r in _rows:
    r["name"] = "renamed"
st, _ = req("/settings/save", {"api_keys": _rows}, panel(sess))
saved = ST.api_keys(ACC)[0]
check("缺省字段：改名生效", st == 200 and saved.get("name") == "renamed",
      (st, saved))
check("缺省字段：白名单沿用旧值（不是放开成不限）",
      saved.get("models") == ["allowed-model"], saved)
check("缺省字段：备注沿用旧值", saved.get("note") == "备注A", saved)

# 显式提交空列表 = 明确清除限制（语义要能表达）
_rows = [dict(r, models=[]) for r in view.get("api_keys") or []]
st, _ = req("/settings/save", {"api_keys": _rows}, panel(sess))
check("显式 models=[] 才清除限制",
      st == 200 and ST.api_keys(ACC)[0].get("models") == [],
      ST.api_keys(ACC)[0])

# ---------------------------------------------------------------- 2 / 3 桩
UPSTREAM = []      # 每次 open_upstream 的出站请求体
NET = []           # 网络层（http_json）调用：被拒请求必须一次都没有
SCRIPT = []        # aggregate_stream 依次返回的回复


def _fake_open_upstream(payload, **kwargs):
    UPSTREAM.append(copy.deepcopy(payload))
    return io.BytesIO(b""), P.POOL.accounts[0], ""


def _fake_aggregate(*_a, **_k):
    # 没排脚本时也给一条正常回复：回归时「被放行」要表现为断言红，而不是 handler 崩
    return copy.deepcopy(SCRIPT.pop(0) if SCRIPT else reply("unexpected"))


def _fake_http_json(*a, **k):
    NET.append((a, k))
    raise RuntimeError("suite: network layer must not be reached")


def reply(content="ok", usage=(10, 5), tool_calls=None, model="allowed-model"):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {"id": "i131", "object": "chat.completion", "model": model,
            "choices": [{"index": 0, "message": msg,
                         "finish_reason": "tool_calls" if tool_calls else "stop"}],
            "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1],
                      "total_tokens": usage[0] + usage[1]}}


_stubs = [mock.patch.object(P, "open_upstream", _fake_open_upstream),
          mock.patch.object(P, "aggregate_stream", _fake_aggregate),
          mock.patch.object(A, "http_json", _fake_http_json)]
for _s in _stubs:
    _s.start()

AUTH = {"Authorization": "Bearer sk-limited-131"}
ST.set_api_keys(ACC, [limited_key()])
ST.set_web_tools_enabled(ACC, True)
CHAT = {"model": "blocked-model", "messages": [{"role": "user", "content": "hi"}]}
FETCH_TOOL = [{"type": "web_fetch"}]

print("[#131-2] 声明 web 工具不能绕过模型封禁 / Key 白名单")
ST.set_banned_models(ACC, ["blocked-model"])
del UPSTREAM[:], NET[:]
st, _ = req("/v1/chat/completions", CHAT, AUTH)
check("对照：普通请求封禁模型 → 400", st == 400, st)
st, body = req("/v1/chat/completions", dict(CHAT, tools=FETCH_TOOL), AUTH)
check("chat + web_fetch 请求封禁模型 → 400", st == 400, (st, body))
st, body = req("/v1/responses", {"model": "blocked-model", "input": "hi",
                                 "tools": [{"type": "web_search"}]}, AUTH)
check("responses + web_search 请求封禁模型 → 400", st == 400, (st, body))
check("封禁路径没有出站（open_upstream 0 次）", len(UPSTREAM) == 0, len(UPSTREAM))
check("封禁路径没有出站（网络层 0 次）", len(NET) == 0, len(NET))

ST.set_banned_models(ACC, [])
st, _ = req("/v1/chat/completions", CHAT, AUTH)
check("对照：普通请求白名单外模型 → 400", st == 400, st)
st, body = req("/v1/chat/completions", dict(CHAT, tools=FETCH_TOOL), AUTH)
check("chat + web_fetch 请求白名单外模型 → 400", st == 400, (st, body))
check("白名单拒绝路径没有出站", len(UPSTREAM) == 0 and len(NET) == 0,
      (len(UPSTREAM), len(NET)))

SCRIPT[:] = [reply("ok")]
st, body = req("/v1/chat/completions",
               dict(CHAT, model="allowed-model", tools=FETCH_TOOL), AUTH)
_decl = [((t.get("function") or {}).get("name"))
         for t in ((UPSTREAM[-1].get("tools") or []) if UPSTREAM else [])]
check("对照：白名单内模型 + web_fetch → 200", st == 200, (st, body))
check("对照：确实走了 web 工具分支（出站带 web_fetch 函数声明）",
      "web_fetch" in _decl, _decl)

print("[#131-3] web 工具多轮推理要逐轮记账并合计回给客户端")


def _usage_rows():
    try:
        with io.open(P.USAGE_LOG, encoding="utf-8") as fh:
            return [json.loads(l) for l in fh if l.strip()]
    except OSError:
        return []


_before_rows = len(_usage_rows())
_before_tokens = A.fold_daily_usage(P.USAGE_LOG)["tokens"].get(UID, 0)
_call = {"id": "call_131", "type": "function",
         "function": {"name": "web_fetch",
                      "arguments": json.dumps({"url": "http://127.0.0.1:9/x"})}}
SCRIPT[:] = [reply("", usage=(10, 5), tool_calls=[_call]),
             reply("done", usage=(20, 7))]
del UPSTREAM[:]
st, body = req("/v1/chat/completions",
               dict(CHAT, model="allowed-model", tools=FETCH_TOOL), AUTH)
_u = body.get("usage") or {}
_new = [r for r in _usage_rows()[_before_rows:] if not r.get("error")]
check("两轮推理 → 200 且上游被调用两次", st == 200 and len(UPSTREAM) == 2,
      (st, len(UPSTREAM)))
check("响应 usage 是两轮合计（30/12/42）",
      (_u.get("prompt_tokens"), _u.get("completion_tokens"),
       _u.get("total_tokens")) == (30, 12, 42), _u)
check("用量流水每轮一行", len(_new) == 2, len(_new))
check("流水行归属到真实账号", _new and all(r.get("account") == UID for r in _new),
      [r.get("account") for r in _new])
check("流水行归属到请求 Key", _new and all(r.get("key_id") == "limited" for r in _new),
      [r.get("key_id") for r in _new])
check("每日限额折叠能看到这部分消耗",
      A.fold_daily_usage(P.USAGE_LOG)["tokens"].get(UID, 0) - _before_tokens == 42,
      A.fold_daily_usage(P.USAGE_LOG)["tokens"].get(UID, 0))
_tool_msgs = [m for m in ((UPSTREAM[-1].get("messages") or []) if UPSTREAM else [])
              if m.get("role") == "tool"]
check("工具轮里抓内网地址被 SSRF 守卫挡下（结果回灌给模型）",
      len(_tool_msgs) == 1 and "allowed" in str(_tool_msgs[0].get("content")),
      _tool_msgs)

for _s in _stubs:
    _s.stop()

# ---------------------------------------------------------------- 4
print("[#131-4] 导入保留 uid 不能覆盖系统配置")
ST.set_panel_password(ACC, "strong-pass-131")
ST.set_api_keys(ACC, [dict(id="protected", key="sk-protected-131", enabled=True)])
_settings_path = os.path.join(ACC, "settings.json")
_settings_before = io.open(_settings_path, "rb").read()
sess = P.PANEL.create()
_RESERVED_TRIES = (("settings", False), ("settings", True), ("SETTINGS", True),
                   ("usage-2026", True), ("machine_identity", True))
for uid, overwrite in _RESERVED_TRIES:
    st, rep = req("/accounts/import",
                  {"data": [{"uid": uid, "accessToken": "FAKE-IMPORT-TOKEN"}],
                   "overwrite": overwrite}, panel(sess))
    _res = rep.get("result") or {}
    check("导入 uid=%s overwrite=%s 未计入新增" % (uid, overwrite),
          not _res.get("added") and not _res.get("updated"), (st, rep))
check("settings.json 字节未变", io.open(_settings_path, "rb").read() == _settings_before)
check("默认密码 admin 仍被拒绝", not ST.verify_panel_password(ACC, "admin"))
check("原面板密码仍有效", ST.verify_panel_password(ACC, "strong-pass-131"))
check("API Key 列表未被清空",
      [k.get("id") for k in ST.api_keys(ACC)] == ["protected"], ST.api_keys(ACC))
check("账号池里没有保留名账号",
      not any(P.POOL.get(uid) for uid, _ in _RESERVED_TRIES),
      [a.uid for a in P.POOL.accounts])
st, _ = req("/accounts/import",
            {"data": [{"uid": "normal-uid-131", "accessToken": "FAKE-OK"}]},
            panel(sess))
check("对照：普通 uid 照常导入", P.POOL.get("normal-uid-131") is not None, st)

# ---------------------------------------------------------------- 5
print("[#131-5] 匿名 / 伪造凭据不能注销全部会话")
sess = P.PANEL.create()
st, _ = req("/panel/logout", {})
check("匿名 logout → 401", st == 401, st)
check("匿名 logout 后原会话仍有效", P.PANEL.valid(sess))
st, _ = req("/panel/logout", {}, panel("forged-token"))
check("伪造 token logout → 401", st == 401, st)
st, _ = req("/panel/logout", {}, {"Authorization": "Bearer launcher-key-131"})
check("只带 API Key（无面板会话）logout → 401", st == 401, st)
check("上述尝试后原会话仍有效", P.PANEL.valid(sess))
st, _ = req("/panel/logout", {}, panel(sess))
check("对照：带有效会话 logout → 200 且会话作废",
      st == 200 and not P.PANEL.valid(sess), st)

# ---------------------------------------------------------------- 6
print("[#131-6] SSRF：重绑定 / 代理 / 多记录 / 重定向 / 地址分类")
HITS = {"internal": 0, "public": 0, "proxy": 0}
SENTINEL = "INTERNAL_SENTINEL_131"


class Internal(_Quiet, http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        HITS["internal"] += 1
        body = SENTINEL.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Public(_Quiet, http.server.BaseHTTPRequestHandler):
    """模拟一台公网服务：/redir?to=X 返回 302 → X，其它路径返回 PUBLIC_OK。"""

    def do_GET(self):
        HITS["public"] += 1
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        if q.get("to"):
            self.send_response(302)
            self.send_header("Location", q["to"][0])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = b"PUBLIC_OK"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Proxy(_Quiet, http.server.BaseHTTPRequestHandler):
    """把任何请求转发到内网服务的测试代理（命中即说明环境代理被用上了）。"""

    def do_GET(self):
        HITS["proxy"] += 1
        conn = http.client.HTTPConnection("127.0.0.1", INTERNAL_PORT, timeout=5)
        conn.request("GET", "/")
        body = conn.getresponse().read()
        conn.close()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


INTERNAL_PORT = serve(Internal)
PUBLIC_PORT = serve(Public)
PROXY_PORT = serve(Proxy)


def fetch(url):
    WT._CACHE.clear()
    return WT.fetch(url)


def leaked(res):
    return SENTINEL in str(res.get("text") or "")


with mock.patch.object(socket, "getaddrinfo", fake_dns), \
        mock.patch.object(socket, "create_connection", fake_create_connection):
    res = fetch("http://pub.i131.invalid:%d/hello" % PUBLIC_PORT)
    check("对照：公网域名正常抓取", res.get("ok") and res.get("text") == "PUBLIC_OK", res)

    HITS.update(internal=0)
    res = fetch("http://rebind.i131.invalid:%d/" % INTERNAL_PORT)
    check("DNS 重绑定（校验/建连两次解析）读不到内网",
          not leaked(res) and HITS["internal"] == 0, (res, HITS))
    HITS.update(internal=0)
    res = fetch("http://flip.i131.invalid:%d/" % INTERNAL_PORT)
    check("DNS 重绑定（首次公网、之后环回）读不到内网",
          not leaked(res) and HITS["internal"] == 0, (res, HITS))

    HITS.update(internal=0)
    res = fetch("http://multi.i131.invalid:%d/" % INTERNAL_PORT)
    check("多记录域名（公网 + 环回）整体拒绝",
          not res.get("ok") and HITS["internal"] == 0, (res, HITS))

# 「启动时已配代理」：子进程在 import 之前就带上代理变量
_proxy = "http://127.0.0.1:%d" % PROXY_PORT
_child_env = dict(os.environ, I131_PUBLIC_PORT=str(PUBLIC_PORT),
                  I131_PROXY_CHILD=json.dumps({
                      "inner": "http://inner.i131.invalid:%d/" % INTERNAL_PORT,
                      "nxdomain": "http://private.i131.invalid:%d/" % INTERNAL_PORT,
                      "public": "http://pub.i131.invalid:%d/via-env-proxy" % PUBLIC_PORT}))
_child_env.update({k: _proxy for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                                       "http_proxy", "https_proxy", "all_proxy")})
HITS.update(internal=0, proxy=0)
_child_env.pop("NO_PROXY", None)
_child_env.pop("no_proxy", None)
_child_env["PYTHONIOENCODING"] = "utf-8"
_proc = subprocess.run([sys.executable, os.path.abspath(__file__)], env=_child_env,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
_child_out = _proc.stdout.decode("utf-8", "replace")
_child = {}
for _line in _child_out.splitlines():
    if _line.startswith("I131_CHILD_RESULT "):
        _child = json.loads(_line[len("I131_CHILD_RESULT "):])
check("代理子进程正常跑完", _proc.returncode == 0 and _child, _child_out[-600:])
_ca, _cb, _cc = (_child.get(k) or {} for k in ("inner", "nxdomain", "public"))
check("启动即配代理：解析到内网的域名被拒",
      _child and not _ca.get("ok") and SENTINEL not in _ca.get("text", ""), _ca)
check("启动即配代理：解析不了的域名被拒（不再豁免交给代理）",
      _child and not _cb.get("ok") and SENTINEL not in _cb.get("text", ""), _cb)
check("启动即配代理：公网域名直连（不经环境代理）",
      _cc.get("text") == "PUBLIC_OK", _cc)
check("环境代理一次都没被用上，内网未被触达",
      HITS["proxy"] == 0 and HITS["internal"] == 0, HITS)

with mock.patch.object(socket, "getaddrinfo", fake_dns), \
        mock.patch.object(socket, "create_connection", fake_create_connection):
    HITS.update(internal=0)
    _pub_before = HITS["public"]
    for target in ("http://127.0.0.1:%d/" % INTERNAL_PORT,
                   "http://inner.i131.invalid:%d/" % INTERNAL_PORT,
                   "http://[::ffff:127.0.0.1]:%d/" % INTERNAL_PORT):
        res = fetch("http://pub.i131.invalid:%d/redir?to=%s"
                    % (PUBLIC_PORT, urllib.parse.quote(target, safe="")))
        check("重定向到 %s 被逐跳拦下" % target,
              not res.get("ok") and not leaked(res), res)
    check("重定向用例确实发生了第一跳（对照）",
          HITS["public"] - _pub_before == 3, HITS["public"] - _pub_before)
    check("重定向用例内网未被触达", HITS["internal"] == 0, HITS)

for ip in ("100.65.0.1", "100.127.255.254", "198.18.0.1", "0.0.0.0",
           "[::]", "[::ffff:127.0.0.1]", "[fe80::1]"):
    check("check_url 拒绝 %s" % ip, WT.check_url("http://%s/" % ip)[0] is False,
          WT.check_url("http://%s/" % ip))

_guard.stop()
for _srv in _servers:
    _srv.shutdown()
    _srv.server_close()
finish()

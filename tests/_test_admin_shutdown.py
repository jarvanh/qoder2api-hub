# -*- coding: utf-8 -*-
"""面板关闭网关（POST /admin/shutdown）：鉴权、闸门、真实停止、以及不死锁。

    python tests/_test_admin_shutdown.py

为什么单独测这几条：
  1. **鉴权层次**：这是唯一能「让网关消失」的端点，面板会话 + 本机 + 显式
     confirm 三层必须是与逻辑与，任一层缺失都要拒绝；
  2. **不死锁**：serve_forever 同线程调用 shutdown() 会死锁（#156）；
     ThreadingHTTPServer 的请求工作线程不同。此处独立线程延迟关闭，
     所以这里真的把服务关停并断言「响应先到、监听随后释放」；
  3. **不可误关**：`confirm` 必须是布尔 true（不是 1 / "true" / 任意真值），
     裸 POST 不能关掉网关；QD_SHUTDOWN_DISABLE=1 能一键禁用。
"""
import http.client
import http.server
import io
import json
import os
import sys
import tempfile
import threading
import time
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *          # noqa: F401,F403

_TMP = tempfile.mkdtemp(prefix="qd-shutdown-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["QD_PROXY_USAGE_DIR"] = os.path.join(_TMP, "usage")
os.environ.pop("QD_SHUTDOWN_DISABLE", None)
os.environ.pop("QD_SHUTDOWN_ALLOW_REMOTE", None)

P, A = bootstrap()
declare_suite(__file__, ["qoder_proxy.py", "dashboard.html", "docs/api.md",
                         "docs/deployment.md"])
import qoder_settings as ST        # noqa: E402

P.ACCOUNTS_DIR = os.environ["ACCOUNTS_DIR"]
P.API_KEY = ""
P.POOL = A.AccountPool(P.ACCOUNTS_DIR, log=lambda *a, **k: None)
P.PANEL = ST.PanelSessions(lambda: P.ACCOUNTS_DIR)
ST.set_panel_password(P.ACCOUNTS_DIR, "fixture-panel-password")
TOKEN = P.PANEL.create()


class _Quiet(object):
    def log_message(self, *_):
        pass


class Gateway(_Quiet, P.Handler):
    pass


def make_server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
    srv.daemon_threads = True
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05},
                              daemon=True)
    thread.start()
    return srv, thread, srv.server_address[1]


def post(port, path, body, token=None, timeout=15):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["X-Panel-Token"] = token
    conn.request("POST", path, body=json.dumps(body).encode("utf-8"),
                 headers=headers)
    r = conn.getresponse()
    raw = r.read()
    conn.close()
    try:
        return r.status, json.loads(raw.decode("utf-8"))
    except Exception:
        return r.status, {"raw": raw[:200]}


def get(port, path, token=None, timeout=15):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    headers = {}
    if token is not None:
        headers["X-Panel-Token"] = token
    conn.request("GET", path, headers=headers)
    r = conn.getresponse()
    raw = r.read()
    conn.close()
    try:
        return r.status, json.loads(raw.decode("utf-8"))
    except Exception:
        return r.status, {"raw": raw[:200]}


def accepts(port, timeout=2):
    """端口是否还在接受连接（停止后应当拒绝）。"""
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        conn.request("GET", "/ping")
        conn.getresponse().read()
        conn.close()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 1) 闸门
print("[1] 鉴权与闸门（服务保持运行）")
srv, thread, port = make_server()
P._ACTIVE_SERVER = srv

st, body = post(port, "/admin/shutdown", {"confirm": True}, token=None)
check("无面板会话 -> 401", st == 401, (st, body))
check("无会话时没有关掉任何东西", thread.is_alive() and accepts(port))

st, body = post(port, "/admin/shutdown", {"confirm": True}, token="bogus-token")
check("伪造 token -> 401", st == 401, (st, body))
check("伪造 token 没有关停", thread.is_alive() and accepts(port))

for label, payload in (("缺少 confirm -> 400", {}),
                       ("confirm=1（非布尔）-> 400", {"confirm": 1}),
                       ('confirm="true"（字符串）-> 400', {"confirm": "true"}),
                       ("confirm 任意真值都不算 -> 400", {"confirm": [True]})):
    st, body = post(port, "/admin/shutdown", payload, token=TOKEN)
    check(label, st == 400, (st, body))
check("confirm 不合法时服务仍在", thread.is_alive() and accepts(port))

with mock.patch.object(P, "SHUTDOWN_DISABLED", True):
    st, body = post(port, "/admin/shutdown", {"confirm": True}, token=TOKEN)
    check("QD_SHUTDOWN_DISABLE=1 -> 403", st == 403, (st, body))
    check("禁用时服务仍在", thread.is_alive() and accepts(port))
    info = P.shutdown_plan_info()
    check("禁用时 plan.can_shutdown=False 且给出原因",
          info["can_shutdown"] is False and info["blocked_reasons"], info)
    check("禁用原因的 code 可供前端翻译",
          info["blocked_codes"] == ["disabled"], info)

with mock.patch.object(P, "_is_loopback", lambda host: False):
    st, body = post(port, "/admin/shutdown", {"confirm": True}, token=TOKEN)
    check("非本机 -> 403（默认 loopback-only）", st == 403, (st, body))
    check("非本机被拒后服务仍在", thread.is_alive() and accepts(port))
    with mock.patch.dict(os.environ, {"QD_SHUTDOWN_ALLOW_REMOTE": "1"}):
        check("QD_SHUTDOWN_ALLOW_REMOTE=1 时放开非本机闸门",
              P.shutdown_plan_info()["allow_remote"] is True)

# plan 里必须如实反映「可执行性」，面板据此决定是否画按钮（#132 的教训）
info = P.shutdown_plan_info()
check("持有监听器时 plan.can_shutdown=True",
      info["can_shutdown"] is True and not info["blocked_reasons"], info)
check("plan 带重启提示", bool(info["restart_hint"]), info)
st, settings = get(port, "/settings", token=TOKEN)
check("GET /settings 回传 shutdown 块",
      st == 200 and isinstance(settings.get("shutdown"), dict)
      and settings["shutdown"].get("can_shutdown") is True,
      (st, settings.get("shutdown")))

srv.shutdown()
srv.server_close()
thread.join(3)
check("闸门阶段结束：服务已收尾", not thread.is_alive())

# 幂等：连点 / 并发都只允许起一次关停，第二次起返回 False
P._SHUTDOWN_STARTED = False
srv_idem, thread_idem, port_idem = make_server()
P._ACTIVE_SERVER = srv_idem
check("首次 schedule_shutdown 返回 True", P.schedule_shutdown(delay=0.05) is True)
check("重复调用被去重（不否认已开始）", P.schedule_shutdown(delay=0.05) is False)
thread_idem.join(3)
check("去重后服务仍按预期停止", not thread_idem.is_alive())
srv_idem.server_close()
P._SHUTDOWN_STARTED = False

# ---------------------------------------------------------------- 2) 真实关停
print("[2] 真实关停：响应先到，监听随后释放")
srv2, thread2, port2 = make_server()
P._ACTIVE_SERVER = srv2
started = time.time()
st, body = post(port2, "/admin/shutdown", {"confirm": True}, token=TOKEN,
                timeout=10)
elapsed = time.time() - started
check("合法请求 -> 200 stopping=true",
      st == 200 and body.get("ok") is True and body.get("stopping") is True,
      (st, body))
check("响应没有被关闭流程拖住（< 3s）", elapsed < 3.0, elapsed)
check("200 里带重启提示", bool(body.get("restart_hint")), body)
deadline = time.monotonic() + 5
while thread2.is_alive() and time.monotonic() < deadline:
    time.sleep(0.02)
check("serve_forever 真的退出了（不是只返回 200）", not thread2.is_alive())
check("监听端口已释放", not accepts(port2, timeout=1))

# 没有监听器时 schedule_shutdown 必须返回 False（不能凭空报成功）：
# 直接测函数而不是再打一次已关闭的端口——Windows 上连已释放端口会挂到超时。
P._ACTIVE_SERVER = None
P._SHUTDOWN_STARTED = False
check("无监听器时 schedule_shutdown 返回 False", P.schedule_shutdown() is False)
_no_listener_plan = P.shutdown_plan_info()
check("plan 在没有监听器时如实不可执行",
      _no_listener_plan["can_shutdown"] is False)
check("无监听器原因的 code 可供前端翻译",
      _no_listener_plan["blocked_codes"] == ["no_listener"],
      _no_listener_plan)
srv2.server_close()

# ---------------------------------------------------------------- 3) 源码接线
print("[3] 源码接线（main 必须登记监听器，前端必须留有入口）")
src = io.open(os.path.join(ROOT, "qoder_proxy.py"), encoding="utf-8").read()
check("main 把 server 登记为 _ACTIVE_SERVER",
      "global _ACTIVE_SERVER" in src and "_ACTIVE_SERVER = server" in src)
check("关停走独立线程（避免阻塞请求响应）",
      "threading.Thread(target=_run, name=\"qd-shutdown\"" in src)
check("延迟关闭在 shutdown 之前 sleep（先让响应发出去）",
      "time.sleep(delay)" in src and "server.shutdown()" in src)
check("POST 路由在 admin 鉴权之后才处理",
      'path.startswith("/admin/")' in src and '"/admin/shutdown"' in src)

dash = io.open(os.path.join(ROOT, "dashboard.html"), encoding="utf-8").read()
check("面板有关闭网关按钮", 'id="btnShutdown"' in dash
      and 'onclick="shutdownGateway(this)"' in dash)
check("面板调用 /admin/shutdown 并带 confirm:true",
      "postJSON('/admin/shutdown'" in dash and 'confirm: true' in dash)
check("面板在不可执行时隐藏按钮（can_shutdown 门控）",
      "info.can_shutdown" in dash and "blocked_reasons" in dash)
check("面板会停掉轮询避免向已停进程刷请求",
      "PANEL_TIMERS || []" in dash and "clearInterval" in dash)

api = io.open(os.path.join(ROOT, "docs", "api.md"), encoding="utf-8").read()
check("docs/api.md 记录了 /admin/shutdown", "/admin/shutdown" in api)
conf = io.open(os.path.join(ROOT, "docs", "deployment.md"),
               encoding="utf-8").read()
check("docs/deployment.md 记录了 QD_SHUTDOWN_DISABLE",
      "QD_SHUTDOWN_DISABLE" in conf)
check("docs/deployment.md 记录了 QD_SHUTDOWN_ALLOW_REMOTE",
      "QD_SHUTDOWN_ALLOW_REMOTE" in conf)

finish()
# -*- coding: utf-8 -*-
"""安全加固套件：默认口令 / 凭据落盘权限 / HTTP 监听上限。

    python tests/_test_security_hardening.py

覆盖三处修复：
  Q1  首次启动不再接受 "admin"：随机口令 + 0600 文件 + 环境变量覆盖 + 显式 reset
  Q2  settings.json / 账号 JSON / 机器身份缓存均为 0600 原子写，目录 0700
  Q3  连接数上限（超限 503）、socket 超时（空连接被关闭）、请求体上限（413）
"""
import os
import socket
import stat
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# The payload limit is read at import time; keep the oversize test cheap.
os.environ["QD_MAX_PAYLOAD_BYTES"] = "1024"
os.environ["ACCOUNTS_DIR"] = tempfile.mkdtemp(prefix="qd-sec-")
os.environ["QD_PROXY_USAGE_DIR"] = os.path.join(
    os.environ["ACCOUNTS_DIR"], "usage")

from _suite_head import *          # noqa: F401,F403

P, A = bootstrap()
import qoder_settings as ST       # noqa: E402

declare_suite(__file__, ["qoder_settings.py", "qoder_accounts.py",
                         "qoder_proxy.py"])


def mode_of(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def free_port_server(handler=None, max_connections=4):
    srv = P.BoundedThreadingHTTPServer(
        ("127.0.0.1", 0), handler or P.Handler,
        max_connections=max_connections)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


# ---------------------------------------------------------------- Q1
print("[Q1] 默认口令 admin 不再生效，首启随机口令")
d = tempfile.mkdtemp(prefix="qd-pwd-")
check("全新目录：admin 不能登录", not ST.verify_panel_password(d, "admin"))
check("全新目录：标记为未配置口令", ST.panel_password_is_default(d))

pwd, created, source = ST.ensure_panel_password(d)
check("首启生成了随机口令", created and source == "created" and bool(pwd), 
      (created, source, pwd))
check("随机口令不是 admin 且足够长", pwd != "admin" and len(pwd) >= 16,
      pwd)
check("随机口令可登录", ST.verify_panel_password(d, pwd))
check("引导后 admin 仍被拒绝", not ST.verify_panel_password(d, "admin"))
check("引导后不再标记为默认口令", not ST.panel_password_is_default(d))

settings_json = os.path.join(d, "settings.json")
pw_file = ST.panel_password_file(d)
check("settings.json 权限 0600", mode_of(settings_json) == 0o600,
      oct(mode_of(settings_json)))
check("accounts 目录权限 0700", mode_of(d) == 0o700, oct(mode_of(d)))
check("口令文件存在且权限 0600",
      os.path.exists(pw_file) and mode_of(pw_file) == 0o600,
      (pw_file, mode_of(pw_file) if os.path.exists(pw_file) else None))
try:
    with open(pw_file, encoding="utf-8") as fh:
        check("口令文件内容与生成的随机口令一致", fh.read().strip() == pwd)
except OSError as exc:
    check("口令文件内容与生成的随机口令一致", False, exc)

# 环境变量覆盖（容器场景）：不落明文口令文件，admin 依旧无效
d_env = tempfile.mkdtemp(prefix="qd-pwd-env-")
os.environ[ST.PANEL_PASSWORD_ENV] = "env-strong-pass-123"
try:
    env_pwd, env_created, env_source = ST.ensure_panel_password(d_env)
finally:
    os.environ.pop(ST.PANEL_PASSWORD_ENV, None)
check("环境变量覆盖生效",
      env_source == "env" and env_pwd == "env-strong-pass-123" and not env_created,
      (env_pwd, env_source))
check("环境变量口令可登录且 admin 无效",
      ST.verify_panel_password(d_env, "env-strong-pass-123")
      and not ST.verify_panel_password(d_env, "admin"))

# 显式 reset：旧口令立即失效，新口令写入 0600 文件
old_pwd = pwd
new_pwd, new_path = ST.reset_panel_password(d)
check("显式 reset 换出新口令", new_pwd != old_pwd and bool(new_path))
check("旧口令失效、新口令有效",
      not ST.verify_panel_password(d, old_pwd)
      and ST.verify_panel_password(d, new_pwd))
check("reset 后的口令文件权限 0600",
      os.path.exists(new_path) and mode_of(new_path) == 0o600,
      (new_path, mode_of(new_path) if os.path.exists(new_path) else None))

try:
    ST.set_panel_password(d, "")
    check("显式设置空口令被拒绝", False)
except ValueError:
    check("显式设置空口令被拒绝", True)

# ---------------------------------------------------------------- Q2
print("[Q2] 凭据落盘 0600 / 原子写")
acc_dir = tempfile.mkdtemp(prefix="qd-acc-")
acc = A.Account({"uid": "sec-acct", "realm": "cn",
                 "accessToken": "dt-secret-token"})
acc_path = acc.save(acc_dir)
check("账号 JSON 权限 0600", mode_of(acc_path) == 0o600,
      oct(mode_of(acc_path)))
check("账号目录权限 0700", mode_of(acc_dir) == 0o700, oct(mode_of(acc_dir)))
check("没有遗留 .tmp 文件", not os.path.exists(acc_path + ".tmp"))
with open(acc_path, encoding="utf-8") as fh:
    check("账号令牌确实写入（内容完整）",
          json.load(fh).get("accessToken") == "dt-secret-token")

id_dir = tempfile.mkdtemp(prefix="qd-ident-")
_prev_accounts = os.environ.get("ACCOUNTS_DIR")
os.environ["ACCOUNTS_DIR"] = id_dir
try:
    ok = A._write_identity_cache_doc({"version": 1, "realm": {}})
    id_path = A.machine_identity_cache_path()
finally:
    if _prev_accounts is None:
        os.environ.pop("ACCOUNTS_DIR", None)
    else:
        os.environ["ACCOUNTS_DIR"] = _prev_accounts
check("机器身份缓存写入成功", ok, id_path)
check("机器身份缓存权限 0600",
      os.path.exists(id_path) and mode_of(id_path) == 0o600,
      (id_path, mode_of(id_path) if os.path.exists(id_path) else None))

# ---------------------------------------------------------------- Q3
print("[Q3] 连接上限 / socket 超时 / 请求体上限")

# 连接数上限：占满唯一槽位后，新连接收到 503
srv, port = free_port_server(max_connections=1)
idle = socket.create_connection(("127.0.0.1", port), timeout=5)
_deadline = time.time() + 5
while getattr(srv, "_connection_slots")._value != 0 and time.time() < _deadline:
    time.sleep(0.01)
check("空闲连接占满连接槽位", srv._connection_slots._value == 0,
      srv._connection_slots._value)
over = socket.create_connection(("127.0.0.1", port), timeout=5)
over.settimeout(5)
over.sendall(b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n")
try:
    reply = over.recv(200)
except OSError as exc:
    reply = b"<recv failed: %s>" % str(exc).encode()
check("超过连接上限立即 503", reply.startswith(b"HTTP/1.1 503"),
      reply[:80])
over.close()
idle.close()
srv.shutdown()
srv.server_close()

# socket 超时：连上不发送任何数据，服务端应在超时后主动关闭
class _IdleHandler(P.Handler):
    timeout = 0.4


srv2, port2 = free_port_server(handler=_IdleHandler, max_connections=4)
sock = socket.create_connection(("127.0.0.1", port2), timeout=5)
sock.settimeout(5)
_started = time.time()
try:
    tail = sock.recv(100)
except (ConnectionResetError, socket.timeout):
    tail = b""
_elapsed = time.time() - _started
check("空连接被 socket 超时关闭", tail == b"" and _elapsed < 3.0,
      (_elapsed, tail))
sock.close()
srv2.shutdown()
srv2.server_close()

# 请求体上限：Content-Length 超过 QD_MAX_PAYLOAD_BYTES -> 413 且关闭连接
srv3, port3 = free_port_server(max_connections=4)
body = b"{}"
req = (b"POST /panel/login HTTP/1.1\r\n"
       b"Host: x\r\n"
       b"Content-Type: application/json\r\n"
       b"Content-Length: 2048\r\n"
       b"Connection: keep-alive\r\n\r\n" + body)
sock3 = socket.create_connection(("127.0.0.1", port3), timeout=5)
sock3.settimeout(5)
sock3.sendall(req)
_buf = b""
_closed_after_body = False
try:
    while len(_buf) < 65536:
        chunk = sock3.recv(4096)
        if not chunk:
            _closed_after_body = True
            break
        _buf += chunk
except socket.timeout:
    pass
check("超限请求体返回 413", b" 413 " in _buf.split(b"\r\n", 1)[0],
      _buf.split(b"\r\n", 1)[0])
check("超限请求体后关闭连接（不把残余体当下一请求）",
      _closed_after_body)
sock3.close()
srv3.shutdown()
srv3.server_close()

finish()

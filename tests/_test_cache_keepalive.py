# -*- coding: utf-8 -*-
"""会话缓存保温（QD_CACHE_KEEPALIVE）：登记/巡检/淘汰行为 + 网关接线检查。

背景：上游隐式前缀缓存实测 ~5 分钟闲置过期（配套 issue）。保温线程在会话
闲置超过 QD_CACHE_KEEPALIVE_INTERVAL 后，用登记的同 payload 重发
stream=False、max_tokens=1 的最小请求为缓存续期（消耗 ≈ 一次缓存读）。
"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *          # noqa: F401,F403

P, A = bootstrap(env={"QD_CACHE_KEEPALIVE": "1"})
declare_suite(__file__, ["qoder_proxy.py", "qoder_keepalive.py",
                         "docs/configuration.md"])

import qoder_keepalive as K

print("[keepalive] 配置默认值")
check("默认间隔 240s（小于上游 ~5min TTL）", K.interval() == 240)
check("默认最长保温 1800s", K.max_age() == 1800)
check("默认最多 2 个会话", K.max_convs() == 2)

# 固定小参数便于测试（bootstrap 已设 QD_CACHE_KEEPALIVE=1）
os.environ["QD_CACHE_KEEPALIVE_INTERVAL"] = "60"
os.environ["QD_CACHE_KEEPALIVE_MAX_AGE"] = "120"
os.environ["QD_CACHE_KEEPALIVE_MAX_CONVS"] = "2"
check("开启后 enabled()", K.enabled() is True)
check("间隔读到 60s", K.interval() == 60)

print("[keepalive] 登记 / 深拷贝 / 状态")
K.stop_loop()
with K._LOCK:
    K._ENTRIES.clear()
payload = {"model": "Qwen3.8-Flash", "stream": True,
           "messages": [{"role": "user", "content": "hello"}]}
check("observe 登记成功", K.observe(payload, "win:aaa", "uid-1", "cn",
                                    "Qwen3.8-Flash") is True)
st = K.status()
check("状态里有 1 条", len(st["entries"]) == 1)
check("entry 记录 session 前缀", st["entries"][0]["session_id"].startswith("win:aaa"))
payload["stream"] = False   # 改原对象，不应影响登记的快照
check("payload 深拷贝（快照不受原对象影响）",
      K._ENTRIES["win:aaa"]["payload"].get("stream") is True)
os.environ["QD_CACHE_KEEPALIVE"] = "0"
check("关闭开关时 observe 不登记",
      K.observe(payload, "win:off", "uid-1", "cn", "m") is False
      and "win:off" not in K._ENTRIES)
os.environ["QD_CACHE_KEEPALIVE"] = "1"

print("[keepalive] tick：未到期不发包、到期发包、超龄淘汰")
calls = []


def fake_sender(entry):
    calls.append(dict(entry))
    return {"prompt_tokens": 10, "cached_tokens": 10}


K.configure(fake_sender)
at_a = K._ENTRIES["win:aaa"]["at"]
r = K.tick(now=at_a + 30)                       # 30s < 60s 间隔
check("未到期：不发送", r["sent"] == 0 and not calls)
r = K.tick(now=at_a + 61)                       # >= 60s
check("到期：发送一次", r["sent"] == 1 and len(calls) == 1)
e = calls[0]
check("sender 收到完整 entry（含 session/account/realm/model）",
      e["account_uid"] == "uid-1" and e["realm"] == "cn"
      and e["session_id"] == "win:aaa" and e["model"] == "Qwen3.8-Flash")
check("发送后 pings=1", K._ENTRIES["win:aaa"]["pings"] == 1)
r = K.tick(now=K._ENTRIES["win:aaa"]["at"] + 30)
check("续期后不再发送", r["sent"] == 0)
r = K.tick(now=K._ENTRIES["win:aaa"]["at"] + 121)
check("超龄：淘汰且不发送", r["stale"] == 1 and r["sent"] == 0
      and "win:aaa" not in K._ENTRIES)

print("[keepalive] LRU 上限 + 失败淘汰")
K.observe({"model": "m", "messages": []}, "win:b", "uid-1", "cn", "m")
K.observe({"model": "m", "messages": []}, "win:c", "uid-1", "cn", "m")
K.observe({"model": "m", "messages": []}, "win:d", "uid-1", "cn", "m")
check("超过 max_convs=2：最旧的 win:b 被淘汰",
      "win:b" not in K._ENTRIES and "win:c" in K._ENTRIES
      and "win:d" in K._ENTRIES)


def boom(entry):
    raise RuntimeError("upstream down")


K.configure(boom)
with K._LOCK:
    K._ENTRIES.clear()          # 隔离：上一步的 win:c/win:d 也已同时到期
K.observe({"model": "m", "messages": []}, "win:boom", "uid-1", "cn", "m")
at0 = K._ENTRIES["win:boom"]["at"]
r = K.tick(now=at0 + 61)
check("sender 异常：会话被淘汰且计 failed", r["failed"] == 1
      and "win:boom" not in K._ENTRIES)
check("tick 永不抛异常（空表再 tick 返回 dict）", isinstance(K.tick(), dict))

print("[keepalive] qoder_proxy 接线")
_src = io.open(os.path.join(ROOT, "qoder_proxy.py"), encoding="utf-8").read()
check("import qoder_keepalive", "import qoder_keepalive" in _src)
check("open_upstream 成功路径登记保温", "qoder_keepalive.observe(" in _src)
_i = _src.index("qoder_keepalive.observe(")
check("只登记真实流量（排除 only_account 测试/探测路径）",
      "only_account is None" in _src[_i - 300:_i])
check("observe 在 note_success 之后（成功才登记）",
      "note_success" in _src[_i - 500:_i])
check("发送函数存在", "def _keepalive_send(entry):" in _src)
_j = _src.index("def _keepalive_send(entry):")
_body = _src[_j:_src.index("def main(", _j)]
check("保温请求 max_tokens=1", 'payload["max_tokens"] = 1' in _body)
check("保温请求非流式", 'payload["stream"] = False' in _body)
check("同会话复用（session_key=登记的 session_id）",
      'session_key=entry["session_id"]' in _body)
check("保温记账 key_id=keepalive", '"keepalive"' in _body)
check("主循环按需启动", "qoder_keepalive.configure(_keepalive_send)" in _src
      and "qoder_keepalive.start_loop()" in _src)
_m = _src.index("qoder_keepalive.configure(")
check("启动点在 POOL 初始化之后",
      _src.index("qoder_accounts.AccountPool(") < _m)
check("网关模块可正常 import（语法/接线无破坏）", P is not None
      and callable(P._keepalive_send))

finish()

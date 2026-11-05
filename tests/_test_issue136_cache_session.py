"""#136 prompt 缓存 98%↔0% 抖动：session_id 与「一次提问」解耦。"""
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *          # noqa: F401,F403

P, A = bootstrap()
declare_suite(__file__, ["qoder_proxy.py"])

_src = io.open(os.path.join(ROOT, "qoder_proxy.py"), encoding="utf-8").read()


def hist(first="hi", turns=0, system="sys"):
    msgs = [{"role": "system", "content": system},
            {"role": "user", "content": first}]
    for i in range(turns):
        msgs.append({"role": "assistant", "content": "ok%d" % i})
        msgs.append({"role": "user", "content": "turn%d" % i})
    return msgs


print("[#136] 用量归组 vs 缓存会话：两个 id 分开")
P._CONVERSATIONS.clear()
a = P.conversation_identity({"model": "m", "messages": hist()})
b = P.conversation_identity({"model": "m", "messages": hist(turns=1)})
check("同一窗口 -> window_id 相同（session_id 稳定、缓存能命中）",
      a["window_id"] == b["window_id"], (a["window_id"], b["window_id"]))
check("新提问 -> id 不同（用量仍按一次对话归组）", a["id"] != b["id"])
check("返回值同时带 id/window_id/begin_at",
      set(("id", "window_id", "begin_at")).issubset(a.keys()), list(a.keys()))

print("[#136] 上游缓存键：system 每轮变化也不能影响窗口键")
P._CONVERSATIONS.clear()
c1 = P.conversation_identity({"model": "m", "messages": hist(system="cwd=/a")})
c2 = P.conversation_identity({"model": "m", "messages": hist(system="cwd=/b time=1")})
check("system 变了 window_id 仍然相同", c1["window_id"] == c2["window_id"],
      (c1["window_id"], c2["window_id"]))

print("[#136] 账号亲和键同样只认首条 user")
k1 = P.derive_affinity_key(hist(system="cwd=/a"))
k2 = P.derive_affinity_key(hist(system="cwd=/b"))
check("system 变化不影响亲和键（不会因上下文变动换号）", k1 == k2, (k1, k2))
k3 = P.derive_affinity_key(hist(first="另一个问题", system="cwd=/a"))
check("不同首条提问 -> 不同亲和键（负载仍分散）", k1 != k3)
check("亲和键按前缀命名", str(k1).startswith("pfx-"), k1)

print("[#136] session_id 兜底用 window_id")
check("assign_prefix_session 传的是 window_id",
      'fallback_session_id=((_conv or {}).get("window_id")' in _src)
check("不再用「一次提问」的 id 兜底",
      'fallback_session_id=(_conv or {}).get("id")' not in _src)
_acc = A.Account({"uid": "u136", "realm": "cn", "accessToken": "t"})
_conv = P.conversation_identity({"model": "m", "messages": hist()})
_b1 = P.build_qoder_body({"messages": hist()}, _acc, "dfmodel", "cn",
                         conversation=_conv)
P.assign_prefix_session(_b1, _acc, "dfmodel",
                        fallback_session_id=_conv["window_id"])
check("session_id == window_id", _b1["session_id"] == _conv["window_id"])
check("business.id == 提问 id（归组不受影响）",
      _b1["business"]["id"] == _conv["id"])
_b2 = P.build_qoder_body({"messages": hist(turns=1)}, _acc, "dfmodel", "cn",
                         conversation=P.conversation_identity(
                             {"model": "m", "messages": hist(turns=1)}))
P.assign_prefix_session(_b2, _acc, "dfmodel",
                        fallback_session_id=P.conversation_identity(
                            {"model": "m", "messages": hist(turns=1)})["window_id"])
check("下一轮提问：business.id 变了（两条记录）",
      _b1["business"]["id"] != _b2["business"]["id"])
check("下一轮提问：session_id 不变（缓存继续命中）",
      _b1["session_id"] == _b2["session_id"],
      (_b1["session_id"], _b2["session_id"]))

print("[#136] 三段拆分列能直接显示这类全价行")
_sp = P.split_credit(200000, 0, 100)
check("零缓存长 prompt：输入段占绝对多数",
      _sp["input"] > _sp["cached"] and _sp["input"] > _sp["output"], _sp)
_sp2 = P.split_credit(200000, 190000, 100)
check("高命中：缓存段把输入段压下去",
      _sp2["cached"] > _sp2["input"], _sp2)

finish()

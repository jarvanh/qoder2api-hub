"""#128 一次对话被拆成 N 条：上游会话身份稳定化。"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *          # noqa: F401,F403

P, A = bootstrap()
declare_suite(__file__, ["qoder_proxy.py"])


def history(tag, n=3):
    return [{"role": "system", "content": "you are a coding agent"},
            {"role": "user", "content": "帮我重构 %s 这个模块" % tag},
            {"role": "assistant", "content": "好的"}][:n]


print("[#128] 显式会话键：同一会话复用同一身份")
P._CONVERSATIONS.clear()
a = P.conversation_identity({"messages": history("A")}, "sess-1")
b = P.conversation_identity({"messages": history("A")}, "sess-1")
check("同窗口同提问 -> id 相同", a["id"] == b["id"], (a["id"], b["id"]))
check("begin_at 相同（对话时长连续）", a["begin_at"] == b["begin_at"])
_next_q = history("A") + [{"role": "assistant", "content": "ok"},
                          {"role": "user", "content": "再改一处"}]
a2 = P.conversation_identity({"messages": _next_q}, "sess-1")
check("同窗口但新提问 -> 新 id（面板能看单次时长）", a["id"] != a2["id"])
c = P.conversation_identity({"messages": history("A")}, "sess-2")
check("不同窗口 id 不同", a["id"] != c["id"])

print("[#128] 无显式键：用首条 user 消息 + 系统提示 + 模型退化")
P._CONVERSATIONS.clear()
d1 = P.conversation_identity({"model": "gpt-5", "messages": history("X")})
d2 = P.conversation_identity({"model": "gpt-5", "messages": history("X")})
check("同一对话（历史被追加）仍命中同一条", d1["id"] == d2["id"])
# 工具步进（追加 tool/assistant，不追加 user）仍属同一条
step = history("X") + [{"role": "assistant", "content": ""},
                      {"role": "tool", "content": "结果"}]
d3 = P.conversation_identity({"model": "gpt-5", "messages": step})
check("同一次提问内的工具步进仍归同一条", d1["id"] == d3["id"])
# 用户再次提问 -> 新一条（#128 后续：按「一次对话」切分，而非整个窗口）
grow = history("X") + [{"role": "assistant", "content": "ok"},
                       {"role": "user", "content": "继续"}]
d3b = P.conversation_identity({"model": "gpt-5", "messages": grow})
check("用户下一次提问 -> 新会话 id", d1["id"] != d3b["id"])
d4 = P.conversation_identity({"model": "gpt-5", "messages": history("Y")})
check("不同对话不合并", d1["id"] != d4["id"])
d5 = P.conversation_identity({"model": "other-model", "messages": history("X")})
check("换模型算新对话", d1["id"] != d5["id"])

print("[#128] 拿不到任何线索时保持旧行为")
P._CONVERSATIONS.clear()
check("空消息 -> None（不瞎并）",
      P.conversation_identity({"messages": []}) is None)
check("非 dict -> None", P.conversation_identity(None) is None)

print("[#128] 关闭开关后完全回到旧行为")
_orig = P.CONVERSATION_SHARE
P.CONVERSATION_SHARE = False
check("QD_CONVERSATION_SHARE=0 -> None",
      P.conversation_identity({"messages": history("Z")}, "sess-z") is None)
P.CONVERSATION_SHARE = _orig

print("[#128] 出站请求体：business.id / begin_at / session_id 都稳定")
P._CONVERSATIONS.clear()
acc = A.Account({"uid": "u128", "realm": "cn", "accessToken": "t"})
conv = P.conversation_identity({"messages": history("B")}, "sess-b")
b1 = P.build_qoder_body({"messages": history("B")}, acc, "dfmodel", "cn",
                        conversation=conv)
b2 = P.build_qoder_body({"messages": history("B")}, acc, "dfmodel", "cn",
                        conversation=conv)
check("business.id 一致", b1["business"]["id"] == b2["business"]["id"])
check("business.id 就是会话 id", b1["business"]["id"] == conv["id"])
check("begin_at 一致", b1["business"]["begin_at"] == b2["business"]["begin_at"])
check("begin_at 是会话起点（不是每请求刷新）",
      b1["business"]["begin_at"] == conv["begin_at"])
check("request_id 仍然每请求唯一（不能并成一条请求）",
      b1["request_id"] != b2["request_id"])
check("chat_record_id 也稳定（面板按它归组「一条对话」）",
      b1["chat_record_id"] == b2["chat_record_id"] == conv["id"],
      (b1["chat_record_id"], b2["chat_record_id"]))
P.assign_prefix_session(b1, acc, "dfmodel", client_session_key="sess-b",
                        fallback_session_id=conv["id"])
P.assign_prefix_session(b2, acc, "dfmodel", client_session_key="sess-b",
                        fallback_session_id=conv["id"])
check("无前缀缓存命中时 session_id 也用会话 id",
      b1["session_id"] == b2["session_id"] == conv["id"],
      (b1["session_id"], b2["session_id"]))

print("[#128] 没有会话身份时逐请求唯一（不误并）")
b3 = P.build_qoder_body({"messages": history("C")}, acc, "dfmodel", "cn")
b4 = P.build_qoder_body({"messages": history("C")}, acc, "dfmodel", "cn")
check("business.id 各自唯一", b3["business"]["id"] != b4["business"]["id"])
check("chat_record_id 也各自唯一", b3["chat_record_id"] != b4["chat_record_id"])
# 毫秒级同刻会取到同一个时间戳（不是 bug），这里只断言「不是复用会话起点」
check("begin_at 不是复用会话起点（取当前时间）",
      b3["business"]["begin_at"] <= b4["business"]["begin_at"])

print("[#128] LRU 上限与过期")
P._CONVERSATIONS.clear()
for i in range(P._CONV_MAX_ENTRIES + 20):
    P.conversation_identity({"messages": history("k%d" % i)}, "s%d" % i)
check("entries 不超过上限", len(P._CONVERSATIONS) <= P._CONV_MAX_ENTRIES,
      len(P._CONVERSATIONS))
P._CONVERSATIONS.clear()
k = P.conversation_identity({"messages": history("T")}, "ttl")
# 会话键现在是「显式键|提问摘要」，直接取唯一那条来改时间
_only_key = list(P._CONVERSATIONS)[0]
P._CONVERSATIONS[_only_key]["at"] = time.time() - P._CONV_TTL_SECS - 1
k2 = P.conversation_identity({"messages": history("T")}, "ttl")
check("超过 TTL 重新分配（不无限延用旧会话）", k["id"] != k2["id"])

finish()

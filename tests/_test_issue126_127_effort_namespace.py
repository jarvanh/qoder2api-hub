"""#126 思考档位 clamp 双向化 + #127 namespace 工具展开/回程还原 + additional_tools 合并。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *          # noqa: F401,F403

P, A = bootstrap()
declare_suite(__file__, ["qoder_anthropic.py", "qoder_proxy.py"])
import qoder_anthropic as AN       # noqa: E402

SUP3 = ["low", "high", "max"]
META3 = {"thinking_config": {"enabled": {"efforts": {
    "low": {}, "high": {}, "max": {"is_default": True}}}}}

print("[#126] budget_tokens -> 档位：双向就近（同距偏默认档）")
cases = [
    (1024, "low"), (2048, "low"), (4096, "low"),
    (8192, "high"), (16384, "high"), (20000, "high"),
    (31999, "high"),   # ultrathink 落在 high 边界内（issue #126 表格）
    (40000, "max"),
]
for budget, want in cases:
    got = AN.effort_from_anthropic({"thinking": {"type": "enabled",
                                                 "budget_tokens": budget}},
                                   supported=SUP3, catalog_meta=META3)
    check("budget=%d -> %s" % (budget, want), got == want, got)

print("[#126] 与 output_config.effort 路径结论一致")
for lvl in ("minimal", "low", "medium", "high", "max"):
    a = AN.effort_from_anthropic({"thinking": {"type": "enabled", "effort": lvl}},
                                 supported=SUP3, catalog_meta=META3)
    b, _ = P.normalize_reasoning_effort(lvl, META3)
    check("档位 %s 两条路径一致（%s）" % (lvl, b), a == b, (a, b))

print("[#126] 合法档位原样透传 / 关闭仍不下发")
check("low 原样", AN.effort_from_anthropic(
    {"thinking": {"type": "enabled", "budget_tokens": 2048}},
    supported=SUP3, catalog_meta=META3) == "low")
check("disabled 返回 None", AN.effort_from_anthropic(
    {"thinking": {"type": "disabled"}}, supported=SUP3, catalog_meta=META3) is None)
check("空档位表不下发", AN.effort_from_anthropic(
    {"thinking": {"type": "enabled", "budget_tokens": 2048}},
    supported=[], catalog_meta={}) is None)

print("[#127 F1] namespace 展开")
ns_tool = {"type": "namespace", "name": "mcp__github", "tools": [
    {"type": "function", "name": "create_issue",
     "description": "open an issue", "parameters": {"type": "object"}},
    {"name": "read_file", "description": "no type field", "parameters": {}},
    {"type": "custom", "name": "exec", "description": "freeform"},
]}
req = {"model": "gpt-5", "input": "hi",
       "tools": [{"type": "function", "name": "top_tool", "parameters": {}}, ns_tool]}
chat = P.responses_to_chat(req)
names = [P._tool_declared_name(t) for t in chat.get("tools") or []]
check("容器内三条 + 顶层一条都活着",
      names == ["top_tool", "create_issue", "read_file", "exec"], names)
check("namespace 映射记下来了",
      chat.get("_qd_tool_ns") == {"create_issue": "mcp__github",
                                  "read_file": "mcp__github",
                                  "exec": "mcp__github"},
      chat.get("_qd_tool_ns"))
check("出站没有任何 namespace 类型（不会触发 #63 的 400）",
      all((t.get("type") or "function") != "namespace"
          for t in chat.get("tools") or []))
check("custom 降级为 function（exec 可被上游表达）",
      any(P._tool_declared_name(t) == "exec" for t in chat.get("tools") or []))

print("[#127 F1] custom 判型递归进容器")
customs = P.custom_tool_names([ns_tool])
check("容器内的 custom 被识别（否则回程判型错、调用不执行）",
      customs == {"exec"}, customs)

print("[#127 F1] 重名策略 + 深度上限")
dup_req = {"model": "gpt-5", "input": "hi", "tools": [
    {"type": "namespace", "name": "a", "tools": [{"type": "function", "name": "dup"}]},
    {"type": "namespace", "name": "b", "tools": [{"type": "function", "name": "dup"}]},
]}
dup_chat = P.responses_to_chat(dup_req)
dup_names = [P._tool_declared_name(t) for t in dup_chat.get("tools") or []]
check("同名只留第一个", dup_names == ["dup"], dup_names)
check("映射保留第一个 namespace", dup_chat.get("_qd_tool_ns") == {"dup": "a"})

deep = {"type": "namespace", "name": "n0", "tools": []}
cur = deep
for i in range(1, 8):
    nxt = {"type": "namespace", "name": "n%d" % i, "tools": []}
    cur["tools"].append(nxt)
    cur = nxt
_flat, _ = P._flatten_ns_tools([deep])
check("超深嵌套不炸（被丢弃并告警）", isinstance(_flat, list))

print("[#127 F3] input 里的 additional_tools 合并")
req3 = {"model": "gpt-5",
        "input": [{"type": "message", "role": "user", "content": "hi"},
                  {"type": "additional_tools",
                   "tools": [{"type": "function", "name": "extra_tool",
                              "parameters": {}}]}],
        "tools": [{"type": "function", "name": "top_tool", "parameters": {}}]}
chat3 = P.responses_to_chat(req3)
names3 = [P._tool_declared_name(t) for t in chat3.get("tools") or []]
check("顶层与 additional_tools 合并", names3 == ["top_tool", "extra_tool"], names3)

print("[#127 F1] 回程：非流式补 namespace")
obj = {"model": "auto", "choices": [{"finish_reason": "tool_calls",
                                     "message": {"role": "assistant", "content": "",
                                                 "tool_calls": [
                                                     {"id": "c1", "function": {"name": "create_issue", "arguments": "{}"}},
                                                     {"id": "c2", "function": {"name": "exec", "arguments": "{\"input\":\"ls\"}"}},
                                                     {"id": "c3", "function": {"name": "top_tool", "arguments": "{}"}}]}}],
       "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
ns = {"create_issue": "mcp__github", "exec": "mcp__github"}
resp = P.chat_to_response(obj, "auto", {"exec"}, ns_map=ns)
items = [i for i in resp.get("output") or [] if i.get("type") in ("function_call", "custom_tool_call")]
by_name = {i["name"]: i for i in items}
check("容器内工具补上了 namespace",
      by_name["create_issue"].get("namespace") == "mcp__github", by_name.get("create_issue"))
check("custom_tool_call 也补", by_name["exec"].get("namespace") == "mcp__github")
check("顶层工具不加 namespace（保持原样）", "namespace" not in by_name["top_tool"])

print("[#127 F1] 回程：流式 added 帧就带 namespace")
frames = [
    ("data: " + json.dumps({"id": "chatcmpl-1", "model": "auto",
                            "choices": [{"index": 0, "delta": {"tool_calls": [
                                {"index": 0, "id": "call_1", "type": "function",
                                 "function": {"name": "create_issue", "arguments": ""}}]}}]})).encode(),
    ("data: " + json.dumps({"id": "chatcmpl-1", "model": "auto",
                            "choices": [{"index": 0, "delta": {"tool_calls": [
                                {"index": 0, "function": {"arguments": "{}"}}]},
                                        "finish_reason": "tool_calls"}]})).encode(),
]
evs = []
for raw in P.stream_responses_events(iter(frames), "auto",
                                     {"usage": None, "custom_names": set(),
                                      "tool_ns": {"create_issue": "mcp__github"}}):
    text = raw.decode("utf-8", "replace")
    for part in text.split("\n\n"):
        if "data: " in part:
            try:
                evs.append(json.loads(part.split("data: ", 1)[1]))
            except Exception:
                pass
added = [e for e in evs if e.get("type") == "response.output_item.added"]
check("added 帧的 item 就带 namespace（不能只在 done 补）",
      added and added[0]["item"].get("namespace") == "mcp__github",
      added[0]["item"] if added else None)
done = [e for e in evs if e.get("type") == "response.output_item.done"]
check("done 帧同样带", done and done[0]["item"].get("namespace") == "mcp__github")

finish()

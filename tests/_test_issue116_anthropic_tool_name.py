"""#116 Anthropic 流式：tool_use 首帧无 name 时不得透传空 name。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *          # noqa: F401,F403

P, A = bootstrap()
declare_suite(__file__, ["qoder_anthropic.py"])
import qoder_anthropic as AN       # noqa: E402


def chunk(delta, finish=None):
    obj = {"choices": [{"index": 0, "delta": delta}]}
    if finish:
        obj["choices"][0]["finish_reason"] = finish
    return ("data: " + json.dumps(obj, ensure_ascii=False)).encode("utf-8")


def events(lines, model="deepseek-flash"):
    out = []
    for raw in AN.stream_anthropic_events(lines, model=model, holder={}):
        for part in raw.decode("utf-8").split("\n\n"):
            if not part.strip():
                continue
            body = part.split("data: ", 1)[-1]
            try:
                out.append(json.loads(body))
            except Exception:
                pass
    return out


def tc(index, id_="", name=None, args=None):
    fn = {}
    if name is not None:
        fn["name"] = name
    if args is not None:
        fn["arguments"] = args
    return {"index": index, "id": id_, "function": fn}


print("[#116] name 迟到的首帧：不提前开块，name 到手后补发 args")
lines = [
    chunk({"tool_calls": [tc(0, "call_1", args="")]}),
    chunk({"tool_calls": [tc(0, args='{"cmd": "ls')]}),
    chunk({"tool_calls": [tc(0, name="bash")]}),
    chunk({"tool_calls": [tc(0, args='"}')]}, "tool_calls"),
    b"data: [DONE]",
]
ev = events(lines)
starts = [e for e in ev if e.get("type") == "content_block_start"]
deltas = [e for e in ev if e.get("type") == "content_block_delta"]

check("只开了一个 tool_use 块", len(starts) == 1, len(starts))
check("块名非空（不是空串透传）",
      bool(starts) and starts[0]["content_block"].get("name") == "bash",
      starts[0]["content_block"] if starts else None)
check("id 用首帧给的（#47 语义保留）",
      bool(starts) and starts[0]["content_block"].get("id") == "call_1")
joined = "".join(d["delta"].get("partial_json") or "" for d in deltas)
check("缓冲的 arguments 一片不少、顺序不变",
      joined == '{"cmd": "ls"}', joined)
check("没有空 name 的块漏出去",
      all(s["content_block"].get("name") for s in starts))

print("[#116] name 始终缺席：丢弃该块并降级 stop_reason")
bad = [
    chunk({"tool_calls": [tc(0, "call_x", args='{"a":1}')]}, "tool_calls"),
    b"data: [DONE]",
]
ev2 = events(bad)
check("不发空 name 的 tool_use 块",
      not [e for e in ev2 if e.get("type") == "content_block_start"])
md2 = [e for e in ev2 if e.get("type") == "message_delta"]
check("stop_reason 降级为 stop（下游不会等工具结果）",
      bool(md2) and md2[0]["delta"]["stop_reason"] == "end_turn",
      md2[0]["delta"] if md2 else None)
check("流仍然正常收尾（message_stop 在）",
      any(e.get("type") == "message_stop" for e in ev2))

print("[#116] 正常首帧带 name：行为不变")
ok = [
    chunk({"tool_calls": [tc(0, "call_ok", name="bash", args='{"a":1}')]},
          "tool_calls"),
    b"data: [DONE]",
]
ev3 = events(ok)
s3 = [e for e in ev3 if e.get("type") == "content_block_start"]
check("正常块照旧立刻开", len(s3) == 1 and s3[0]["content_block"]["name"] == "bash")
d3 = [e for e in ev3 if e.get("type") == "content_block_delta"]
check("正常块参数只发一次（不重复）",
      "".join(d["delta"].get("partial_json") or "" for d in d3) == '{"a":1}',
      [d["delta"].get("partial_json") for d in d3])
md3 = [e for e in ev3 if e.get("type") == "message_delta"]
check("正常工具轮 stop_reason 仍是 tool_use",
      bool(md3) and md3[0]["delta"]["stop_reason"] == "tool_use",
      md3[0]["delta"] if md3 else None)

print("[#116] 非流式侧同样不发空 name")
msg = AN.chat_to_messages({"model": "deepseek-flash",
                           "choices": [{"finish_reason": "tool_calls",
                                        "message": {"role": "assistant",
                                                    "content": "",
                                                    "tool_calls": [
                                                        {"id": "c1", "function": {"name": "", "arguments": "{}"}},
                                                        {"id": "c2", "function": {"name": "bash", "arguments": '{"x":2}'}}]}}],
                           "usage": {"prompt_tokens": 3, "completion_tokens": 1,
                                     "total_tokens": 4}}, model="deepseek-flash")
tools = [b for b in msg["content"] if b.get("type") == "tool_use"]
check("无名调用被丢弃、有名调用保留",
      len(tools) == 1 and tools[0]["name"] == "bash", tools)
check("非流式也不再出现空 name", all(b.get("name") for b in tools))

print("[#116] 多工具：后一个名字先到也不能串位")
lines_multi = [
    chunk({"tool_calls": [tc(0, "c_a", args='{"p":1}')]}, "tool_calls"),
    chunk({"tool_calls": [tc(1, "c_b", name="grep", args='{"q":2}')]}, "tool_calls"),
    b"data: [DONE]",
]
ev4 = events(lines_multi)
st4 = [e for e in ev4 if e.get("type") == "content_block_start"]
check("只有拿到 name 的那个开块", len(st4) == 1 and st4[0]["content_block"]["name"] == "grep", st4)
md4 = [e for e in ev4 if e.get("type") == "message_delta"]
check("有块被丢时也把 stop_reason 降级", md4 and md4[0]["delta"]["stop_reason"] == "end_turn",
      md4[0]["delta"] if md4 else None)

finish()

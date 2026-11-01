"""#124 chat 流式出口：同帧 tool_calls 不被吞 + 无名调用扣住/降级。"""
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *          # noqa: F401,F403

P, A = bootstrap()
declare_suite(__file__, ["qoder_proxy.py"])


def chunk(delta, finish=None, **top):
    obj = {"id": "chatcmpl-t", "object": "chat.completion.chunk",
           "created": 1, "model": "auto",
           "choices": [{"index": 0, "delta": delta}]}
    if finish:
        obj["choices"][0]["finish_reason"] = finish
    obj.update(top)
    return ("data: " + json.dumps(obj, ensure_ascii=False)).encode("utf-8")


def run(frame_bytes, **kw):
    out = []
    for raw in P.recover_leaked_tool_calls(iter(frame_bytes), **kw):
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        data = P.strip_data_prefix(text)
        if not data or data == "[DONE]":
            continue
        try:
            out.append(json.loads(data))
        except Exception:
            pass
    return out


def tcs(chunks):
    got = []
    for c in chunks:
        for ch in c.get("choices") or []:
            for tc in (ch.get("delta") or {}).get("tool_calls") or []:
                got.append(tc)
    return got


print("[#124] 同帧 content + tool_calls 起点：起点不能被吞")
frames = [
    chunk({"content": "好的，我来改这个变量。",
           "tool_calls": [{"index": 0, "type": "function",
                           "function": {"name": "edit", "arguments": ""}}]}),
    chunk({"tool_calls": [{"index": 0, "type": "function",
                           "function": {"arguments": '{"path":"a.css"}'}}]}),
]
out = run(frames)
starts = [t for t in tcs(out) if (t.get("function") or {}).get("name")]
check("起点帧带回了 name", starts and starts[0]["function"]["name"] == "edit",
      starts[:1])
check("起点帧带 id（#47）", bool(starts and starts[0].get("id")))
body = "".join((ch.get("delta") or {}).get("content") or ""
               for c in out for ch in c.get("choices") or [])
check("正文没丢", "好的" in body, body)

print("[#124] 同帧 content + reasoning + 起点")
frames = [
    chunk({"content": "过渡话", "reasoning_content": "想一想",
           "tool_calls": [{"index": 0, "type": "function",
                           "function": {"name": "bash", "arguments": ""}}]}),
]
out = run(frames)
names = [(t.get("function") or {}).get("name") for t in tcs(out)]
check("name 保住", "bash" in names, names)
reasoning = "".join((ch.get("delta") or {}).get("reasoning_content") or ""
                    for c in out for ch in c.get("choices") or [])
check("reasoning 也没被吞", reasoning == "想一想", reasoning)

print("[#124] 孤儿无名调用：扣住、流末丢弃、finish 降级")
frames = [
    chunk({"tool_calls": [{"index": 0, "type": "function",
                           "function": {"arguments": '{"a":1}'}}]}, "tool_calls"),
]
out = run(frames)
check("没有任何带 name 的 tool_calls 漏出",
      not [t for t in tcs(out) if (t.get("function") or {}).get("name")])
finals = [ch.get("finish_reason") for c in out for ch in c.get("choices") or []
          if ch.get("finish_reason")]
check("finish_reason 降级为 stop", finals and finals[-1] == "stop", finals)

print("[#124] name 后到：合并成一帧再发（不重复、不粘连）")
frames = [
    chunk({"tool_calls": [{"index": 0, "type": "function",
                           "function": {"arguments": '{"x":'}}]}),
    chunk({"tool_calls": [{"index": 0, "type": "function",
                           "function": {"name": "grep", "arguments": "1}"}}]},
          "tool_calls"),
]
out = run(frames)
started = [t for t in tcs(out) if (t.get("function") or {}).get("name")]
check("只发了一次起点", len(started) == 1, started)
check("name 不粘连（不是 grepgrep）",
      started and started[0]["function"]["name"] == "grep", started[:1])
check("攒下的 arguments 与后到的拼在一起",
      started and started[0]["function"]["arguments"] == '{"x":1}',
      started[0]["function"]["arguments"] if started else None)

print("[#124] 正常并行双调用不受影响")
frames = [
    chunk({"tool_calls": [{"index": 0, "type": "function",
                           "function": {"name": "a", "arguments": ""}},
                          {"index": 1, "type": "function",
                           "function": {"name": "b", "arguments": ""}}]}),
    chunk({"tool_calls": [{"index": 0, "function": {"arguments": "{}"}},
                          {"index": 1, "function": {"arguments": "{}"}}]},
          "tool_calls"),
]
out = run(frames)
names = [(t.get("function") or {}).get("name") for t in tcs(out)]
check("两个 name 都在", names.count("a") == 1 and names.count("b") == 1, names)
finals = [ch.get("finish_reason") for c in out for ch in c.get("choices") or []
          if ch.get("finish_reason")]
check("正常工具轮 finish 仍是 tool_calls",
      finals and finals[-1] == "tool_calls", finals)

print("[#124] 纯正文流：内容与帧数不变")
plain = [chunk({"content": "你好"}), chunk({"content": "世界"})]
raw_out = list(P.recover_leaked_tool_calls(iter(plain)))
_pieces = []
for _raw in raw_out:
    _d = P.strip_data_prefix(_raw.decode("utf-8"))
    _o = json.loads(_d)
    _pieces.append((_o.get("choices") or [{}])[0].get("delta", {}).get("content"))
check("正文顺序与内容不变", _pieces == ["你好", "世界"], _pieces)
check("没有凭空多出帧", len(raw_out) == 2, len(raw_out))
check("没有注入 tool_calls", not tcs([json.loads(P.strip_data_prefix(r.decode("utf-8")))
                                             for r in raw_out]))

print("[#124] 同帧 content + 调用 + finish 不重复发")
frames = [
    chunk({"content": "走起",
           "tool_calls": [{"index": 0, "type": "function",
                           "function": {"name": "edit", "arguments": "{}"}}]},
          "tool_calls"),
]
out = run(frames)
names = [(t.get("function") or {}).get("name") for t in tcs(out)]
check("起点只发一次（不出现 editedit）", names.count("edit") == 1, names)

finish()

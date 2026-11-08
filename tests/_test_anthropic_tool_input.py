# -*- coding: utf-8 -*-
"""Anthropic 工具参数：双重编码不能把字符串写进客户端历史。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import ROOT, check, declare_suite, finish

sys.path.insert(0, ROOT)
import qoder_anthropic as AN

declare_suite(__file__, ["qoder_anthropic.py"])


def chunk(calls=(), finish=None, content=None):
    delta = {"tool_calls": list(calls)}
    if content is not None:
        delta["content"] = content
    return ("data: " + json.dumps({"choices": [{
        "index": 0, "delta": delta, "finish_reason": finish}]},
        ensure_ascii=False) + "\n\n").encode("utf-8")


def call(args, index=0, name="Read", cid="call_1"):
    fn = {"arguments": args}
    if name is not None:
        fn["name"] = name
    return {"index": index, "id": cid, "function": fn}


def event(raw):
    for line in raw.decode("utf-8").splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    return None


def events(lines):
    return [e for e in (event(raw) for raw in AN.stream_anthropic_events(lines))
            if e is not None]


def tools(evs):
    """模拟客户端：按块累计增量，只在块结束时解析完整 input。"""
    blocks, partials, closed = {}, {}, []
    for e in evs:
        kind, index = e["type"], e.get("index")
        if kind == "content_block_start":
            blocks[index] = dict(e["content_block"])
            partials[index] = []
        elif kind == "content_block_delta" and e["delta"]["type"] == "input_json_delta":
            partials[index].append(e["delta"]["partial_json"])
        elif kind == "content_block_stop":
            block = blocks[index]
            if block["type"] == "tool_use":
                if partials[index]:
                    block["input"] = json.loads("".join(partials[index]))
                closed.append(block)
    return closed


def nonstream(args):
    response = AN.chat_to_messages({"choices": [{
        "message": {"tool_calls": [call(args)]}, "finish_reason": "tool_calls"}]})
    return response["content"][0]["input"]


obj = {"file_path": "/tmp/中文.txt", "options": {"lines": [1, 2]}}
encoded = json.dumps(obj, ensure_ascii=False)
double = json.dumps(encoded, ensure_ascii=False)
cases = [
    ("正常对象", encoded, obj),
    ("空对象", "{}", {}),
    ("双重编码对象", double, obj),
    ("带前后空白的双重编码", " \n" + double + "\t", obj),
    ("数组", '[1, {"x": 2}]', {"value": [1, {"x": 2}]}),
    ("双重编码数组", json.dumps('[1, {"x": 2}]'), {"value": [1, {"x": 2}]}),
    ("整数", "42", {"value": 42}),
    ("零", "0", {"value": 0}),
    ("负数", "-7", {"value": -7}),
    ("小数", "1.5", {"value": 1.5}),
    ("真值", "true", {"value": True}),
    ("假值", "false", {"value": False}),
    ("空值", "null", {"value": None}),
    ("普通字符串", '"你好"', {"value": "你好"}),
    ("空字符串", '""', {"value": ""}),
    ("数字字符串不改类型", '"42"', {"value": "42"}),
    ("字符串内容不是完整 JSON", json.dumps('{"x":'), {"value": '{"x":'}),
    ("无参数", "", {}),
]
for label, args, expected in cases:
    # 逐字符分片覆盖引号、反斜线、空白、中文跨分片的情况。
    lines = [chunk([call(piece)]) for piece in args] if args else [chunk([call("")])]
    evs = events(lines + [chunk(finish="tool_calls"), b"data: [DONE]\n\n"])
    actual = tools(evs)
    check(label + "：流式最终 input 必须是预期对象",
          len(actual) == 1 and isinstance(actual[0]["input"], dict)
          and actual[0]["input"] == expected, actual)
    check(label + "：非流式与流式口径一致", nonstream(args) == expected,
          nonstream(args))

split_failures = []
for boundary in range(len(double) + 1):
    actual = tools(events([
        chunk([call(double[:boundary])]),
        chunk([call(double[boundary:])], finish="tool_calls")]))
    if len(actual) != 1 or actual[0]["input"] != obj:
        split_failures.append(boundary)
check("双重编码在任意两段边界切分都能恢复对象", not split_failures, split_failures)

# 用惰性上游证明正常参数没有被推迟到流结束。
consumed = []
def normal_source():
    consumed.append(1)
    yield chunk([call(' \n')])
    consumed.append(2)
    yield chunk([call('{"file_path":')])
    consumed.append(3)
    yield chunk([call('"/tmp/x.txt"}')], finish="tool_calls")

stream = AN.stream_anthropic_events(normal_source())
first_deltas = []
for raw in stream:
    e = event(raw)
    if e and e["type"] == "content_block_delta":
        first_deltas.append(e["delta"]["partial_json"])
        if "{" in e["delta"]["partial_json"]:
            break
check("正常对象未读下一片就输出，前导空白逐字保留",
      consumed == [1, 2] and "".join(first_deltas) == ' \n{"file_path":',
      (consumed, first_deltas))
list(stream)

consumed = []
def quoted_source():
    consumed.append(1)
    yield chunk([call(double[:1])])
    yield b": ping\n\n"
    consumed.append(2)
    yield chunk([call(double[1:])], finish="tool_calls")

stream = AN.stream_anthropic_events(quoted_source())
before_ping = []
for raw in stream:
    if raw.startswith(b":"):
        break
    e = event(raw)
    if e and e["type"] == "content_block_delta":
        before_ping.append(e)
check("疑似字符串在校验前不输出参数，心跳仍即时透传",
      consumed == [1] and not before_ping, before_ping)
list(stream)

evs = events([
    chunk([call(double[:3], name=None)]),
    chunk([call('{"query":', index=1, name="Search", cid="call_2")], content="查询中"),
    chunk([call(double[3:], name=None)]),
    chunk([call('"例子"}', index=1, name=None, cid="")]),
    chunk([call("", name="Read", cid="")], finish="tool_calls"),
])
actual = tools(evs)
by_id = {b["id"]: b for b in actual}
check("并行工具与迟到 name：id、参数、名称不串位",
      len(actual) == 2 and by_id["call_1"]["input"] == obj
      and by_id["call_1"]["name"] == "Read"
      and by_id["call_2"]["input"] == {"query": "例子"}
      and by_id["call_2"]["name"] == "Search", actual)
check("规范化后仍正常收尾，工具轮 stop_reason 保持 tool_use",
      evs[-2]["delta"]["stop_reason"] == "tool_use"
      and evs[-1]["type"] == "message_stop")

payload = {"messages": [
    {"role": "assistant", "content": actual},
    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1",
                                     "content": "读取完成"}]}]}
chat = AN.messages_to_chat(payload)
upstream = {c["id"]: c["function"] for c in chat["messages"][0]["tool_calls"]}
check("客户端保存再回传：上游收到对象 JSON，工具结果仍配对",
      json.loads(upstream["call_1"]["arguments"]) == obj
      and chat["messages"][1]["tool_call_id"] == "call_1", chat)

for label, history, expected in [
    ("对象", obj, obj), ("对象字符串", encoded, obj),
    ("双重编码历史", double, obj), ("数组字符串", "[1, 2]", [1, 2]),
    ("双重编码数组历史", json.dumps("[1, 2]"), [1, 2]),
]:
    payload = {"messages": [{"role": "assistant", "content": [
        {"type": "tool_use", "id": "old_call", "name": "Read", "input": history}]}]}
    args = AN.messages_to_chat(payload)["messages"][0]["tool_calls"][0]["function"]["arguments"]
    check(label + "：历史入站只保留一层 JSON 编码", json.loads(args) == expected, args)
    check(label + "：转换不修改客户端传入的历史", payload["messages"][0]["content"][0]["input"] == history)

for history in ('"text"', "42", "null", "", '{"x":'):
    payload = {"messages": [{"role": "assistant", "content": [
        {"type": "tool_use", "id": "old_call", "name": "Read", "input": history}]}]}
    args = AN.messages_to_chat(payload)["messages"][0]["tool_calls"][0]["function"]["arguments"]
    check("历史不是对象/数组时保留原有处理：%r" % history, args == history, args)

for malformed in ('{"x":', '"{\\"x\\":', '[1,', ' \n'):
    evs = events([chunk([call(malformed)], finish="length")])
    partials = [e["delta"]["partial_json"] for e in evs
                if e["type"] == "content_block_delta"]
    check("截断/非法 JSON 不编造参数：%r" % malformed,
          "".join(partials) == malformed
          and evs[-2]["delta"]["stop_reason"] == "max_tokens", partials)

check("非流式非法 JSON 保留原有空对象回退", nonstream('{"x":') == {})
finish()

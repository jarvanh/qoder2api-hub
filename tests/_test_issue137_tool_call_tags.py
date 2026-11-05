# -*- coding: utf-8 -*-
"""#137 伪 XML 工具调用：`<tool_call>Name {json}</tool_call>` 回读。

    python tests/_test_issue137_tool_call_tags.py

背景：自定义供应商 / 转换层中转的模型会把工具调用写成伪 XML。网关此前只认
结构化 tool_calls / DSML / 网关 marker+JSON 数组，这种形态要么当正文漏给
客户端，要么被判成「无法还原的协议残片」而 fail-closed（整轮静默中断）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *          # noqa: F401,F403

P, A = bootstrap()
declare_suite(__file__, ["qoder_proxy.py"])

NAME = "Bash"

# 用户实际报障的样本：内层引号未转义 + 前缀 marker + 尾随空格。
USER_SAMPLE = (
    '[assistant 请求调用工具] <tool_call> Bash {"command":"cd /tmp && '
    'echo "=== 1. 示例目录结构对等 ===" && for d in examples/*/; do '
    'n=$(basename d); printf \\"%-18s\\" \\"n"; done", '
    '"description":"Verify example structure parity"} </tool_call>'
)


def frame(delta, finish=None):
    return ("data: " + json.dumps({"id": "c", "model": "x",
             "choices": [{"index": 0, "delta": delta,
                          "finish_reason": finish}]},
             ensure_ascii=False) + "\n\n").encode()


def frames(text, n):
    out = [frame({"content": text[i:i + n]}) for i in range(0, len(text), n)]
    out.append(frame({}, "stop"))
    return out


def run_stream(text, n, names):
    calls, txt, fins = [], [], []
    for f in P.recover_leaked_tool_calls(iter(frames(text, n)), names):
        d = P.strip_data_prefix(f.decode("utf-8", "replace"))
        if not d or d == "[DONE]":
            continue
        o = json.loads(d)
        ch = (o.get("choices") or [{}])[0]
        dd = ch.get("delta") or {}
        calls.extend(dd.get("tool_calls") or [])
        txt.append(dd.get("content") or "")
        if ch.get("finish_reason"):
            fins.append(ch["finish_reason"])
    return calls, "".join(txt), fins


def args_of(call):
    return json.loads((call.get("function") or {}).get("arguments") or "{}")


# ---------------------------------------------------------------------------
# [1] 解析器：用户样本与各种变体
# ---------------------------------------------------------------------------
calls, clean = P.parse_tool_call_tags(USER_SAMPLE, {NAME})
check("[1a] 用户样本可还原", bool(calls))
check("[1b] 工具名正确", calls and calls[0]["function"]["name"] == NAME)
check("[1c] 未转义引号已修复（参数是合法 JSON）",
      calls and isinstance(args_of(calls[0]).get("command"), str))
check("[1d] description 保留",
      calls and args_of(calls[0]).get("description")
      == "Verify example structure parity")
check("[1e] marker 已从正文剥离", clean == "")

calls, _ = P.parse_tool_call_tags(
    '<tool_call>Bash {"command":"ls -la"}</tool_call>', {NAME})
check("[2] 裸标签（无 marker）可还原", bool(calls))

calls, _ = P.parse_tool_call_tags(
    '<tool_call>{"name":"Read","arguments":{"file_path":"a.js"}}</tool_call>',
    {"Read"})
check("[3] JSON 对象形态可还原",
      calls and calls[0]["function"]["name"] == "Read")

calls, _ = P.parse_tool_call_tags(
    '<tool_call>Bash({"command":"pwd"})</tool_call>', {NAME})
check("[4] 函数调用形态（括号）可还原", bool(calls))

calls, _ = P.parse_tool_call_tags(
    '<tool_call>Read {"file_path":"a"}</tool_call>\n'
    '<tool_call>Bash {"command":"pwd"}</tool_call>', {"Read", NAME})
check("[5] 多个标签块 -> 多个调用", calls is not None and len(calls) == 2)

calls, _ = P.parse_tool_call_tags('<tool_call>Bash</tool_call>', {NAME})
check("[6] 只有工具名 -> 空参数调用", bool(calls) and args_of(calls[0]) == {})

# ---------------------------------------------------------------------------
# [2] 严格守卫：不许误伤正常正文
# ---------------------------------------------------------------------------
prose = ('例如可以写成 <tool_call>Bash {"command":"ls"}</tool_call> 这样，'
         '注意闭标签不能少。')
calls, clean = P.parse_tool_call_tags(prose, {NAME})
check("[7] 讲解示例（标签后有正文）不还原", calls is None)
check("[7b] 讲解示例原样返回", clean == prose)

calls, clean = P.parse_tool_call_tags(
    '<tool_call>Hack {"x":1}</tool_call>', {NAME})
check("[8] 未声明的工具名不还原", calls is None)

calls, clean = P.parse_tool_call_tags(
    '<tool_call>Bash {"command":"ls"}</tool_call>', None)
check("[9] 未声明 tools 的会话不还原", calls is None)

calls, _ = P.parse_tool_call_tags('<tool_call>Bash {"command": ls}</tool_call>',
                                  {NAME})
check("[10] 参数不是合法 JSON 不还原", calls is None)

calls, _ = P.parse_tool_call_tags('没有标签的普通回复', {NAME})
check("[11] 没有标签 -> 不命中", calls is None)

# ---------------------------------------------------------------------------
# [3] 流式：各种分帧都要能恢复
# ---------------------------------------------------------------------------
for n in (1, 2, 3, 8, 64, 4096):
    calls, txt, fins = run_stream(USER_SAMPLE, n, {NAME})
    check("[12 n=%d] 流式恢复出一个调用" % n, len(calls) == 1)
    check("[12 n=%d] 工具名正确" % n,
          calls and calls[0]["function"]["name"] == NAME)
    check("[12 n=%d] 参数是合法 JSON" % n,
          calls and isinstance(args_of(calls[0]), dict))
    check("[12 n=%d] 正文不留协议残片" % n, txt == "")
    check("[12 n=%d] finish_reason=tool_calls" % n,
          "tool_calls" in fins)

bare = '<tool_call>Bash {"command":"pwd"}</tool_call>'
for n in (1, 4, 4096):
    calls, txt, fins = run_stream(bare, n, {NAME})
    check("[13 n=%d] 裸标签流式恢复" % n, len(calls) == 1 and txt == "")

# 讲解式文本在流式里必须逐字节保留
calls, txt, fins = run_stream(prose, 3, {NAME})
check("[14] 流式讲解示例不还原", len(calls) == 0)
check("[14b] 流式讲解示例逐字节守恒", txt == prose)

# 未声明 tools：完全不缓冲，逐字节透传
calls, txt, _ = run_stream(bare, 5, None)
check("[15] 未声明 tools 时流式逐字节透传", calls == [] and txt == bare)

# ---------------------------------------------------------------------------
# [4] 非流式 chat 聚合（aggregate_stream 的回读段）
# ---------------------------------------------------------------------------
obj = {"choices": [{"index": 0, "finish_reason": "stop",
                    "message": {"role": "assistant", "content": USER_SAMPLE}}]}
res = P.chat_to_response(obj, "m", allowed_names={NAME})
types = [o["type"] for o in res["output"]]
check("[16] Responses 非流式产出 function_call",
      types == ["function_call"])
check("[16b] output_text 为空（协议已剥离）", res["output_text"] == "")
check("[16c] function_call 参数可解析",
      isinstance(json.loads(res["output"][0]["arguments"]), dict))

res2 = P.chat_to_response(obj, "m")
check("[17] 未声明工具时 Responses 不转换",
      [o["type"] for o in res2["output"]] == ["message"])

# ---------------------------------------------------------------------------
# [5] Responses 流式事件
# ---------------------------------------------------------------------------
evs = []
holder = {"allowed_names": {NAME}}
for f in P.stream_responses_events(
        P.recover_leaked_tool_calls(iter(frames(USER_SAMPLE, 6)), {NAME}),
        "m", holder):
    s = f.decode("utf-8", "replace") if isinstance(f, bytes) else f
    for line in s.splitlines():
        if line.startswith("event: "):
            evs.append(line[7:])
check("[18] Responses 流出现 function_call_arguments.done",
      "response.function_call_arguments.done" in evs)
check("[18b] Responses 流正常完成", evs[-1] == "response.completed")
check("[18c] Responses 流没有正文 delta", "response.output_text.delta" not in evs)

# ---------------------------------------------------------------------------
# [6] 回归护栏：既有三种形态不受影响
# ---------------------------------------------------------------------------
array_form = ('[assistant 请求调用工具]\n'
              '[{"name":"Bash","arguments":"{\\"command\\":\\"ls\\"}"}]')
calls, txt, _ = run_stream(array_form, 7, {NAME})
check("[19] marker+JSON 数组仍走原解析器", len(calls) == 1 and txt == "")

dsml = ('<|DSML|calls><|DSML|invoke name="Bash">'
        '<|DSML|parameter name="command">ls</|DSML|parameter>'
        '</|DSML|invoke></|DSML|calls>')
calls, txt, _ = run_stream(dsml, 9, {NAME})
check("[20] DSML 仍可恢复", len(calls) == 1 and txt == "")

plain = "这是一段完全正常的回复，没有任何工具调用。"
calls, txt, _ = run_stream(plain, 4, {NAME})
check("[21] 普通正文逐字节守恒", calls == [] and txt == plain)

plain_lt = "比较 a < b 且 c > d，还有 <div> 标签。"
calls, txt, _ = run_stream(plain_lt, 3, {NAME})
check("[22] 含尖括号的普通正文逐字节守恒", calls == [] and txt == plain_lt)

finish()

# -*- coding: utf-8 -*-
"""#138 从真实 502 日志反推的形态回归。

    python tests/_test_issue138_502_forms.py

素材来源：用户 qoder-proxy 运行日志里 issue #72 / #49 的 502 记录。
每条 check 都对应一个真实出现过的字符长度或形态：

  · 21  chars marker_only           -> 纯 marker，无内容（不可恢复，源头另修）
  · 26/31/41/52/53/57 chars         -> marker + `<tool_call` 刚开头就被判证伪
  · 58  chars unrecovered_dsml      -> DSML 截断
  · 717 / 1545 chars                -> 真实块，旧代码解析不出（属性/未转义引号）
  · 2863 chars issue #9             -> 长块被当截断吞掉

修复点：伪 XML 标签在流式 hold 里不再被提前证伪；属性形态
`<tool_call name="X">`、逗号/冒号分隔、块后收尾散文都能还原；围栏内的示例
（文档/教程）一律不转换。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *          # noqa: F401,F403

P, A = bootstrap()
declare_suite(__file__, ["qoder_proxy.py"])

BASH = "Bash"
READ = "Read"
NAMES = {BASH, READ}
MARKER = P.LEAK_MARKER

# 与网关写进历史的前缀逐字节一致（21 字符）：flatten_messages 的
# `text + "\n\n" + LEAK_MARKER + "\n" + json`。
PREFIX21 = "\n\n" + MARKER + "\n"


def frame(delta, finish=None):
    return ("data: " + json.dumps({"id": "c", "model": "x",
             "choices": [{"index": 0, "delta": delta,
                          "finish_reason": finish}]},
             ensure_ascii=False) + "\n\n").encode()


def frames(text, n):
    out = [frame({"content": text[i:i + n]}) for i in range(0, len(text), n)]
    out.append(frame({}, "stop"))
    return out


def run(text, n=6, names=NAMES):
    """返回 (命中/异常, 工具名列表, 正文, finish 列表)。"""
    calls, txt, fins = [], [], []
    try:
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
        return "ok", [c["function"]["name"] for c in calls], "".join(txt), fins
    except Exception as exc:
        return type(exc).__name__, [], "", []


def ok_call(text, n=6, name=BASH):
    state, names, txt, fins = run(text, n)
    return (state == "ok" and names == [name] and "tool_calls" in fins
            and txt.strip() == "")


print("[138-1] 前缀校准：21 字符就是网关自己写进历史的脚手架")
check("21 字符 = \\n\\n + marker + \\n", len(PREFIX21) == 21, len(PREFIX21))
_flat = P.flatten_messages([
    {"role": "assistant", "content": "",
     "tool_calls": [{"id": "t1", "type": "function",
                     "function": {"name": BASH, "arguments": "{}"}}]},
], structured=False)[1][0]["content"]
check("文本化历史确实以这 21 字符起头", _flat.startswith(PREFIX21), repr(_flat[:30]))

print("\n[138-2] 各种分帧下都能从真实块里还原调用")
real = ('\n\n' + MARKER + '\n<tool_call> Bash {'
        '"command":"cd /tmp && echo "=== 1 ===" && ls -la", '
        '"description":"verify example structure parity"} </tool_call>')
for n in (1, 2, 3, 7, 13, 64, 4096):
    check("n=%d 用户样本可还原" % n, ok_call(real, n), run(real, n))

print("\n[138-3] 日志里 unrecog 的成因：标签刚开头就被判证伪")
# 旧代码在 hold 到 "<tool_call " 时会 _json_array_prefix_ok 判 False -> 立刻
# 放行/fail-closed。这些长度正是「前缀 + 标签开头若干字符」。
for cut in (26, 31, 41, 52, 53, 57):
    prefix = (PREFIX21 + "<tool_call>")[:cut]
    check("截断到 %d 字符时不抛异常" % cut,
          run(prefix)[0] in ("ok", "EmptyToolEcho"), run(prefix)[0])

print("\n[138-4] 属性形态 / 分隔符形态（旧解析器全部认不出）")
attr = '\n\n' + MARKER + '\n<tool_call name="Bash">{"command":"ls"}</tool_call>'
check("name 属性形态可还原", ok_call(attr), run(attr))
for n in (3, 9, 4096):
    check("name 属性形态 n=%d" % n, ok_call(attr, n), run(attr, n))

comma = '<tool_call>Bash, {"command":"ls"}</tool_call>'
check("逗号分隔可还原", ok_call(comma), run(comma))
colon = '<tool_call>Read: {"file_path":"a.js"}</tool_call>'
check("冒号分隔可还原", ok_call(colon, name=READ), run(colon))

print("\n[138-5] 块后的收尾散文（717/1545 这类长块常见）")
after = ('\n\n' + MARKER +
         '\n<tool_call>Bash {"command":"ls"}</tool_call>\n\n以上是本次操作。')
# 收尾散文要**保留**（它是模型真实的说明文字），只剥协议块本身。
check("marker 块 + 收尾散文可还原",
      run(after)[1] == [BASH] and "tool_calls" in run(after)[3], run(after))
for n in (3, 11, 4096):
    _st, _names, _txt, _fins = run(after, n)
    check("收尾散文 n=%d 可还原" % n,
          _names == [BASH] and "tool_calls" in _fins, (_names, _fins))
    check("收尾散文 n=%d 正文保留" % n, "以上是本次操作" in _txt, repr(_txt))

print("\n[138-6] 防误伤：文档示例 / 讲解 / 未声明工具")
fenced = '示例：\n```\n<tool_call>Bash {"command":"ls"}</tool_call>\n```'
for n in (1, 4, 4096):
    st, names, txt, _ = run(fenced, n)
    check("围栏内示例不转换 n=%d" % n, names == [], (n, names))
    check("围栏内示例逐字节透传 n=%d" % n, "```" in txt and "<tool_call>" in txt,
          repr(txt[:60]))

prose = '格式是 <tool_call>Bash {"command":"ls"}</tool_call> 这样。'
for n in (3, 9, 4096):
    check("讲解示例不转换 n=%d" % n, run(prose, n)[1] == [], run(prose, n))

check("未声明工具不转换",
      P.parse_tool_call_tags('<tool_call>Bash {"command":"ls"}</tool_call>',
                             None)[0] is None)
check("未声明工具时流式不缓冲（逐字节）",
      run('<tool_call>Bash {"command":"ls"}</tool_call>', 5, None)[2]
      == '<tool_call>Bash {"command":"ls"}</tool_call>')

print("\n[138-7] marker_only 不可恢复，但必须如实归类（不伪装成恢复）")
st, names, txt, _ = run(PREFIX21)
check("纯 marker -> EmptyToolEcho/marker_only",
      st == "EmptyToolEcho" and names == [], (st, names))

print("\n[138-8] 结构化历史：默认支持 intl，可显式回退 cn")
_msgs = [
    {"role": "assistant", "content": "",
     "tool_calls": [{"id": "t1", "type": "function",
                     "function": {"name": BASH, "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "t1", "content": "ok"},
]
os.environ.pop("QD_STRUCTURED_TOOL_HISTORY", None)
os.environ.pop("QD_STRUCTURED_TOOL_HISTORY_REALMS", None)
check("默认：cn + dfmodel 启用",
      P.structured_tool_history_enabled(model="DeepSeek-Flash",
                                       model_key="dfmodel", realm="cn",
                                       messages=_msgs) is True)
check("默认：intl 启用，避免复述历史信封",
      P.structured_tool_history_enabled(model="DeepSeek-Flash",
                                       model_key="dfmodel", realm="intl",
                                       messages=_msgs) is True)
os.environ["QD_STRUCTURED_TOOL_HISTORY_REALMS"] = "cn,intl"
check("开关放开：intl + dfmodel 启用",
      P.structured_tool_history_enabled(model="DeepSeek-Flash",
                                       model_key="dfmodel", realm="intl",
                                       messages=_msgs) is True)
check("开关放开后非白名单仍排除（minimax）",
      P.structured_tool_history_enabled(model="MiniMax-M2", model_key="minimax",
                                       realm="intl", messages=_msgs) is False)
check("开关放开后 id 不齐仍回退（fail-safe）",
      P.structured_tool_history_enabled(
          model="DeepSeek-Flash", model_key="dfmodel", realm="intl",
          messages=[{"role": "tool", "content": "x"}]) is False)
os.environ.pop("QD_STRUCTURED_TOOL_HISTORY_REALMS", None)
check("移除开关后回到默认",
      P.structured_tool_history_enabled(model="DeepSeek-Flash",
                                       model_key="dfmodel", realm="intl",
                                       messages=_msgs) is True)
os.environ["QD_STRUCTURED_TOOL_HISTORY_REALMS"] = "cn"
check("显式 cn 仍可回退国际区旧行为",
      P.structured_tool_history_enabled(model="GLM-5.3", realm="intl",
                                       messages=_msgs) is False)
os.environ.pop("QD_STRUCTURED_TOOL_HISTORY_REALMS", None)

print("\n[138-9] 既有三种协议形态不受影响")
_array = ('\n\n' + MARKER + '\n'
          '[{"name":"Bash","arguments":"{\\"command\\":\\"ls\\"}"}]')
check("marker+JSON 数组仍可还原", ok_call(_array, name=BASH), run(_array))
_dsml = ('<|DSML|calls><|DSML|invoke name="Bash">'
         '<|DSML|parameter name="command">ls</|DSML|parameter>'
         '</|DSML|invoke></|DSML|calls>')
check("DSML 仍可还原", ok_call(_dsml, 9), run(_dsml, 9))
check("普通正文逐字节守恒",
      run("完全正常的回复，没有工具调用。", 4)[2] == "完全正常的回复，没有工具调用。")

finish()

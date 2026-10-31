# -*- coding: utf-8 -*-
"""issue #37/#60 follow-up：回声抢救盲区（裸控制字符 / 多调用数组 / 混合坏形）。

    python tests/_test_issue37_salvage_followup.py

背景：Kimi 类模型复述 `[assistant 请求调用工具] [{"name",...}]` 回声时，实际坏形
不止「内层引号未转义」（#60 已修）：
  A) Bash / Write 类参数里带**真实换行、制表符**（JSON 禁止裸控制字符）；
  B) 一次**多个调用**的数组里部分或全部 arguments 坏了——旧宽松重建只认单调用；
  C) 两种坏形混合（多调用 + 未转义引号 + 裸换行）。
本套件钉住泛化后的 _salvage_echo_array：能整体救回就还原成可执行调用，
救不回（任一段坏）必须整体放弃、保持 fail-closed——半截还原比失败更危险。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *          # noqa: F401,F403

P, A = bootstrap()
declare_suite(__file__, ["qoder_proxy.py"])

MARK = P.LEAK_MARKER          # "[assistant 请求调用工具]"

NAMES = {"Bash", "Write", "Read"}


def echo(body):
    return "%s %s" % (MARK, body)


def run_stream(text, n, names=NAMES):
    """按 n 字符/帧喂进 recover_leaked_tool_calls，收集 (calls, 正文, finish)。"""
    frames = []
    for i in range(0, len(text), n):
        frames.append(("data: " + json.dumps(
            {"id": "c", "choices": [{"index": 0,
                                     "delta": {"content": text[i:i + n]},
                                     "finish_reason": None}]},
            ensure_ascii=False) + "\n\n").encode())
    frames.append(("data: " + json.dumps(
        {"id": "c", "choices": [{"index": 0, "delta": {},
                                 "finish_reason": "stop"}]},
        ensure_ascii=False) + "\n\n").encode())
    calls, txt, fins = [], [], []
    for f in P.recover_leaked_tool_calls(iter(frames), names):
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


# ---------------------------------------------------------------------------
# A) 裸控制字符：参数里有真实 \n / \t
# ---------------------------------------------------------------------------
print("[37f] A：arguments 值含裸换行/制表符")
_cmd = "for f in *.txt; do\ncat\t\"$f\" >> all.md\n"
_A1 = echo('[{"name": "Bash", "arguments": {"command": "%s", "timeout": 30000}}]'
           % _cmd)
_c, _ = P.parse_leaked_tool_calls(_A1, NAMES)
check("#37f A1 原生对象 + 裸控制符 -> 恢复", bool(_c)
      and _c[0]["function"]["name"] == "Bash"
      and json.loads(_c[0]["function"]["arguments"])["command"] == _cmd,
      _c[0]["function"] if _c else None)

_A2 = echo('[{"name": "Write", "arguments": "{"path": "a.md", "content": "# 标题'
           '\n\n正文"}"}]')
_c, _ = P.parse_leaked_tool_calls(_A2, NAMES)
check("#37f A2 字符串形 + 未转义引号 + 裸换行 -> 恢复",
      bool(_c) and json.loads(_c[0]["function"]["arguments"])
      == {"path": "a.md", "content": "# 标题\n\n正文"},
      _c[0]["function"]["arguments"] if _c else None)

for _n in (1, 8, 64, 9999):
    _calls, _txt, _fins = run_stream(_A1, _n)
    check("#37f A1 流式 n=%d：1 调用、零泄漏、finish=tool_calls" % _n,
          len(_calls) == 1 and "请求调用工具" not in _txt
          and _fins == ["tool_calls"], (len(_calls), _fins))

# ---------------------------------------------------------------------------
# B) 多调用数组：全部坏 / 部分坏
# ---------------------------------------------------------------------------
print("[37f] B：多调用数组的宽松重建")
_B1 = echo('[{"name": "Read", "arguments": {"file_path": "a.txt"}}, '
           '{"name": "Write", "arguments": "{"path": "b.txt", "content": "x"}"}]')
_c, _ = P.parse_leaked_tool_calls(_B1, NAMES)
check("#37f B1 两调用（一个合法 + 一个未转义引号）-> 全部恢复",
      _c is not None and len(_c) == 2
      and [x["function"]["name"] for x in _c] == ["Read", "Write"]
      and json.loads(_c[1]["function"]["arguments"])
      == {"path": "b.txt", "content": "x"},
      [x["function"] for x in _c] if _c else None)

_B2 = echo('[{"name": "Read", "arguments": {"file_path": "a.txt"}}, '
           '{"name": "Bash"')
_c, _ = P.parse_leaked_tool_calls(_B2, NAMES)
check("#37f B2 任一段救不回 -> 整体放弃（不半截还原）", _c is None, _c)

for _n in (8, 9999):
    _calls, _txt, _fins = run_stream(_B1, _n)
    check("#37f B1 流式 n=%d：2 调用、零泄漏、finish=tool_calls" % _n,
          len(_calls) == 2 and "请求调用工具" not in _txt
          and _fins == ["tool_calls"], (len(_calls), _fins))

# ---------------------------------------------------------------------------
# C) 多调用 + 裸控制符混合（Kimi 实际坏形）
# ---------------------------------------------------------------------------
print("[37f] C：多调用 + 裸控制符混合")
_C = echo('[{"name": "Write", "arguments": {"path": "n.md", "content": "一'
           '\n二\t三"}}, {"name": "Bash", "arguments": {"command": "grep -n '
           '"kw" file.txt"}}]')
_c, _ = P.parse_leaked_tool_calls(_C, NAMES)
check("#37f C 两调用都带裸控制符 -> 全部恢复且参数可 loads",
      _c is not None and len(_c) == 2
      and all(json.loads(x["function"]["arguments"]) for x in _c),
      [x["function"]["arguments"] for x in _c] if _c else None)

# ---------------------------------------------------------------------------
# D) 护栏：坏回声仍 fail-closed、散文仍透传
# ---------------------------------------------------------------------------
print("[37f] D：护栏不变")
_D1 = echo('[{"name": "Bash", "arguments": {"p": ')
check("#37f D1 彻底截断的数组 -> 不恢复", P.parse_leaked_tool_calls(_D1, NAMES)[0] is None)
_calls, _txt, _fins = run_stream(_D1, 16)
check("#37f D1 流式：截断数组维持 #9 丢弃语义（吞正文、无泄漏；只回 marker 才走 #72 fail-closed）",
      _calls == [] and "请求调用工具" not in _txt, (_txt[:40], _fins))

_prose = ('我在讨论 [assistant 请求调用工具] 这个协议格式，'
          '例如 [{"name": "X", "arguments": {"a": 1}}, {"name": "Y"}] 这种数组。')
_calls, _txt, _fins = run_stream(_prose, 16)
check("#37f D2 纯散文（含示例数组）不被误判、逐字透传、正常收尾",
      _calls == [] and _prose in _txt and _fins == ["stop"], (_calls, _fins))

check("#37f D3 旧单调用未转义引号形态（#60）不回归",
      P.parse_leaked_tool_calls(
          echo('[{"name": "Write", "arguments": "{"path": "z", "v": "1"}"}]'),
          NAMES)[0] is not None)

check("#37f D4 未声明工具名的多调用 -> 不恢复（name 命中集守卫在宽松路径同样生效）",
      P.parse_leaked_tool_calls(
          echo('[{"name": "Evil", "arguments": "{"x": 1}"}]'), NAMES)[0] is None)

finish()

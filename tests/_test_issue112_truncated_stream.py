"""#112 流被干净截断：tool_calls 参数没写完 + 无 finish_reason 必须 fail-closed。

问题现场（本机 8790 代理，2026-10-30 16:27）：
  客户端日志  Clean EOF, no finish_reason: server ended the stream (no transport
              exception) while a tool call's arguments were still incomplete
  用户可见    助手只回了一小段工具语法/命令片段就停了，任务静默中断

两条泄漏路径（都补在这里）：
  1. 原生 tool_calls 增量写到一半，上游干净 EOF（_sse_guard_truncated_tool_args）
  2. 协议残片「标记 + 半截标签」被 fail-open 补发成正文（_marker_junk_only）
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *          # noqa: F401,F403

P, A = bootstrap()
declare_suite(__file__, ["qoder_proxy.py"])


def _frame(delta, finish=None):
    return ("data: " + json.dumps({"id": "c", "model": "m",
                                   "choices": [{"index": 0, "delta": delta,
                                                "finish_reason": finish}]},
                                  ensure_ascii=False) + "\n\n").encode()


def _tc(args=None, name=None, index=0):
    fn = {}
    if name:
        fn["name"] = name
    if args is not None:
        fn["arguments"] = args
    return {"index": index, "id": "call_1", "type": "function", "function": fn}


def _run(frames, allowed=("write_file",)):
    out = []
    try:
        for f in P.recover_leaked_tool_calls(iter(frames), set(allowed)):
            out.append(f)
    except Exception as exc:                      # noqa: BLE001
        return out, exc
    return out, None


# ---------------------------------------------------------------- 判定函数

# ------------------------------------------- 路径 1：tool_calls 截断 + 干净 EOF
_frames = [
    _frame({"role": "assistant", "tool_calls": [_tc(name="write_file")]}),
    _frame({"tool_calls": [_tc(args='{"path": "/home/admin/x.py", "conte')]}),
    b"data: [DONE]\n\n",
]
_out, _err = _run(_frames)
check("#112 参数截断 + 无 finish_reason -> 抛 EmptyToolEcho",
      isinstance(_err, P.EmptyToolEcho), type(_err).__name__)
check("#112 该错误继承 InvalidToolArguments（四条出口自动 fail-closed）",
      isinstance(_err, P.InvalidToolArguments))
check("#112 错误文案标明 truncated_arguments",
      "truncated_arguments" in (getattr(_err, "detail", "") or ""),
      getattr(_err, "detail", "")[:80])
check("#112 fail-closed 前已透传的帧保持原样（不吞已发出的内容）",
      len(_out) >= 1, len(_out))

# 参数跨多帧累积但最终完整 + 有 finish_reason -> 不抛
_ok = [
    _frame({"role": "assistant", "tool_calls": [_tc(name="write_file")]}),
    _frame({"tool_calls": [_tc(args='{"path": "/a.py",')]}),
    _frame({"tool_calls": [_tc(args=' "content": "hi"}')]}),
    _frame({}, "tool_calls"),
    b"data: [DONE]\n\n",
]
_out2, _err2 = _run(_ok)
check("#112 参数完整 + finish_reason=tool_calls -> 不抛错", _err2 is None,
      type(_err2).__name__ if _err2 else None)
check("#112 完整调用流仍逐帧透传", len(_out2) >= 4, len(_out2))

# 截断但上游给了 finish_reason（模型自己写坏参数）-> 不在本守卫范围内
_out3, _err3 = _run([
    _frame({"tool_calls": [_tc(args='{"a": 1')]}),
    _frame({}, "tool_calls"),
])
check("#112 有 finish_reason 时不越权改写语义（交给 #37 路径）", _err3 is None,
      type(_err3).__name__ if _err3 else None)

# ------------------------------------- 路径 2：#16 超限放行绕过 #72（marker-only 空壳）
# 实测日志：released oversized leak hold (21 chars / 201 frames) — fail-open
# 即 marker-only 的空壳块被「空转帧上限」这条兜底当正文补发了出去。
_spin = [_frame({"content": P.LEAK_MARKER})]
_spin += [_frame({}) for _ in range(210)]
_spin.append(_frame({}, "stop"))
_out4, _err4 = _run(_spin)
check("#112 marker-only 空壳 + 声明 tools + 空转超限 -> fail-closed",
      isinstance(_err4, P.EmptyToolEcho), type(_err4).__name__)
check("#112 错误文案标明 marker_only_released",
      "marker_only_released" in (getattr(_err4, "detail", "") or ""),
      getattr(_err4, "detail", "")[:80])
_joined = b"".join(_out4).decode("utf-8", "replace")
check("#112 空壳标记不会出现在已发出的正文里",
      P.LEAK_MARKER not in _joined, _joined[:120])

# 未声明 tools 时保持既有 fail-open 语义（不误伤无工具会话）
_out5, _err5 = _run(_spin, allowed=())
check("#112 未声明 tools -> 不抛错（保持旧行为）", _err5 is None,
      type(_err5).__name__ if _err5 else None)

finish()

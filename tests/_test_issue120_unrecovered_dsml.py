"""#120 后续：DSML 残片回读失败时不许当正文补发（fail-open -> fail-closed）。

问题现场（本机 8790，2026-10-30，已部署 #112 补丁之后）：
  17:38:49 unrecovered tool-protocol leak passed through
           (4828 chars, markers=0, dsml=3) at dsml-stream — issue #49
  17:46:19 unrecovered tool-protocol leak passed through
           (31 chars, markers=1, dsml=0) at stream-falsified — issue #49

4828 字符的 `<｜DSML｜` 残片被当成 assistant 正文发出，客户端把它解析进
工具参数，整轮任务静默中断（用户可见：助手只吐了几段命令/语法碎片就停）。

三条 fail-open 出口（本次全部改成「声明了 tools 就抛 EmptyToolEcho」）：
  1. _sse_recover_dsml           -> unrecovered_dsml
  2. _recover_leaked_tool_calls_inner else 分支 -> unrecovered_leak
  3. 同函数「候选被证伪」分支     -> unrecovered_falsified
未声明 tools 的会话保持旧 fail-open（普通散文里出现该标记不该被打断）。
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


def _run(frames, allowed=("write_file",)):
    out = []
    try:
        for f in P.recover_leaked_tool_calls(iter(frames), set(allowed)):
            out.append(f)
    except Exception as exc:                      # noqa: BLE001
        return out, exc
    return out, None


# ---------------------------------------------------------------- 判定函数
check("#120 判定函数：只有标记、没有协议残片 -> 不算残片（散文边界）",
      P._leak_residue_present(P.LEAK_MARKER) is False)
check("#120 判定函数：标记 + DSML 标签 -> 算残片",
      P._leak_residue_present(P.LEAK_MARKER + " <|DSML|calls>") is True)
check("#120 判定函数不把普通中文当残片",
      P._leak_residue_present("这是一段正常回复，提到 DSML 这个词。") is False)
check("#120 空串不算残片", P._leak_residue_present("") is False)

# ------------------------------------- 路径 1：dsml-stream 回读失败（4828 字符现场）
_garbage = P.LEAK_MARKER + "  这里是一堆认不出来的残片：<tool_call>{oops"
_frames = [
    _frame({"role": "assistant", "content": _garbage}),
    _frame({"content": "继续的碎片 </tool_call>"}),
    _frame({}, "stop"),
]
_out, _err = _run(_frames)
check("#120 DSML 残片回读失败 + 声明 tools -> 抛 EmptyToolEcho",
      isinstance(_err, P.EmptyToolEcho), type(_err).__name__)
check("#120 该错误继承 InvalidToolArguments（四条出口自动 fail-closed）",
      isinstance(_err, P.InvalidToolArguments))
check("#120 错误文案标明是未恢复残片（unrecovered_*）",
      "unrecovered_" in (getattr(_err, "detail", "") or ""),
      getattr(_err, "detail", "")[-60:])
_joined = b"".join(_out).decode("utf-8", "replace")
check("#120 残片标记没有出现在已发出的正文里",
      P.LEAK_MARKER not in _joined, _joined[:160])

# 未声明 tools：保持旧 fail-open（不能把普通散文里的标记判成失败）
_out2, _err2 = _run(_frames, allowed=())
check("#120 未声明 tools -> 不抛错（保持旧语义）", _err2 is None,
      type(_err2).__name__ if _err2 else None)
check("#120 未声明 tools 时残片仍按原样补发（行为未扩散）",
      P.LEAK_MARKER in b"".join(_out2).decode("utf-8", "replace"))

# ------------------------- 路径 2：能正常回读的 DSML 调用不受影响（不许误伤）
_good = (P.LEAK_MARKER + "\n"
         "<|DSML|calls>\n"
         '<|DSML|invoke name="write_file">\n'
         '<|DSML|parameter name="path">/tmp/a.txt</|DSML|parameter>\n'
         '<|DSML|parameter name="content">hi</|DSML|parameter>\n'
         "</|DSML|invoke>\n"
         "</|DSML|calls>\n")
_out3, _err3 = _run([
    _frame({"role": "assistant", "content": _good}),
    _frame({}, "stop"),
])
check("#120 可回读的 DSML 调用照旧恢复成 tool_calls（不抛错）",
      _err3 is None, type(_err3).__name__ if _err3 else None)
check("#120 恢复后的帧里带 tool_calls",
      any("tool_calls" in f.decode("utf-8", "replace") for f in _out3),
      len(_out3))

# --------- 路径 3（2026-10-30 17:58 实测复发）：只带 DSML 令牌、没有 LEAK_MARKER
# 日志原文：unrecovered tool-protocol leak passed through (58 chars, markers=0, dsml=3)
# at dsml-stream —— 旧判定要求「必须出现 LEAK_MARKER」，这条就漏过去了。
_dsml_only = "<|DSML|calls>\n</|DSML|invoke>\n</|DSML|calls>\n"
check("#120b 样例确实不含 LEAK_MARKER（复现 markers=0）",
      P.LEAK_MARKER not in _dsml_only and P.LEAK_MARKER_ALT not in _dsml_only)
check("#120b _dsml_residue_present 认出纯 DSML 令牌",
      P._dsml_residue_present(_dsml_only) is True)
_out4, _err4 = _run([
    _frame({"role": "assistant", "content": _dsml_only}),
    _frame({}, "stop"),
])
check("#120b 纯 DSML 残片在 tools 会话里 fail-closed（不再补发）",
      isinstance(_err4, P.EmptyToolEcho), type(_err4).__name__ if _err4 else None)
check("#120b 错误帧 detail 是 unrecovered_*",
      _err4 is not None and "unrecovered_" in getattr(_err4, "detail", ""),
      getattr(_err4, "detail", None) if _err4 else None)
check("#120b 已发出的正文里不得出现 DSML 令牌",
      "DSML" not in b"".join(_out4).decode("utf-8", "replace"))

# 反向：散文里只是「提到 DSML 这个词」不算残片
check("#120b 散文提到 DSML 但不含令牌 -> 不判残片",
      P._dsml_residue_present("这一轮我们讨论的是 DSML 协议为什么会漏。") is False)
_out5, _err5 = _run([
    _frame({"role": "assistant", "content": "这一轮讨论的是 DSML 协议为什么会漏。"}),
    _frame({}, "stop"),
])
check("#120b 提到 DSML 的散文照旧透传（不误伤）",
      _err5 is None and "DSML" in b"".join(_out5).decode("utf-8", "replace"),
      type(_err5).__name__ if _err5 else None)

finish()

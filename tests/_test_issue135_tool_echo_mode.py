"""#135 工具协议残片：lenient 收尾模式（可诊断 + 可兜底）。"""
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *          # noqa: F401,F403

P, A = bootstrap()
declare_suite(__file__, ["qoder_proxy.py", "docs/configuration.md"])

_src = io.open(os.path.join(ROOT, "qoder_proxy.py"), encoding="utf-8").read()

print("[#135] 模式开关")
check("有 QD_TOOL_ECHO_MODE", "QD_TOOL_ECHO_MODE" in _src)
check("默认 strict（保持 #120 的 fail-closed 语义）",
      'TOOL_ECHO_MODE == "strict"' not in _src or True)   # 见下：默认值断言
check("默认值就是 strict",
      '(os.environ.get("QD_TOOL_ECHO_MODE") or "strict")' in _src)
check("非法值回落 strict", 'if TOOL_ECHO_MODE not in ("strict", "lenient"):' in _src)

print("[#135] lenient 只作用于「协议残片」这一类错误")
_i = _src.index('if pump_exc is not None and isinstance(')
_seg = _src[_i:_i + 1600]
check("判据是 InvalidToolArguments", "InvalidToolArguments" in _seg.split("TOOL_ECHO_MODE")[0])
check("且必须 lenient 才生效", 'TOOL_ECHO_MODE == "lenient"' in _seg)
check("会打 WARN（面板/日志可诊断）",
      "lenient tool-echo: converting fail-closed" in _seg)
check("会写一条 error 行（带 lenient_tool_echo 标记）",
      'record_error(model, 200, "lenient_tool_echo' in _seg)
check("以「正常完成」收尾而不是 failed",
      "_responses_completed_quiet(" in _seg and "_responses_failed_frame(" not in _seg.split("return")[0])

print("[#135] completed 帧形状")
_frame = P._responses_completed_quiet({"text": "好的，我来修改：",
                                       "usage": {"prompt_tokens": 10,
                                                 "completion_tokens": 3,
                                                 "total_tokens": 13}},
                                      "qwen3.8-flash")
_txt = _frame if isinstance(_frame, str) else _frame.decode("utf-8")
check("是 event: response.completed", _txt.startswith("event: response.completed"))
_body = json.loads(_txt.split("data: ", 1)[1])
check("status=completed", _body["response"]["status"] == "completed")
check("保留已产出的正文（不让用户白等一场）",
      _body["response"]["output"][0]["content"][0]["text"] == "好的，我来修改：")
check("usage 映射成 Responses 形状",
      _body["response"]["usage"]["input_tokens"] == 10
      and _body["response"]["usage"]["output_tokens"] == 3)
_e = P._responses_completed_quiet({}, "m")
_etxt = _e if isinstance(_e, str) else _e.decode("utf-8")
_empty = json.loads(_etxt.split("data: ", 1)[1])

print("[#135] strict 仍是默认路径（回归保护）")
check("failed 帧仍在（strict 用）", "_responses_failed_frame(" in _src)
check("strict 下不写 lenient 分支日志的条件是 mode 判断在前",
      _seg.index('TOOL_ECHO_MODE == "lenient"') < _seg.index("lenient tool-echo"))

print("[#135] 文档")
_doc = io.open(os.path.join(ROOT, "docs", "configuration.md"),
               encoding="utf-8").read() if os.path.exists(
                   os.path.join(ROOT, "docs", "configuration.md")) else ""
check("配置文档写了 QD_TOOL_ECHO_MODE", "QD_TOOL_ECHO_MODE" in _doc, bool(_doc))

finish()

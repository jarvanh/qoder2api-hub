"""#135 后续：工具协议残片收尾策略可在面板切换（strict/lenient）+ #139 排队诊断。"""
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *          # noqa: F401,F403

P, A = bootstrap()
declare_suite(__file__, ["qoder_proxy.py", "qoder_settings.py", "dashboard.html"])
import qoder_settings as ST        # noqa: E402

_src = io.open(os.path.join(ROOT, "qoder_proxy.py"), encoding="utf-8").read()
_dash = io.open(os.path.join(ROOT, "dashboard.html"), encoding="utf-8").read()

print("[#135+] 设置层读写")
_tmp = tempfile.mkdtemp(prefix="tem135-")
check("默认空（跟随环境/内置）", ST.tool_echo_mode(_tmp) == "")
ST.set_tool_echo_mode(_tmp, "lenient")
check("写入 lenient", ST.tool_echo_mode(_tmp) == "lenient")
ST.set_tool_echo_mode(_tmp, "bogus")
check("非法值 -> 清除（回落默认）", ST.tool_echo_mode(_tmp) == "")
ST.set_tool_echo_mode(_tmp, "strict")
check("写入 strict", ST.tool_echo_mode(_tmp) == "strict")

print("[#135+] 运行时优先级：环境变量 > 面板 > 内置")
_orig_dir = P.ACCOUNTS_DIR
_orig_env = os.environ.get("QD_TOOL_ECHO_MODE")
P.ACCOUNTS_DIR = _tmp
os.environ.pop("QD_TOOL_ECHO_MODE", None)
P.CONVERSATION_SHARE  # noqa: B018  (确保模块已加载)
check("面板 strict -> effective strict", P.tool_echo_mode() == "strict")
ST.set_tool_echo_mode(_tmp, "lenient")
check("面板 lenient -> effective lenient", P.tool_echo_mode() == "lenient")
os.environ["QD_TOOL_ECHO_MODE"] = "strict"
check("环境变量压过面板", P.tool_echo_mode() == "strict")
os.environ["QD_TOOL_ECHO_MODE"] = "bogus"
check("环境变量非法则忽略", P.tool_echo_mode() == "lenient")
if _orig_env is None:
    os.environ.pop("QD_TOOL_ECHO_MODE", None)
else:
    os.environ["QD_TOOL_ECHO_MODE"] = _orig_env
P.ACCOUNTS_DIR = _orig_dir

print("[#135+] 收尾判断走运行时查询（改完立即生效）")
check("不再读模块常量",
      'and tool_echo_mode() == "lenient":' in _src)
check("常量仍作默认兜底", 'TOOL_ECHO_MODE = (os.environ.get("QD_TOOL_ECHO_MODE")' in _src)

print("[#135+] 接口与面板")
check("GET /settings 回显 tool_echo_mode", '"tool_echo_mode": tool_echo_mode()' in _src)
check("POST /settings/tool-echo-mode", 'if path == "/settings/tool-echo-mode":' in _src)
check("非法值 400", "mode must be strict or lenient" in _src)
_route = _src[_src.index('if path == "/settings/tool-echo-mode":'):][:400]
check("需要面板会话", "panel password required" in _route)
check("面板有下拉框", 'id="setToolEcho"' in _dash)
check("面板有保存函数", "function saveToolEchoMode(" in _dash or
      "async function saveToolEchoMode(" in _dash)
check("环境变量优先时面板给出提示", "由环境变量固定为" in _dash)

print("[#139] 排队诊断端点仍可用")
check("路由在", '"/diag/queue"' in _src)
check("方法在", "def _handle_diag_queue" in _src)

finish()

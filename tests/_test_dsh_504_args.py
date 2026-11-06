"""DSH 两类 502/504 的收口：坏 arguments 尽量修复 + 首字超时换账号。"""
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *          # noqa: F401,F403

P, A = bootstrap()
declare_suite(__file__, ["qoder_proxy.py"])

_src = io.open(os.path.join(ROOT, "qoder_proxy.py"), encoding="utf-8").read()

print("[504] 首字超时：同账号重试没有意义，直接换账号")
check("有 _is_first_token_timeout", "def _is_first_token_timeout(" in _src)
check("识别 First Token Timeout",
      P._is_first_token_timeout("First Token Timeout or Upstream Timeout") is True)
check("识别 code 504 的整段详情",
      P._is_first_token_timeout('{"code":"504","message":"First Token Timeout"}') is True)
check("普通错误不误判", P._is_first_token_timeout("boom") is False)
check("空值不误判", P._is_first_token_timeout(None) is False)
_i = _src.index("if _is_first_token_timeout(detail):")
_seg = _src[_i:_i + 500]
check("命中后 break（交给外层换号）", "break" in _seg)
check("命中后不再走同账号退避重试",
      _seg.index("break") < _seg.index("_is_transient_upstream(exc.code, detail)"))
check("记一条 WARN 便于排查", "rotating to another account" in _seg)

print("[args] 坏 JSON 参数尽量修复，修不好才 fail-closed")
check("有 _repair_args_json", "def _repair_args_json(" in _src)
check("内层裸引号能修",
      json.loads(P._repair_args_json('{"command":"echo "hi" && ls"}'))["command"]
      == 'echo "hi" && ls')
check("尾随逗号能修", json.loads(P._repair_args_json('{"a":1,}')) == {"a": 1})
check("合法 JSON 原样返回", P._repair_args_json('{"a":1}') == '{"a":1}')
check("彻底坏的返回 None（不伪造调用）", P._repair_args_json("{bad") is None)
check("空串返回空串", P._repair_args_json("") == "")

print("[args] 解析器与坏参数判定都用上修复")
_p = _src.index("def parse_leaked_tool_calls(")
_pseg = _src[_p:_src.index("def _leak_body_offset(", _p)]
check("parse 里 arguments 非法先尝试修复", "_repair_args_json(args)" in _pseg)
check("修复成功会计数（RECOVERY_STATS.args_repaired）",
      'RECOVERY_STATS["args_repaired"]' in _pseg.replace('\"', '"'))
_b = _src.index("def _echo_with_bad_args(")
_bseg = _src[_b:_src.index("def ", _b + 10)]
check("坏参数判定容忍「可修复」的参数",
      "if _repair_args_json(args) is None:" in _bseg)

# 行为：一条「内层引号未转义」的协议回声应能被还原成真实调用
_echo = ('\n\n[assistant 请求调用工具]\n'
         '[{"name": "Bash", "arguments": "{\\"command\\":\\"echo \\"hi\\" && ls\\"}"}]')
_calls, _clean = P.parse_leaked_tool_calls(_echo, {"Bash"})
check("坏引号的回声能被还原（不再整轮 502）", bool(_calls), _calls)
if _calls:
    _args = json.loads(_calls[0]["function"]["arguments"])
    check("还原后的参数语义正确", _args.get("command") == 'echo "hi" && ls', _args)
    check("工具名正确", _calls[0]["function"]["name"] == "Bash")

print("[args] 真坏（无法修复）仍然 fail-closed")
_bad = '\n\n[assistant 请求调用工具]\n[{"name": "Bash", "arguments": "{bad"}]'
check("不伪造调用", P.parse_leaked_tool_calls(_bad, {"Bash"})[0] is None)
check("判定为坏参数（保持整轮失败语义）",
      P._echo_with_bad_args(_bad, {"Bash"}) is True)

finish()

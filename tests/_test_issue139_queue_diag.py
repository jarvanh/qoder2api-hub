"""#139 排队诊断端点（/diag/queue）：区分「上游排队」与「账号/身份问题」。"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *          # noqa: F401,F403

P, A = bootstrap()
declare_suite(__file__, ["qoder_proxy.py", "docs/api.md"])

_src = io.open(os.path.join(ROOT, "qoder_proxy.py"), encoding="utf-8").read()

print("[#139] 路由与鉴权")
check("允许清单里有 /diag/queue", '"/diag/queue"' in _src)
check("dispatch 走 _handle_diag_queue", "self._handle_diag_queue(payload)" in _src)
_i = _src.index('if path == "/diag/queue":')
_seg = _src[_i:_i + 400]
check("需要面板会话", "panel password required" in _seg)

print("[#139] 探测与解析")
_j = _src.index("def _handle_diag_queue")
_body = _src[_j:_src.index("def _handle_diag_checkin", _j)]
check("发最小探测（max_tokens=8、非流式）",
      '"max_tokens": 8' in _body and '"stream": False' in _body)
check("解析 retryAfterSeconds", "retryAfterSeconds" in _body)
check("解析 queueCount", "queueCount" in _body)
check("识别排队/额度/正常三类", '"queued"' in _body and '"exhausted"' in _body
      and '"ok"' in _body)
check("回显机器身份来源（native/derived）",
      'out["identity_source"]' in _body)
check("排队时给出「与凭证无关」+ fast 模式建议",
      "与 Key/账号凭证无关" in _body and "QD_QUEUE_WAIT_MODE=fast" in _body)
check("额度类给「充值或换号」", "充值或换号" in _body)
check("上游异常不 500（结构化返回）", "except Exception as exc:" in _body)

print("[#139] 与 #137 的文案联动")
check("排队/额度文案已去 403（#137 合并后）",
      "_neutralize_auth_codes" in _src)
check("110 有专门分支", "quota_daily_limit" in _src)

print("[#139] 文档")
_doc = io.open(os.path.join(ROOT, "docs", "api.md"), encoding="utf-8").read()
check("/diag/queue 写进接口文档", "/diag/queue" in _doc)

finish()

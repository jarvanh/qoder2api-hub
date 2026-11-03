"""#123 后续：账号测试 403 诊断 + /diag/vm 来源标注。"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *          # noqa: F401,F403

P, A = bootstrap()
declare_suite(__file__, ["qoder_proxy.py", "dashboard.html"])

_src = io.open(os.path.join(ROOT, "qoder_proxy.py"), encoding="utf-8").read()
_dash = io.open(os.path.join(ROOT, "dashboard.html"), encoding="utf-8").read()

print("[#123+] 账号测试：信封层 403 不再只说 upstream status")
_i = _src.index('if path == "/accounts/test":')
_seg = _src[_i:_i + 6000]
check("有 UpstreamStatus 专门分支", "except UpstreamStatus as exc:" in _seg)
check("回包带 detail（原样回显上游文案）", '"detail": _detail' in _seg)
check("回包带 hint（人话解释）", '"hint": _hint' in _seg)
check("测试失败也写日志（带业务码线索）",
      "upstream=%s %s" in _seg or "upstream=" in _seg)

print("[#123+] 业务码 -> 人话")
cases = [
    ('{"code":"10605","isQueued":true}', "排队"),
    ('{"code":"112","message":"{\\"pricingUrl\\":\\"https://qoder.com.cn\\"}"}', "额度"),
    ('{"code":"110","message":"Billing daily count exceeded"}', "上限"),
    ('{"code":"430"}', "版本"),
    ('{"error":"TOKEN_EXPIRE"}', "凭证"),
    ('{"weird":"x"}', ""),
]
for detail, want in cases:
    got = P._quota_hint(detail)
    ok = (want in got) if want else (got == "")
    check("hint(%s) -> %s" % (detail[:28], want or "空"), ok, got)

print("[#123+] /diag/vm 标注来源")
check("回显 bridge_available", '_st["bridge_available"] = bool(_exe)' in _src)
check("回显 bridge_vm_info（只带判定相关四个键）",
      '_st["bridge_vm_info"] = {' in _src
      and all(k in _src[_src.index('_st["bridge_vm_info"]'):][:400]
              for k in ("isVm", "brand", "percentage", "vmTypeCode")))
check("回显 bridge_exe 路径（空串=没找到桥）",
      '_st["bridge_exe"] = _exe or ""' in _src)
check("回显 local_evidence（本地交叉校验证据）",
      '_st["local_evidence"]' in _src)

print("[#123+] 面板把两个来源分开写")
check("渲染「本地硬件：」一行", "'本地硬件：'" in _dash)
check("渲染「官方风控：」一行", "'官方风控：'" in _dash)
check("桥不可用时明确说桥不可用",
      "桥不可用（本机未找到 runtime-info）" in _dash)
check("不再把本地结论冒充成官方风控（旧的单行提示已移除）",
      "'官方风控桥不可用，以下为本机交叉校验'" not in _dash)

print("[#123+] SBC 判定仍然生效（v1.7.11 的能力不回归）")
import qoder_fingerprint as F       # noqa: E402
_orig = F._linux_sbc_model
F._linux_sbc_model = lambda: "Raspberry Pi 3 Model B Plus Rev 1.3"
_ev, _hint = F._local_vm_evidence()
check("本地证据写明单板计算机", any("单板计算机" in e for e in _ev), _ev)
check("本地不给出虚拟化品牌", _hint == "")
F._linux_sbc_model = _orig

finish()

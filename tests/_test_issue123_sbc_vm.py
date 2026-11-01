"""#123 树莓派等单板计算机不得被误判成 KVM 虚拟机。"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *          # noqa: F401,F403

P, A = bootstrap()
declare_suite(__file__, ["qoder_fingerprint.py"])
import qoder_fingerprint as F       # noqa: E402


print("[#123] 设备树型号识别")
check("有 _linux_sbc_model 助手", hasattr(F, "_linux_sbc_model"))
check("SBC 标记覆盖树莓派系列",
      any("raspberry pi" in m for m in F._SBC_MARKERS))
check("本机不是 SBC（Windows/macOS 上返回空）",
      F._linux_sbc_model() == "")

_orig_open = None
print("[#123] 命中 SBC 时不参与虚拟化判定")
_real_sbc = F._linux_sbc_model
F._linux_sbc_model = lambda: "Raspberry Pi 3 Model B Plus Rev 1.3"
_ev, _hint = F._local_vm_evidence()
check("证据里写明是单板计算机",
      any("单板计算机" in e for e in _ev), _ev)
check("不给出虚拟化品牌（hint 为空）", _hint == "", _hint)
check("证据里不含 hypervisor / 虚拟化驱动字样",
      not any(("hypervisor" in e.lower() or "虚拟化驱动" in e) for e in _ev), _ev)
_st = F.vm_status(bridge_available=False)
check("vm_status 结论不是虚拟机", _st["is_vm"] is False, _st["is_vm"])
F._linux_sbc_model = _real_sbc

print("[#123] 未命中 SBC 时行为不变（继续按原逻辑判定）")
F._linux_sbc_model = lambda: ""
_ev2, _hint2 = F._local_vm_evidence()
check("没有 SBC 证据串进普通机器", not any("单板计算机" in e for e in _ev2), _ev2)
F._linux_sbc_model = _real_sbc

print("[#123] 阈值与文案不变")
check("评分档位仍按 70/40/>0 分档",
      F._vm_level_cn(70) == "高" and F._vm_level_cn(40) == "中"
      and F._vm_level_cn(1) == "低" and F._vm_level_cn(0) == "无")

finish()

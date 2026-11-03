"""#132 一键更新按钮按 can_apply 门控 + #134 账号测试探活与提示。"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *          # noqa: F401,F403

P, A = bootstrap()
declare_suite(__file__, ["qoder_proxy.py", "dashboard.html"])
import qoder_catalog as C          # noqa: E402

_src = io.open(os.path.join(ROOT, "qoder_proxy.py"), encoding="utf-8").read()
_dash = io.open(os.path.join(ROOT, "dashboard.html"), encoding="utf-8").read()

print("[#132] /update/plan 给出权威可执行性")
_i = _src.index('if path == "/update/plan":')
_seg = _src[_i:_i + 1800]
check("回显 client_is_loopback", 'client_is_loopback' in _seg)
check("回显 apply_blocked_reasons", 'apply_blocked_reasons' in _seg)
check("can_apply 被阻断原因覆盖",
      '_info["can_apply"] = bool(_info.get("can_apply")) and not _reasons' in _seg)
for token, why in (("QD_UPDATE_DISABLE=1", "环境变量禁用"),
                   ("容器", "容器内"),
                   ("QD_UPDATE_ALLOW_REMOTE=1", "非本机"),
                   ("已是最新版本", "无新版本")):
    check("阻断原因含：%s" % why, token in _seg)

print("[#132] 面板按 can_apply 渲染按钮")
check("先取 /update/plan", "getJSON('/update/plan')" in _dash)
check("只有 can_apply 才给一键更新按钮",
      "if(canApply){" in _dash and "applyUpdate(this)" in _dash)
check("否则展示阻断原因 + 手动路径",
      "无法一键更新：' + why.join" in _dash and "下载新 release 覆盖安装目录" in _dash)

print("[#134] 测试默认用免费模型探活")
check("有 _default_test_model", "def _default_test_model(" in _src)
check("0 价优先（price_factor == 0）",
      'm.get("price_factor") == 0' in _src)
check("is_free 只作次选（它表示含免费权益，不等于 0 价）",
      'bool(m.get("is_free"))' in _src)
_cn = P._default_test_model("cn")
_intl = P._default_test_model("intl")
check("CN 默认探活模型是真免费模型", "Flash" in _cn or "flash" in _cn, _cn)
check("INTL 默认探活模型同样免费", "Flash" in _intl or "flash" in _intl, _intl)
_i2 = _src.index('if path == "/accounts/test":')
_seg2 = _src[_i2:_i2 + 2000]
check("测试入口用 _default_test_model 兜底", "_default_test_model(" in _seg2)
check("显式 model 仍以调用方为准",
      'payload.get("model")' in _seg2.split("_default_test_model(")[0])

print("[#134] 额度/排队类失败不再给账号叠冷却")
check("112/110/10605 被排除在 note_error 之外",
      '"112" not in _detail' in _src and '"10605" not in _detail' in _src)

print("[#134] 面板 toast 优先显示 hint")
check("hint 放前面", "const _head = r.hint ?" in _dash)
check("原文放括号里（仍可追查）",
      "'（' + (r.error || r.status || '异常') + '）'" in _dash)

finish()

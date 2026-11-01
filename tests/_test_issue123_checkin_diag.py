"""#123 签到诊断端点（只读，不误领）。"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *          # noqa: F401,F403

P, A = bootstrap()
declare_suite(__file__, ["qoder_proxy.py"])

_src = io.open(os.path.join(ROOT, "qoder_proxy.py"), encoding="utf-8").read()

print("[#123] 路由与鉴权")
check("允许清单里有 /diag/checkin",
      '"/diag/checkin"):' in _src)
check("dispatch 走 _handle_diag_checkin 且要 panel_ok",
      'if path == "/diag/checkin":' in _src
      and "_handle_diag_checkin(payload)" in _src)
_disp = _src[_src.index('if path == "/diag/checkin":'):][:300]
check("未带面板会话时 401（不是裸奔诊断）",
      "panel password required" in _disp)

print("[#123] 只读语义")
_i = _src.index("def _handle_diag_checkin")
_seg = _src[_i:_src.index("def _handle_accounts", _i)]
check("不发起领取调用（只读诊断）",
      ".checkin()" not in _seg
      and "claim_campaign(" not in _seg
      and "_checkin_claim" not in _seg)
check("只调 status / capability / campaigns / can_checkin",
      all(k in _seg for k in ("checkin_status", "checkin_capability",
                              "campaigns", "can_checkin")))
check("上游异常也返回结构化 JSON（不 500）",
      _seg.count("except Exception") >= 4)

print("[#123] 回显内容")
check("回显上游 status 原始细节",
      'out["status"] = {"ok": ok, "detail": st}' in _seg)
check("活动按字段原样列 keys + 标量（各区域字段名不同）",
      "sorted(c.keys())[:12]" in _seg and "scalars" in _seg)
check("用 campaign_label 生成可读名（模块级函数，需限定名）",
      "qoder_accounts.campaign_label(c)" in _seg)
check("不涉及 token / access_token 回显",
      "access_token" not in _seg and "refresh_token" not in _seg)

finish()

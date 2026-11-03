"""#129 每次调用的积分消耗可视化（最近请求新增积分列 + 本页合计）。"""
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *          # noqa: F401,F403

P, A = bootstrap()
declare_suite(__file__, ["qoder_proxy.py", "dashboard.html"])

_src = io.open(os.path.join(ROOT, "qoder_proxy.py"), encoding="utf-8").read()
_dash = io.open(os.path.join(ROOT, "dashboard.html"), encoding="utf-8").read()

print("[#129] 后端已经把积分写进每一行")
check("_extract_usage 读 credits（复数优先，兼容单数）",
      'usage.get("credits") or usage.get("credit")' in _src)
check("保留 original_credits（折扣前价格）",
      '"original_credits": usage.get("original_credits")' in _src)
check("credit/original_credits 进入统计字段", '"credit"' in _src)

print("[#129] 面板：最近请求有「积分」列")
check("表头新增积分列", ">积分</th>" in _dash)
check("行内有 creditCell", "creditCell(r)" in _dash)
check("悬停有明细（本次扣费/原价/是否计费）",
      "本次扣费 " in _dash and "原价 " in _dash and "不计费" in _dash)
check("折扣时划掉原价（避免误读成涨价）",
      "text-decoration:line-through" in _dash and "original_credits" in _dash)

print("[#129] 面板：本页合计")
check("有 recentCreditSummary", "function recentCreditSummary(" in _dash)
check("合计挂在表格下方", "+ recentCreditSummary(rows)" in _dash)
check("合计里区分原价", "原价 <span" in _dash or "原价 " in _dash)

print("[#129] 边界：失败行 / 0 积分 / 大额")
seg = _dash[_dash.index("function creditCell("):_dash.index("function recentCreditSummary(")]
check("失败行显示 — 而不是 0（不把失败算成免费）",
      "if(r.error) return" in seg)
check("无积分显示 0（灰色）", chr(34) + "color:var(--dim)" + chr(34) + ">0<" in seg)
check("大额（>=100）用醒目色", "shown >= 100" in seg)
check("合计跳过失败行", "if(r.error) return;" in
      _dash[_dash.index("function recentCreditSummary("):][:400])

finish()

"""#129 后续：积分三段拆分（单行看出贵在哪段）。"""
import io
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *          # noqa: F401,F403

P, A = bootstrap()
declare_suite(__file__, ["qoder_proxy.py", "dashboard.html"])

_src = io.open(os.path.join(ROOT, "qoder_proxy.py"), encoding="utf-8").read()
_dash = io.open(os.path.join(ROOT, "dashboard.html"), encoding="utf-8").read()

print("[#129+] 系数与折算")
check("默认系数来自实测拟合（1.069/0.086/4.277）",
      P.credit_coefficients() == (1.069, 0.086, 4.277), P.credit_coefficients())
check("可用 QD_CREDIT_COEF 覆盖", "QD_CREDIT_COEF" in _src)
_old = os.environ.get("QD_CREDIT_COEF")
os.environ["QD_CREDIT_COEF"] = "1,2,3"
check("覆盖生效", P.credit_coefficients() == (1.0, 2.0, 3.0), P.credit_coefficients())
if _old is None:
    os.environ.pop("QD_CREDIT_COEF", None)
else:
    os.environ["QD_CREDIT_COEF"] = _old

# 报告者给的三行现场数据（应能复算出他的积分）
_r1 = P.split_credit(336146, 0, 129)
check("336k 全价 → 约 360 积分",
      abs((_r1["input"] + _r1["cached"] + _r1["output"]) - 360.0) < 2.0, _r1)
_r2 = P.split_credit(338598, 24416, 1510)
check("338k + 24k 缓存 → 约 344.5 积分",
      abs((_r2["input"] + _r2["cached"] + _r2["output"]) - 344.5) < 2.0, _r2)
check("缓存段按 8% 计价（远小于同量非缓存）",
      _r2["cached"] < _r2["input"] * 0.1, _r2)
check("缓存不能超过 prompt（脏数据钳制）",
      P.split_credit(1000, 5000, 0)["cached"] == round(1000 * 0.086 / 1000, 4))
check("负值/非法值不炸", P.split_credit(-1, "x", None) ==
      {"input": 0.0, "cached": 0.0, "output": 0.0})

print("[#129+] 缓存字段缺失要能区分")
_ex1 = P._extract_usage({"prompt_tokens": 10, "completion_tokens": 1})
check("上游没给缓存字段 -> cached_reported=False",
      _ex1.get("cached_reported") is False, _ex1)
_ex2 = P._extract_usage({"prompt_tokens": 10, "cached_tokens": 0})
check("上游给了（值为 0）-> cached_reported=True",
      _ex2.get("cached_reported") is True, _ex2)
_ex3 = P._extract_usage({"prompt_tokens": 10,
                         "prompt_tokens_details": {"cached_tokens": 5}})
check("嵌套标准位也算已回报", _ex3.get("cached_reported") is True)

print("[#129+] 全价请求卡：缓存字段缺失的行不计入")
_tmp = tempfile.mkdtemp(prefix="fp2-")
_ol, _od = P.USAGE_LOG, P.USAGE_DIR
P.USAGE_DIR, P.USAGE_LOG = _tmp, os.path.join(_tmp, "usage.jsonl")
_now = time.time()
_rows = [
    {"at": _now - 10, "model": "m", "prompt_tokens": 200000, "completion_tokens": 1,
     "total_tokens": 200001, "cached_tokens": 0, "cached_reported": True, "credit": 200.0},
    {"at": _now - 20, "model": "m", "prompt_tokens": 200000, "completion_tokens": 1,
     "total_tokens": 200001, "cached_tokens": 0, "cached_reported": False, "credit": 200.0},
]
with open(P.USAGE_LOG, "w", encoding="utf-8") as fh:
    for r in _rows:
        fh.write(json.dumps(r) + "\n")
_d = P.compute_usage_stats(range="today")
check("只把「确认零缓存」的算成全价",
      (_d.get("full_price") or {}).get("requests") == 1, _d.get("full_price"))
P.USAGE_LOG, P.USAGE_DIR = _ol, _od

print("[#129+] /usage/recent 每行带 credit_split")
check("recent 里注入 credit_split",
      '_row["credit_split"] = split_credit(' in _src)

print("[#129+] 面板：积分拆分列")
check("表头有「积分拆分」", "积分拆分</th>" in _dash)
check("有 creditSplitCell 渲染器", "function creditSplitCell(" in _dash)
check("三段都有颜色区分（输入/缓存/输出）",
      "color:#e08b2b" in _dash and "color:#26bb82" in _dash
      and "color:var(--think)" in _dash)
check("名义成本行标注「名义」", "名义</span>" in _dash)
check("缓存字段缺失显示 ?", 'title="上游没有回报缓存字段' in _dash)
check("悬停给出三段与合计", "非缓存输入 ' + (sp.input || 0)" in _dash)
check("合计行也按三段拆开", "（拆分：' + splitTxt + '）" in _dash)

finish()

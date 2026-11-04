"""#129 后续：全价请求（缓存为 0 的超长 prompt）可视化。"""
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

print("[#129+] 后端：全价请求指标")
check("阈值可配（默认 100k）", "FULL_PRICE_PROMPT_MIN" in _src)
check("统计里累计 requests/prompt/credit",
      'full_price["requests"] += 1' in _src
      and 'full_price["prompt"] += prompt' in _src
      and 'full_price["credit"] += _credit' in _src)
check("判据 = prompt >= 阈值 且 cached <= 0",
      "prompt >= FULL_PRICE_PROMPT_MIN and cached <= 0" in _src)
check("输出带占比与阈值",
      '"credit_share"' in _src and '"threshold": FULL_PRICE_PROMPT_MIN' in _src)

# 行为：真跑一遍统计，验证口径
_tmp = tempfile.mkdtemp(prefix="fp129-")
_orig_log, _orig_ud = P.USAGE_LOG, P.USAGE_DIR
P.USAGE_DIR = _tmp
P.USAGE_LOG = os.path.join(_tmp, "usage.jsonl")
now = time.time()
rows = [
    # 全价：20 万 prompt、0 缓存
    {"at": now - 60, "model": "DeepSeek-Flash", "prompt_tokens": 200000,
     "completion_tokens": 100, "total_tokens": 200100,
     "cached_tokens": 0, "credit": 300.0},
    # 全价：刚好 10 万
    {"at": now - 120, "model": "DeepSeek-Flash", "prompt_tokens": 100000,
     "completion_tokens": 50, "total_tokens": 100050,
     "cached_tokens": 0, "credit": 100.0},
    # 不算：缓存命中
    {"at": now - 180, "model": "DeepSeek-Flash", "prompt_tokens": 200000,
     "completion_tokens": 50, "total_tokens": 200050,
     "cached_tokens": 150000, "credit": 10.0},
    # 不算：prompt 小
    {"at": now - 240, "model": "DeepSeek-Flash", "prompt_tokens": 5000,
     "completion_tokens": 50, "total_tokens": 5050,
     "cached_tokens": 0, "credit": 1.0},
]
with open(P.USAGE_LOG, "w", encoding="utf-8") as fh:
    for r in rows:
        fh.write(json.dumps(r, ensure_ascii=False) + "\n")
d = P.compute_usage_stats(range="today")
fp = d.get("full_price") or {}
check("只统计「大 + 无缓存」的两条", fp.get("requests") == 2, fp.get("requests"))
check("积分合计只含那两条", abs((fp.get("credit") or 0) - 400.0) < 1e-6, fp.get("credit"))
check("占比按窗口总积分算（400/411）",
      fp.get("credit_share") is not None and abs(fp["credit_share"] - 97.3) < 0.2,
      fp.get("credit_share"))
check("prompt 合计只含那两条", fp.get("prompt") == 300000, fp.get("prompt"))
P.USAGE_LOG, P.USAGE_DIR = _orig_log, _orig_ud

print("[#129+] 面板：卡片与渲染")
check("区块标题「全价请求」", "全价请求" in _dash)
check("容器 id", 'id="fullPriceWrap"' in _dash)
check("渲染函数", "function renderFullPrice(" in _dash)
check("无此类请求时给「前缀复用良好」的正向文案",
      "前缀复用良好" in _dash)
check("有请求时展示次数与积分占比",
      "次</span>" in _dash and "占窗口积分" in _dash)
check("给出可操作建议（保持前缀稳定/压缩历史）",
      "保持前缀稳定" in _dash and "压缩历史" in _dash)
check("挂进 stats 回调", "renderFullPrice(statData && statData.full_price)" in _dash)

finish()

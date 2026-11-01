# -*- coding: utf-8 -*-
"""池级配额耗尽的对外语义：402（2026-11-01，对齐 zcode2api b779919）。

    python tests/_test_quota_egress_402.py

钉住三组语义：
  A) _realm_quota_parked 判定：
     1) 全部账号 110 日计数停放 -> True，wait 指向解封时刻；
     2) 全部账号余额 402 停放 -> True；
     3) G2 当日预算护栏挡住 -> True（按日界解封）；
     4) 任一账号就绪 -> False；
     5) 仅错误冷却（cooldown_until）/ 频控（model_cooldowns）挡住 -> False
        （传输抖动整池冷 15s 不得误报「额度耗尽」）；
     6) 空池 / 无本区域账号 -> False。
  B) QuotaExhausted：
     7) message 保持 "no usable account" 前缀（出口翻译分支的前置条件）；
     8) _exc_http_status 认 402（流内路径兜底）；
     9) open_upstream 全停放空手 -> QuotaExhausted（wait>0）。
  C) 出口接线（静态钉住）：
     10) 三个出口 + _exc_http_status 共 4 处 isinstance 分流；
     11) 402 分支带 err_type=model_no_quota。
"""
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("ACCOUNTS_DIR", os.path.join(ROOT, "_acc"))
import qoder_accounts as A
import qoder_proxy as P

PASS = FAIL = 0


def check(label, cond, extra=None):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] %s  %r" % (label, extra))


MODEL = "qwen3.8-flash"


def build(realms):
    accs = []
    for i, realm in enumerate(realms):
        a = A.Account({"uid": "%s-%d" % (realm, i), "realm": realm,
                       "accessToken": "dt-%s%d" % (realm, i)})
        a.expires_at = time.time() + 86400
        accs.append(a)
    pool = A.AccountPool(tempfile.mkdtemp(), log=lambda *a, **k: None)
    pool.accounts = accs
    return pool, accs


import os as _os_fp
import sys as _sys_fp
_sys_fp.path.insert(0, _os_fp.path.dirname(_os_fp.path.abspath(__file__)))
from _suite_head import declare_suite
declare_suite(__file__, ["qoder_proxy.py", "qoder_accounts.py"])
print("[402] 池级配额耗尽出口语义（对齐 zcode2api b779919）")

# ---- A) _realm_quota_parked ----
_pool1, _accs1 = build(["cn"])
_accs1[0].billing_until = time.time() + 3600
P.POOL = _pool1
_parked, _wait = P._realm_quota_parked("cn", MODEL)
check("1) 全账号 110 日计数停放 -> True", _parked is True, (_parked, _wait))
check("1) wait 指向解封时刻（约 3600s）", 3500 <= _wait <= 3600, _wait)

_pool2, _accs2 = build(["cn"])
_accs2[0].balance_until = time.time() + 1800
P.POOL = _pool2
check("2) 全账号余额 402 停放 -> True",
      P._realm_quota_parked("cn", MODEL)[0] is True)

_pool3, _accs3 = build(["cn"])
_accs3[0].daily_token_limit = 100
_accs3[0].daily_tokens_today = 100
P.POOL = _pool3
check("3) G2 当日预算护栏挡住 -> True",
      P._realm_quota_parked("cn", MODEL)[0] is True)

_pool4, _accs4 = build(["cn", "cn"])
_accs4[0].billing_until = time.time() + 3600
P.POOL = _pool4
check("4) 任一账号就绪 -> False",
      P._realm_quota_parked("cn", MODEL)[0] is False)

_pool5, _accs5 = build(["cn"])
_accs5[0].cooldown_until = time.time() + 600
P.POOL = _pool5
check("5) 仅错误冷却挡住 -> False（不算配额）",
      P._realm_quota_parked("cn", MODEL)[0] is False)

_pool6, _accs6 = build(["cn"])
_accs6[0].model_cooldowns[MODEL] = time.time() + 600
P.POOL = _pool6
check("5) 仅频控挡住 -> False（不算配额）",
      P._realm_quota_parked("cn", MODEL)[0] is False)

P.POOL = None
check("6) 无池 -> False", P._realm_quota_parked("cn", MODEL)[0] is False)
_pool7, _accs7 = build(["intl"])
P.POOL = _pool7
check("6) 无本区域账号 -> False",
      P._realm_quota_parked("cn", MODEL)[0] is False)

# ---- B) QuotaExhausted ----
_pool8, _accs8 = build(["cn"])
_accs8[0].billing_until = time.time() + 3600
P.POOL = _pool8
_exc = None
_wrong = None
try:
    P.open_upstream({"model": MODEL,
                     "messages": [{"role": "user", "content": "hi"}]})
except P.QuotaExhausted as exc:
    _exc = exc
except Exception as exc:  # noqa: BLE001
    _wrong = exc
check("9) open_upstream 全停放空手 -> QuotaExhausted",
      _exc is not None, repr(_wrong)[:160])
if _exc is not None:
    check("7) message 保持 no usable account 前缀",
          str(_exc).startswith("no usable account"))
    check("9) wait>0", _exc.wait > 0, _exc.wait)
check("8) _exc_http_status 认 402",
      P._exc_http_status(P.QuotaExhausted("cn", 60)) == 402)

# ---- C) 出口接线（静态钉住）----
_src = open(os.path.join(ROOT, "qoder_proxy.py"), encoding="utf-8").read()
check("10) 3 个出口 ×2 + _exc_http_status 共 7 处 isinstance 分流",
      _src.count("isinstance(exc, QuotaExhausted)") == 7,
      _src.count("isinstance(exc, QuotaExhausted)"))
check("10) 池耗尽抛出点接 _realm_quota_parked（调用点 1 处）",
      _src.count("_realm_quota_parked(realm, model)") == 1,
      _src.count("_realm_quota_parked(realm, model)"))
check("11) 402 分支带 model_no_quota 类型",
      _src.count('"model_no_quota"') >= 3, _src.count('"model_no_quota"'))

print()
print("SUMMARY: TOTAL %d checks, %d passed, %d failed"
      % (PASS + FAIL, PASS, FAIL))
print("RESULT: %s (exit %d)" % ("GREEN" if FAIL == 0 else "RED",
                                1 if FAIL else 0))
sys.exit(1 if FAIL else 0)

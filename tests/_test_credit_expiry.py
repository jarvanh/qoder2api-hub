"""额度到期数据源套件（S1 回归守卫）。

    python tests/_test_credit_expiry.py

背景（task-69 审查 S1）：F 层临期分派依赖 soonest_expiring_days，而它只读
packages 内的 days_left / expires_at / expireAt；实测上游 userQuota /
addOnQuota 内部**没有任何**到期字段——旧实现下真实产物永远算不出非 None，
功能静默不可用。本套件钉三件事：
  E1 真实形状（无包级到期）→ 产物可算且为 None、窗口不触发（现状如实，安全侧）；
  E2 上游补包级字段那天 → fetch_credits 透传 → 立即算得出（透传管道守卫）；
  E3 顶层 expiresAt（归属未证实）不被误贴到包（防反向消耗策略）。
不联网：桩掉 http_json。
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("ACCOUNTS_DIR", os.path.join(ROOT, "_acc"))
import qoder_accounts as A

PASS = FAIL = 0


def check(label, cond, extra=None):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] %s  %r" % (label, extra))


def _quota_shape(addon_extra=None, top_expires=None):
    """探针抓到的真实响应形状（脱敏：数值为无关样例）。

    顶层 expiresAt 用「永不过期」占位（253402214400，实测 CN 号会返回该值），
    顺带验证它不会被误映射到包级。
    """
    aq = {"total": 800.0, "used": 0.0, "remaining": 800.0, "percentage": 0.0,
          "unit": "credits", "detailUrl": "https://example.invalid/detail"}
    if addon_extra:
        aq.update(addon_extra)
    return {
        "userId": "u", "userType": "personal_standard", "usageType": "credits",
        "totalUsagePercentage": 0.14, "isQuotaExceeded": False,
        "expiresAt": 253402214400 if top_expires is None else top_expires,
        "userQuota": {"total": 300.0, "used": 147.0, "remaining": 153.0,
                      "percentage": 0.5, "unit": "credits"},
        "addOnQuota": aq,
    }


def _fetch(shape):
    a = A.Account({"uid": "exp1", "realm": "cn", "accessToken": "dt-x"})
    a.enabled = True
    a.path = None
    orig = A.http_json
    A.http_json = lambda *aa, **kk: shape
    try:
        a.fetch_credits()
    finally:
        A.http_json = orig
    return a


# ---- 指纹行（与前端 _test_dashboard_*.js 同格式；实现见 tests/_suite_head.py）----
import os as _os_fp
import sys as _sys_fp
_sys_fp.path.insert(0, _os_fp.path.dirname(_os_fp.path.abspath(__file__)))
from _suite_head import declare_suite
declare_suite(__file__, ["qoder_accounts.py"])
print("[S1] credit expiry data-source guards")

a1 = _fetch(_quota_shape())
check("E1 真实形状（无包级到期）→ soonest 为 None（如实：当前算不出）",
      a1.soonest_expiring_days() is None,
      a1.credits.get("packages"))
check("E1b 真实形状 → 窗口判定恒 False（安全侧不误触发）",
      a1.in_expiring_window() is False)
check("E1c packages 不引入臆造字段（仅 name/remain/used/size）",
      all(set(p) == {"name", "remain", "used", "size"} for p in a1.credits["packages"]),
      a1.credits["packages"])

a2 = _fetch(_quota_shape(addon_extra={"expiresAt": time.time() + 3 * 86400}))
soon2 = a2.soonest_expiring_days()
check("E2 上游补包级 expiresAt → fetch_credits 透传 → soonest ≈ 3 天（透传管道）",
      soon2 is not None and 2.5 < soon2 < 3.5, (soon2, a2.credits.get("packages")))
a2.expiring_window_days = 7
check("E2b 端到端：真实产物 + 窗口 7 天 → in_expiring_window True（真判据）",
      a2.in_expiring_window() is True)

check("E3 顶层 expiresAt（永不过期占位）不被映射到包级（防反向消耗）",
      all("expires_at" not in p for p in a1.credits["packages"])
      and all("expires_at" not in p for p in a2.credits["packages"]
              if p["name"] == "基础额度"))

# ---- E4：顶层 expiresAt 的**语义**必须显式标注（v1.7.14 起）---------------
# 背景：旧实现只写 credits["expires_at"] 一个裸时刻，下游必然读成「积分作废」。
# 实测 131 号该值与该号面板原文「将于 2026年10月30日 23:59:38 刷新配额」
# 逐秒一致 —— 是**配额刷新**时刻，不是到期作废；而 credits["remain"] 混合了
# 月额剩余与 11/16–11/29 才到期的资源包。下游据此报出「2 小时后损失 1,737」。
# ⚠️ E4 的顶层 expiresAt 必须显式给非占位值：真实 131 号形状是顶层 2 小时后
# 刷新 + 包级无任何到期字段。此前只改 addOnQuota 不够（顶层仍是 NEVER 占位），
# 且旧 normalize_epoch 的毫秒启发式会把占位抹成 1978 年，让本断言**假通过**。
a3 = _fetch(_quota_shape(top_expires=time.time() + 2 * 3600))
check("E4 非占位顶层 expiresAt → expires_kind='refresh'（刷新，不是作废）",
      a3.credits.get("expires_kind") == "refresh", a3.credits.get("expires_kind"))
check("E4b refresh_at 与 expires_at 同源同值（下游按语义读，不必猜）",
      a3.credits.get("refresh_at") == a3.credits.get("expires_at")
      and a3.credits.get("refresh_at", 0) > 0, a3.credits.get("refresh_at"))
check("E4c 永不过期占位 → expires_kind='never'（保留 NEVER 判据语义）",
      a1.credits.get("expires_kind") == "never", a1.credits.get("expires_kind"))
check("E4d 无 packages 包级到期 → package_expiry_known=False（明确告知不可得）",
      a1.credits.get("package_expiry_known") is False,
      a1.credits.get("package_expiry_known"))
a4 = _fetch(_quota_shape(addon_extra={"expiresAt": time.time() + 3 * 86400}))
check("E4e 上游补上包级到期 → package_expiry_known=True（下游可放心算损失）",
      a4.credits.get("package_expiry_known") is True,
      a4.credits.get("package_expiry_known"))
check("E4f 既有 expires_at 字段仍在（qoder_tasks.ledger_is_unknown 依赖）",
      isinstance(a1.credits.get("expires_at"), int),
      a1.credits.get("expires_at"))

# ---- E5：NEVER 占位不得被毫秒启发式抹掉（v1.7.14 顺带修）-------------------
# normalize_epoch 对 >1e11 的值一律当毫秒除以 1000，而「永不过期」占位
# 253402214400 是**秒级** 9999-12-31 → 被抹成 253402214（1978 年），
# 使 ledger_is_unknown 的 `>= LEDGER_NEVER_EPOCH` 判据永不成立。
import qoder_tasks as _T
_deg = _fetch(_quota_shape())          # 顶层是 NEVER 占位
check("E5 NEVER 占位原样保留（不被 /1000 抹成 1978 年）",
      _deg.credits.get("expires_at") == 253402214400,
      _deg.credits.get("expires_at"))
_deg_cred = dict(_deg.credits, remain=0, remain_effective=0, size=0,
                 exceeded=True)
check("E5b 退化 body → ledger_is_unknown 判为 True（未知账本，不当 0 用）",
      _T.ledger_is_unknown(_deg_cred) is True, _deg_cred.get("expires_at"))

# ---- E6：正常毫秒时间戳不得被占位判据误伤（v1.7.15，v1.7.14 的回归）------
# v1.7.14 用 `raw >= _EXPIRY_NEVER_EPOCH` 判占位，但真实毫秒（≈1.79e12）
# 数值上**大于**秒级占位（2.53e11）→ expires_at 原样留毫秒、expires_kind
# 误报 never，下游「配额刷新」提示整体静默消失。实测 131 号部署后立刻复现。
import time as _time
_ms_shape = _quota_shape(top_expires=(_time.time() + 2 * 3600) * 1000)
_ms = _fetch(_ms_shape)
check("E6 正常毫秒 expiresAt → 归一到秒（不留 1.79e12）",
      _ms.credits.get("expires_at") == int((_time.time() + 2 * 3600))
      or abs(_ms.credits.get("expires_at", 0) - _time.time()) < 3 * 3600,
      _ms.credits.get("expires_at"))
check("E6b 正常毫秒 expiresAt → expires_kind='refresh'（不得误报 never）",
      _ms.credits.get("expires_kind") == "refresh",
      (_ms.credits.get("expires_kind"), _ms.credits.get("expires_at")))
check("E6c 毫秒形态占位也必须保住（253402214400000 → 秒级占位）",
      A.normalize_expiry_never_aware(253402214400000) == 253402214400,
      A.normalize_expiry_never_aware(253402214400000))

print("")
print("SUMMARY: TOTAL %d checks, %d passed, %d failed" % (PASS + FAIL, PASS, FAIL))
print("RESULT: %s (exit %d)" % ("RED" if FAIL else "GREEN", 1 if FAIL else 0))
sys.exit(1 if FAIL else 0)

"""#130 CN 签到三处缺陷：活动列表抖动 / 同人去重语义 / 调度器重放。"""
import io
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *          # noqa: F401,F403

P, A = bootstrap()
declare_suite(__file__, ["qoder_accounts.py", "qoder_tasks.py", "qoder_scheduler.py"])
import qoder_tasks as T            # noqa: E402
import qoder_scheduler as S        # noqa: E402

print("[#130-1] 活动列表抖动：空结果要重试")
ok_empty = {"ok": True, "campaigns": [], "show_campaign": True, "claimable": False}
check("空列表 -> 重试", A._campaign_list_needs_retry(ok_empty) is True)
check("showCampaign=false 且 claimable=false -> 重试",
      A._campaign_list_needs_retry({"ok": True, "campaigns": [{"claim_status": "CLAIMED"}],
                                    "show_campaign": False, "claimable": False}) is True)
_claimable = {"ok": True, "campaigns": [{"claim_status": "CLAIMABLE",
                                         "benefit": {"kind": "CREDITS", "amount": 100}}],
              "show_campaign": True, "claimable": True}
check("有可领项 -> 不重试", A._campaign_list_needs_retry(_claimable) is False)
check("查询失败不在这里重试（交给上层错误路径）",
      A._campaign_list_needs_retry({"ok": False, "error": "boom"}) is False)
check("只领积分时，券类 CLAIMABLE 不算命中（仍重试）",
      A._campaign_list_needs_retry(
          {"ok": True, "campaigns": [{"claim_status": "CLAIMABLE",
                                      "benefit": {"kind": "REDEMPTION_CODE"}}],
           "show_campaign": True, "claimable": True},
          only_kinds=("", "CREDITS")) is True)

print("[#130-2] 同人去重：当成「本轮无需再领」并盖签到章")
_acc = A.Account({"uid": "u130", "realm": "cn", "accessToken": "t"})
_stamped = {"n": 0}
class _FakeAcc:
    uid = "u130"; nickname = "测试号"; realm = "cn"
    credits = {"remain": 1, "size": 2}
    campaign_codes = {}
    campaign_status = {}
    def campaign_checkin(self, gap=None, only_kinds=None):
        return {"ok": True, "claimed": [], "already": [], "earned": 0,
                "blocked": [{"failure_code": "SAME_PERSON_ALREADY_CLAIMED",
                             "campaign": "daily"}],
                "pending": [], "locked": [], "codes": [],
                "message": "no claimable official campaign"}
    def _stamp_checkin(self):
        _stamped["n"] += 1
    def fetch_credits(self):
        return {"ok": False}
    def fetch_plan(self):
        return {"ok": False}
    def checkin(self):
        raise AssertionError("blocked 之后不该再走 sash 兜底")

res = T.run_checkin(_FakeAcc(), gap=0)
check("run_checkin 认为成功（不是失败）", res.get("ok") is True, res.get("ok"))
check("盖了本地签到章（避免每轮重复打上游）", _stamped["n"] == 1, _stamped["n"])
check("返回里带原始 failureCode",
      res.get("blocked") == ["SAME_PERSON_ALREADY_CLAIMED"], res.get("blocked"))
_logs = " ".join(res.get("logs") or [])
check("文案说明「按人去重」而不是「其他账号已领」", "按「人」去重" in _logs, _logs[:200])
check("没有把单账号说成「其它账号已领」", "其他账号本轮已领" not in _logs)

print("[#130-3] 调度器 fired 持久化：重启不重放")
_tmpd = tempfile.mkdtemp(prefix="sched130-")
_pool = A.AccountPool(_tmpd, log=lambda *a, **k: None)
_s1 = S.Scheduler(_pool, state_dir=_tmpd)
_now = time.time()
_s1._fired[float(int(_now))] = True
_s1._save_state()
_stpath = _s1._state_path()
_st = json.load(open(_stpath, encoding="utf-8"))
check("状态文件里有 fired", isinstance(_st.get("fired"), list) and _st["fired"], _st.get("fired"))
_s2 = S.Scheduler(_pool, state_dir=_tmpd)
check("新实例恢复了已触发点",
      any(abs(ts - int(_now)) < 2 for ts in _s2._fired), list(_s2._fired)[:3])

print("[#130-3] 脏数据不得锁死排程")
_s3 = S.Scheduler(_pool, state_dir=_tmpd)
_s3._fired = {}
_s3._fired_import([ "abc", None, 10 ** 12, time.time() + 9999,
                    time.time() - 10 * 86400, time.time() - 60])
check("只保留「过去 3 天内」的合理值",
      len(_s3._fired) == 1, list(_s3._fired))

print("[#130-3] 导出只留 3 天")
_s4 = S.Scheduler(_pool, state_dir=_tmpd)
_s4._fired = {time.time() - 60: True, time.time() - 5 * 86400: True}
check("更老的触发点不再导出", len(_s4._fired_export()) == 1, _s4._fired_export())

finish()

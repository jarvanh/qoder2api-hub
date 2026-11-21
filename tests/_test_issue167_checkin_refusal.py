"""#167: upstream denials must not turn into a successful local check-in."""
import io
import json
import os
import sys
import tempfile
import urllib.error
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *
P, A = bootstrap()
import qoder_tasks as T
import qoder_scheduler as S

declare_suite(__file__, ["qoder_accounts.py", "qoder_tasks.py", "qoder_scheduler.py"])


def activity(status="CLAIMABLE", reason=""):
    return {"campaign_id": "fixture", "campaign_key": "daily", "action_type": "CLAIM_BENEFIT",
            "claim_status": status, "unavailable_reason": reason,
            "required_achievement_key": "", "benefit": {"kind": "CREDITS", "amount": 100}}


def response_failure(status, **data):
    return urllib.error.HTTPError("https://fixture.invalid/claim", status, "fixture", {},
                                  io.BytesIO(json.dumps(data).encode()))


cases = [
    ("generic blocked", {"status": "BLOCKED"}, activity(), "上游未确认领取", False),
    ("risk claim", {"status": "BLOCKED", "failureCode": "RISK_BLOCKED"}, activity(), "RISK_BLOCKED", False),
    ("risk service failed", {"status": "DENIED", "failureCode": "RISK_DEPENDENCY_UNAVAILABLE"},
     activity(), "RISK_DEPENDENCY_UNAVAILABLE", False),
    ("inactive campaign", {"status": "NOT_ELIGIBLE", "failureCode": "CAMPAIGN_NOT_ACTIVE"},
     activity(), "CAMPAIGN_NOT_ACTIVE", False),
    ("ambiguous HTTP conflict", response_failure(409, message="not confirmed"), activity(), "HTTP 409", False),
    ("explicit HTTP policy denial", response_failure(403, errorCode="RISK_BLOCKED"), activity(), "RISK_BLOCKED", False),
    ("explicit HTTP replay", response_failure(409, errorCode="ALREADY_CLAIMED"), activity(), "已领取", True),
    ("explicit same person", {"status": "BLOCKED", "failureCode": "SAME_PERSON_ALREADY_CLAIMED"},
     activity(), "同人已领取", True),
    ("not claimable due to policy", {}, activity("NOT_ELIGIBLE", "RISK_BLOCKED"), "RISK_BLOCKED", False),
    ("campaign stock empty", {"status": "NOT_ELIGIBLE", "failureCode": "REDEMPTION_CODE_OUT_OF_STOCK"},
     activity(), "名额已发完", True),
    ("achievement really missing", {"status": "NOT_ELIGIBLE", "failureCode": "ACHIEVEMENT_NOT_COMPLETED"},
     activity(), "新人任务", True),
]
for name, reply, row, expected, ok in cases:
    account = A.Account({"uid": "fixture", "realm": "cn", "accessToken": "FAKE"})
    with patch.object(A, "http_json", side_effect=reply if isinstance(reply, Exception) else None,
                      return_value=reply) as transport, \
         patch.object(A, "native_machine_identity"), patch.object(A.time, "sleep"), \
         patch.object(account, "desktop_headers", return_value={}), \
         patch.object(account, "campaigns", return_value={"ok": True, "campaigns": [row]}), \
         patch.object(account, "_stamp_checkin", wraps=account._stamp_checkin) as stamp, \
         patch.object(account, "fetch_credits", return_value={"ok": False}), \
         patch.object(account, "fetch_plan", return_value={}), \
         patch.object(account, "checkin", side_effect=AssertionError("unexpected legacy claim")) as old:
        result = T.run_checkin(account, gap=0, only_daily=True)
        check(name + ": classification", result["ok"] is ok and expected in result["message"], result)
        check(name + ": no new credits or legacy claim", result["earned_credit"] == 0 and not old.called)
        check(name + ": no false local stamp", stamp.called is (name in ("explicit HTTP replay", "explicit same person")))
        check(name + ": at most one claim", transport.call_count == int(row["claim_status"] == "CLAIMABLE"))
        if not ok:
            check(name + ": no misleading achievement message", "新人任务" not in result["message"])

with tempfile.TemporaryDirectory() as tmp:
    pool = A.AccountPool(tmp, log=lambda *a, **kw: None)
    pool.accounts = [A.Account({"uid": "ineligible", "enabled": False})]
    sched = S.Scheduler(pool, state_dir=tmp)
    sched._pending_focus = "签到期 09:00"
    sched._run_cycle("fixture", S.CYCLE_HOUR)
    check("scheduler reports no eligible accounts, not all already checked in",
          any("没有符合本轮签到条件的账号" in line for line in sched.logs)
          and not any("所有账号今日已签到" in line for line in sched.logs))
finish()

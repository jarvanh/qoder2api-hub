"""Release regressions: check-in outcomes, catalog model display, reasoning intent."""
import io
import json
import os
import sys
import tempfile
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import bootstrap, check, declare_suite, finish
P, A = bootstrap()
import qoder_tasks as T
import qoder_activity as activity

declare_suite(__file__, ["qoder_proxy.py", "qoder_catalog.py", "qoder_accounts.py", "qoder_tasks.py", "qoder_activity.py"])

for payload, effort, intent in [
    ({}, None, "absent"),
    ({"reasoning": {"summary": "auto"}}, None, "absent"),
    ({"reasoning_effort": "HIGH"}, "high", "level"),
    ({"thinking": {"level": "low"}}, "low", "level"),
    ({"thinking": {"type": "disabled", "effort": "high"}}, "none", "disabled"),
    ({"thinking": False}, "none", "disabled"),
    ({"enable_thinking": False}, "none", "disabled"),
    ({"reasoning": {"enabled": False}}, "none", "disabled"),
    ({"reasoning_effort": "off"}, "none", "disabled"),
    ({"thinking": True}, None, "enabled_default"),
    ({"reasoning": {"enabled": True}}, None, "enabled_default"),
    ({"thinking": {"type": "enabled", "budget_tokens": 2048}}, None, "budget_unsupported"),
    ({"thinking_budget": 2048}, None, "budget_unsupported"),
    ({"reasoning": {"max_tokens": 2048}}, None, "budget_unsupported"),
    ({"extra_body": {"reasoning_effort": "medium"}}, "medium", "level"),
    ({"reasoning_effort": "high", "extra_body": {"enable_thinking": False}}, "high", "level"),
    ({"thinking": {"level": {"unexpected": True}}}, None, "unrecognized"),
    ({"thinking_budget": True}, None, "unrecognized"),
]:
    check("reasoning request: " + str(payload), P.reasoning_request(payload) == (effort, intent))

acc = A.Account({"uid": "fixture", "realm": "cn", "accessToken": "fake"})
with mock.patch.object(P, "catalog_meta_for", return_value={"display_name": "Fixture"}):
    for shape, sent in (({"enable_thinking": False}, "none"), ({"thinking_budget": 7777}, None)):
        chat = P.responses_to_chat(dict(model="Qwen4-Max", input="hi", **shape))
        body = P.build_qoder_body(chat, acc, "qmodel_latest", realm="cn")
        check("reasoning request survives Responses conversion without guessing",
              body["parameters"].get("reasoning_effort") == sent)
usage = {"prompt_tokens": 4, "completion_tokens": 6, "total_tokens": 10,
         "completion_tokens_details": {"reasoning_tokens": 2}}
with mock.patch.object(P, "_persist_usage"), mock.patch.object(P, "log") as log:
    row = P.record_usage("Qwen4-Max", usage, session_trace={"reasoning_intent": "budget_unsupported"})
    check("budget intent is audited with no raw budget or fake named level",
          row.get("reasoning_intent") == "budget_unsupported" and "reasoning_effort" not in row)
    check("usage and runtime log agree about actual reasoning tokens",
          row["reasoning_tokens"] == 2 and any("reasoning_tokens=2" in a.args[0] for a in log.call_args_list))

check("Qwen4-Max maps to the pinned official catalog key",
      P.resolve_request_model("Qwen4-Max", "cn") == "qmodel_latest")
for model, key, wire, display in [
    ("Qwen4-Max", "qmodel_latest", "auto", "Qwen4-Max"),
    ("Qwen4-Max", "qmodel_latest", "actual-model", "actual-model"),
    ("auto", "auto", "actual-model", "actual-model"),
    ("CUSTOM_MODEL", "auto", "auto", "auto"),
    ("CUSTOM_MODEL", "CUSTOM_MODEL", "auto", "auto"),
]:
    chunk = {"model": wire, "choices": [{"index": 0, "delta": {"reasoning_content": "thought"}, "finish_reason": "stop"}]}
    data = ("data: " + json.dumps({"statusCodeValue": 200, "body": json.dumps(chunk)}) + "\n\n").encode()
    ctx = {"requested_model": model, "upstream_model_key": key, "resolved_model": None}
    stream = io.BytesIO(data)
    stream._qd_model_audit = ctx
    actual = json.loads(next(P.iter_inner_sse(stream, chat_display=True)).decode()[6:])
    check("stream keeps model presentation and audit separate: " + model + "/" + key + "/" + wire,
          actual["model"] == display and ctx["resolved_model"] == wire and actual["choices"] == chunk["choices"])
    obj = P.aggregate_stream(io.BytesIO(data), model)
    obj["model"] = P._chat_display_model(obj["model"], ctx)
    check("nonstream preserves actual identity: " + model + "/" + key + "/" + wire,
          obj["model"] == display and obj["resolved_model"] == wire
          and list(P._chat_chunks(obj, model, chat_display=True))[0]["model"] == display
          and list(P._chat_chunks(obj, model))[0]["model"] == wire)

with tempfile.TemporaryDirectory() as temp:
    fixtures = [SimpleNamespace(uid=zone + "-fixture", nickname=zone, realm=zone,
                               checkin_eligible=lambda: True) for zone in ("cn", "intl")]
    def outcome(account, **kw):
        assert kw["only_daily"]
        return {"ok": True, "earned_credit": 0, "state": "no_credit",
                "message": "gateway list contains no claimable credits"} if account.realm == "cn" else {
                    "ok": True, "earned_credit": 100, "state": "claimed", "message": "grant confirmed"}
    handler = object.__new__(P.Handler)
    handler._json = lambda code, data: (code, data)
    with mock.patch.multiple(P, USAGE_DIR=temp, POOL=SimpleNamespace(accounts=fixtures)), \
         mock.patch.object(P, "account_views", return_value=[]), \
         mock.patch.object(T, "run_checkin", side_effect=outcome):
        code, body = handler._handle_accounts("/accounts/checkin", {})
    check("HTTP 200 is independent from per-account grants",
          code == 200 and body["credit_added"] == 100
          and body["results"][0]["state"] == "no_credit"
          and body["results"][0]["earned_credit"] == 0 and body["results"][1]["state"] == "claimed")
    history = {row["realm"]: row for row in activity.recent(temp)}
    check("activity ledger preserves exact no-credit reason and outcome",
          history["cn"]["outcome"] == "no_credit" and history["cn"]["amount"] == 0
          and "no claimable credits" in history["cn"]["detail"] and history["intl"]["amount"] == 100)

for zone in ("cn", "intl"):
    account = A.Account({"uid": "fixture-" + zone, "realm": zone, "accessToken": "fake"})
    for receipt, expected, state in ((None, 0, "no_credit"), (0, 0, "no_credit"), (15, 15, "claimed")):
        rows = [] if receipt is None else [{"campaign_id": "fixture", "campaign_key": "daily", "action_type": "CLAIM_BENEFIT",
                    "claim_status": "CLAIMABLE", "unavailable_reason": "", "benefit": {"kind": "CREDITS", "amount": 100}}]
        with mock.patch.object(A, "native_machine_identity"), mock.patch.object(A.time, "sleep"), \
             mock.patch.object(account, "campaigns", return_value={"ok": True, "campaigns": rows}), \
             mock.patch.object(account, "desktop_headers", return_value={}), \
             mock.patch.object(A, "http_json", return_value={"status": "GRANTED", "benefit": {"amount": receipt}}) as grant, \
             mock.patch.object(account, "_stamp_checkin") as stamp, \
             mock.patch.object(account, "checkin", side_effect=AssertionError("no legacy claim")), \
             mock.patch.object(account, "fetch_credits", return_value={}), \
             mock.patch.object(account, "fetch_plan", return_value={}):
            result = T.run_checkin(account, gap=0, only_daily=True)
        check("daily claim follows upstream eligibility and receipt: %s %s" % (zone, receipt),
              result["earned_credit"] == expected and result["state"] == state
              and grant.call_count == stamp.call_count == int(receipt is not None))
finish()

"""Model lookup refusals stop inference retries without changing the prompt."""
import io
import json
import os
import sys
import tempfile
import types
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import bootstrap, check, declare_suite, finish
P, A = bootstrap()
declare_suite(__file__, ["qoder_proxy.py"])

reject = {"type": "provider_error", "message": "Cannot find model", "extra": "private-fixture"}
detail = json.dumps(reject)
for code in (400, 404):
    for body in (detail, json.dumps({"error": reject})):
        check("request fault with exact model error", P._is_request_fault(code, body))
        check("not transient", not P._is_transient_upstream(code, body))
        msg, kind = P.friendly_upstream_error(code, body)
        check("actionable message without raw provider data", kind == "upstream_model_rejected"
              and "/v1/models" in msg and "private-fixture" not in msg)
for code, body in ((503, detail), (400, '{"code":"provider_error","message":"upstream timeout"}'),
                   (400, '{"message":"Cannot find model"}')):
    check("different errors do not change classification", not P._is_upstream_model_rejection(code, body))
check("generic transient provider failures still retry", P._is_transient_upstream(418, '{"code":"provider_error"}'))

payload = {"model": "Qwen3.8-Flash", "messages": [{"role": "system", "content": "Retain private-fixture instructions."},
                                                  {"role": "user", "content": "Hello"}]}
with tempfile.TemporaryDirectory() as directory:
    pool = A.AccountPool(directory, log=lambda *a, **k: None)
    for i in range(2):
        acc = A.Account({"uid": "model-%s" % i, "realm": "cn", "accessToken": "fake"})
        acc.enabled, acc.expires_at = True, 0
        pool.accounts.append(acc)
    def fail_provider(request, **kwargs):
        raise urllib.error.HTTPError(request.full_url, 400, "refused", {}, io.BytesIO(detail.encode()))
    session = types.SimpleNamespace(headers=lambda *a, **k: {})
    with mock.patch.multiple(P, POOL=pool, ACCOUNTS_DIR=directory,
                            SESSIONS=types.SimpleNamespace(get=lambda a: session)), \
         mock.patch.object(P, "validate_public_http_url", side_effect=lambda x: x), \
         mock.patch.object(P.urllib.request, "urlopen", side_effect=fail_provider) as request, \
         mock.patch.object(P.time, "sleep") as sleep, mock.patch.object(P, "log") as log, \
         mock.patch.object(pool.affinity, "unbind") as unbind:
        try:
            P.open_upstream(payload, session_key="window", target_realm="cn")
        except urllib.error.HTTPError as exc:
            check("HTTP retains original status with safe detail", exc.code == 400
                  and exc.qoder_detail == P._MODEL_REJECTION_DETAIL)
        else:
            check("refusal is never a successful stream", False)
        check("single HTTP attempt without sleep or rotation", request.call_count == 1
              and not sleep.called and not unbind.called)
        check("no private content in shape diagnostic", "private-fixture" not in str(log.call_args_list))
    check("all accounts remain healthy", all(not a.last_error for a in pool.accounts))
    check("original prompt remains unchanged", payload["messages"][0]["content"] == "Retain private-fixture instructions.")

for value in ({"statusCodeValue": 400, "body": detail},
              {"statusCodeValue": "404", "body": {"error": reject}},
              {"statusCodeValue": 200, "body": json.dumps({"error": reject})}):
    wire = io.BytesIO(("data: " + json.dumps(value) + "\n\n").encode())
    try:
        list(P.iter_inner_sse(wire))
    except P.UpstreamStatus as exc:
        check("SSE is an explicit failure with safe detail", int(exc.status) in (400, 404)
              and exc.detail == P._MODEL_REJECTION_DETAIL)
        acc = mock.Mock()
        with mock.patch.object(P, "POOL", mock.Mock()) as pool:
            P._handle_envelope_account_cooldown(acc, exc, "test-model", "window")
            check("SSE neither cools the account nor clears affinity", not acc.note_error.called
                  and not pool.affinity.unbind.called)
        check("SSE does not reopen before output", not P.should_retry_envelope(exc, False, 0))
    else:
        check("SSE cannot masquerade as successful empty output", False)
finish()

# -*- coding: utf-8 -*-
"""#137 排队 / 额度错误不能被客户端当成「API Key 无效」。

    python tests/_test_issue137_error_class.py

现象：DSH 接本网关时，上游排队（10605）和额度耗尽（112）都显示成「API 密钥无效」。
根因：DSH 的 pi-ai 适配层按错误文字归类，第一条规则就是 /\\b(?:401|403)\\b/ -> AUTH；
而网关的中文提示里原样带着上游信封的 "code":"403"。预检阶段的错误还直接用 403 状态码，
OpenAI SDK 会把它拼成「403 …」，同样命中。

本套件把 DSH 的分类规则原样移植过来（JS 的 \\b 只认 ASCII，这里用 re.ASCII 对齐），
断言网关发出的文字 / 状态码会被分到正确的类别；并带反向对照：修复前的文字确实会被
判成 AUTH，真正的凭证错误仍然保持 AUTH。
"""
import http.client
import http.server
import json
import os
import re
import sys
import tempfile
import threading
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *          # noqa: F401,F403

_TMP = tempfile.mkdtemp(prefix="i137-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["QD_PROXY_USAGE_DIR"] = os.path.join(_TMP, "usage")
os.environ["QD_NATIVE_IDENTITY"] = "0"
P, A = bootstrap()
declare_suite(__file__, ["qoder_proxy.py"])

# ---------------------------------------------------------------- DSH 分类规则
# 摘自 DeepSeek Harness 0.2.x 打包的 dsh-llm-pi-ai：classifyPiAiError /
# isQuotaExceededError（只保留与本问题相关的前几条，顺序不变）。
_F = re.ASCII | re.IGNORECASE


def dsh_is_quota(msg):
    return bool(
        re.search(r"\binsufficient[\s_-]+(?:quota|balance|credits?)\b", msg, _F)
        or re.search(r"\b(?:quota|usage[\s_-]+limit)[\s_-]+(?:exceeded|exhausted|reached)\b", msg, _F)
        or re.search(r"\bexceed(?:ed|s)?[\s_-]+(?:(?:your|the)[\s_-]+)?(?:current[\s_-]+)?quota\b", msg, _F)
        or re.search(r"\b(?:balance|credits?)[\s_-]+(?:exhausted|depleted)\b", msg, _F)
        or re.search(r"\bout[\s_-]+of[\s_-]+(?:credits?|budget)\b", msg, _F))


def dsh_classify(msg):
    if re.search(r"\b(?:401|403)\b", msg, re.ASCII):
        return "AUTH"
    if dsh_is_quota(msg):
        return "QUOTA"
    if re.search(r"\b429\b|rate.?limit", msg, _F):
        return "RATE_LIMIT"
    if re.search(r"\b5\d\d\b", msg, re.ASCII):
        return "SERVER"
    return "PI_AI_ERROR"


def sdk_message(status, msg):
    """OpenAI Node SDK 对 HTTP 错误的 message：「<status> <error.message>」；流内错误只有文字。"""
    return ("%s %s" % (status, msg)) if status else msg


# 真实日志里的上游详情（2026-11-05，Qwen3.8-Flash 排队 / Qwen3.8-Max 额度耗尽）
QUEUED = ('{"code":"403","message":"{\\"code\\":\\"10605\\",\\"message\\":\\"{\\\\\\"isQueued'
          '\\\\\\":true,\\\\\\"modelKey\\\\\\":\\\\\\"qfmodel\\\\\\",\\\\\\"queueCount\\\\\\":14883,'
          '\\\\\\"queueType\\\\\\":\\\\\\"p3\\\\\\",\\\\\\"retryAfterSeconds\\\\\\":30,'
          '\\\\\\"subCode\\\\\\":\\\\\\"RESOURCE_LIMIT\\\\\\"}\\",\\"subCode\\":\\"RESOURCE_LIMIT\\"}"}')
QUOTA112 = '{"code":"112","message":"{\\"pricingUrl\\":\\"https://qoder.com.cn/pricing?client=qoder\\"}"}'
QUOTA112_ENV = '{"code":"403","message":"{\\"code\\":\\"112\\",\\"message\\":\\"{\\\\\\"pricingUrl\\\\\\":\\\\\\"x\\\\\\"}\\"}"}'
DAILY110 = '{"code":"403","message":"{\\"code\\":\\"110\\",\\"message\\":\\"Billing daily count exceeded\\"}"}'

print("[#137-0] 反向对照：修复前的文字确实会被 DSH 判成 AUTH")
_old = ("上游排队中（10605 / RESOURCE_LIMIT）：已按 retryAfterSeconds 等待并重试，"
        "仍拿不到可用资源。上游详情：%s" % QUEUED[:200])
check("旧排队文字 -> AUTH（复现报告现象）", dsh_classify(_old) == "AUTH", dsh_classify(_old))
check("旧预检额度错误「403 …」-> AUTH",
      dsh_classify(sdk_message(403, "上游账号额度耗尽（112 / pricingUrl）")) == "AUTH")

print("[#137-1] 排队错误 -> RATE_LIMIT")
for st in (403, 429, 503):
    msg, et = P.friendly_upstream_error(st, QUEUED)
    out = P._client_error_status(st, et)
    check("排队 status=%s：类型 upstream_queued" % st, et == "upstream_queued", et)
    check("排队 status=%s：流内文字 -> RATE_LIMIT" % st, dsh_classify(msg) == "RATE_LIMIT",
          (dsh_classify(msg), msg[-160:]))
    check("排队 status=%s：预检「状态码 + 文字」-> RATE_LIMIT（状态码 %s）" % (st, out),
          dsh_classify(sdk_message(out, msg)) == "RATE_LIMIT", out)
    check("排队 status=%s：中文原因与 10605 / 排队数仍在" % st,
          "排队" in msg and "10605" in msg and "14883" in msg, msg[:120])

print("[#137-2] 额度耗尽（112）/ 每日上限（110）-> QUOTA")
for name, det in (("112", QUOTA112), ("112 套信封", QUOTA112_ENV), ("110", DAILY110)):
    msg, et = P.friendly_upstream_error(403, det)
    out = P._client_error_status(403, et)
    check("%s：类型非兜底" % name, et in ("quota_exhausted", "quota_daily_limit"), et)
    check("%s：状态码 403 -> 429" % name, out == 429, out)
    check("%s：流内文字 -> QUOTA" % name, dsh_classify(msg) == "QUOTA",
          (dsh_classify(msg), msg[-160:]))
    check("%s：预检「状态码 + 文字」-> QUOTA" % name,
          dsh_classify(sdk_message(out, msg)) == "QUOTA", sdk_message(out, msg)[:80])

print("[#137-3] 不误伤：真正的凭证 / 其它错误保持原样")
check("非目标类型不改状态码：upstream_error 403",
      P._client_error_status(403, "upstream_error") == 403)
check("非目标类型不改状态码：invalid_request_error 401",
      P._client_error_status(401, "invalid_request_error") == 401)
check("非数字状态码原样返回", P._client_error_status("x", "upstream_queued") == "x")
_msg, _et = P.friendly_upstream_error(401, '{"code":"401","message":"token expired"}')
check("上游 401（凭证过期）仍会被判 AUTH", dsh_classify(sdk_message(401, _msg)) == "AUTH", _msg)
check("只改写独立的 401/403：版本号、端口、长数字不动",
      P._neutralize_auth_codes("v1.4030 port 14031 id 4013 code 403") ==
      "v1.4030 port 14031 id 4013 code HTTP_403")

print("[#137-4] 端到端：预检阶段的排队 / 额度错误，HTTP 状态码与文字")


class Gateway(P.Handler):
    def log_message(self, *_):
        pass


P.ACCOUNTS_DIR = os.environ["ACCOUNTS_DIR"]
P.POOL = A.AccountPool(P.ACCOUNTS_DIR, log=lambda *_a, **_k: None)
P.POOL.add(A.Account(dict(uid="fake-137", realm="cn", accessToken="FAKE")))
P.API_KEY = "k-137"
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
srv.daemon_threads = True
threading.Thread(target=srv.serve_forever, daemon=True).start()
PORT = srv.server_address[1]


def post(body):
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=15)
    conn.request("POST", "/v1/chat/completions", body=json.dumps(body),
                 headers={"Content-Type": "application/json",
                          "Authorization": "Bearer k-137"})
    r = conn.getresponse()
    raw = r.read().decode("utf-8", "replace")
    conn.close()
    return r.status, raw


for name, det, want in (("排队", QUEUED, "RATE_LIMIT"), ("额度 112", QUOTA112, "QUOTA")):
    def _raise(*_a, **_k):
        raise P.UpstreamStatus(403, det)
    for stream in (False, True):
        with mock.patch.object(P, "open_upstream", _raise):
            st, raw = post({"model": "Qwen3.8-Flash", "stream": stream,
                            "messages": [{"role": "user", "content": "hi"}]})
        try:
            m = json.loads(raw)["error"]["message"]
        except Exception:
            m = raw
        check("%s stream=%s：状态码不是 401/403" % (name, stream), st not in (401, 403), st)
        check("%s stream=%s：DSH 归类为 %s" % (name, stream, want),
              dsh_classify(sdk_message(st, m)) == want,
              (st, dsh_classify(sdk_message(st, m)), m[-120:]))

print("[#137-5] 流内错误帧与 Responses 失败帧也走同一映射（源码落点）")
_src = open(os.path.join(ROOT, "qoder_proxy.py"), encoding="utf-8").read()
check("chat 流内错误帧 code 经 _client_error_status",
      '"code": _client_error_status(_cst, etype)' in _src)
check("Responses 失败帧状态经 _client_error_status",
      "_client_error_status(_to_int_status(_pst), _etype)" in _src)
check("_error 统一映射", "code = _client_error_status(code, err_type)" in _src)
check("_rate_limited 附带的上游详情也改写", "_neutralize_auth_codes(exc.detail[:200])" in _src)

srv.shutdown()
srv.server_close()
finish()

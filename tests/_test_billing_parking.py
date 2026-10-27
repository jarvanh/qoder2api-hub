# -*- coding: utf-8 -*-
"""110 日计数超限停放 + 流式惰性建流套件（fork 根治 2026-10-27）。

    python tests/_test_billing_parking.py

钉住两组语义：
  A) 110 Billing daily count exceeded：
     1) 识别（含 JSON 转义 body），且 10605 排队/普通 403 不误伤；
     2) 停放到 UTC+8 日界，ready() 挡住；
     3) note_success / clear_error / token 刷新路径**都清不掉**（这是
        2026-10-27 现场手设 cooldownUntil 被 refresh() 清零、整池复活空烧
        的直接教训）；
     4) 信封冷却入口走停放，不再给 60s 短冷却；
     5) 重开上游预算不被 110 烧穿（should_retry_envelope False）。
  B) SSE 惰性建流：
     6) 首字节前不发响应头（调用方可改发真实 HTTP 状态）；
     7) 首字节后补发头一次，后续写入不重复发头；
     8) _exc_http_status 认 str 状态码（"403" 不再回落 429）。
"""
import datetime
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


import os as _os_fp
import sys as _sys_fp
_sys_fp.path.insert(0, _os_fp.path.dirname(_os_fp.path.abspath(__file__)))
from _suite_head import declare_suite
declare_suite(__file__, ["qoder_proxy.py", "qoder_accounts.py"])
print("[billing] 110 日计数停放 + 流式惰性建流（fork 根治）")

MODEL = "qwen3.8-flash"
# 2026-10-27 现场原始 body（外层信封里内层 JSON 被转义）
DETAIL_110 = ('{\\"code\\":\\"110\\",\\"message\\":\\"Billing daily '
              'count exceeded\\"}')
DETAIL_110_PLAIN = '{"code":"11Billing daily count exceeded"}'
DETAIL_QUEUE = ('{"code":"10605","isQueued":true,"retryAfterSeconds":30,'
                '"message":"queue is full"}')
DETAIL_OTHER403 = '{"code":"403","message":"Forbidden"}'


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


# ---- 1/2) 识别 ----
check("110 转义 body 被识别", P._is_billing_limit(403, DETAIL_110) is True)
check("110 明文 body 被识别", P._is_billing_limit(403, DETAIL_110_PLAIN) is True)
check("10605 排队不是 110（#71 语义不变）",
      P._is_billing_limit(403, DETAIL_QUEUE) is False)
check("普通 403 不是 110", P._is_billing_limit(403, DETAIL_OTHER403) is False)
check("非 403 一律不是 110",
      P._is_billing_limit(429, DETAIL_110_PLAIN) is False)

# ---- 3) 停放 + ready 门禁 + 清不掉 ----
_deadline = P.qoder_accounts.next_daily_reset()
_now8 = datetime.datetime.now(P.qoder_accounts._UTC8)
_check = (_deadline > time.time()
          and datetime.datetime.fromtimestamp(_deadline,
                                              P.qoder_accounts._UTC8).hour == 0
          and (_deadline - time.time()) <= 86400)
check("next_daily_reset = 下一个 UTC+8 00:00 且不超过 24h", _check,
      (_deadline, _now8.isoformat()))

_pool, _accs = build(["cn"])
P.POOL = _pool
a = _accs[0]
_before = a.cooldown_until
_dl = a.note_billing_limit("billing daily count exceeded (HTTP 403)")
check("note_billing_limit 写到日界", abs(_dl - _deadline) <= 2, (_dl, _deadline))
check("ready() 被停放挡住", a.ready(MODEL) is False)
check("last_error 记录 110", "billing daily" in (a.last_error or "").lower(),
    a.last_error)
# 现场教训的核心：这些路径一律清不掉 billing_until
a.note_success(model=MODEL)
check("note_success 清不掉停放", a.billing_until > time.time()
      and a.ready(MODEL) is False)
a.clear_error()
check("clear_error 清不掉停放", a.billing_until > time.time())
a.cooldown_until = 0            # token 刷新成功路径做的事（_post_token 尾部）
check("刷新式清 cooldown 也清不掉停放", a.billing_until > time.time()
      and a.ready(MODEL) is False)
check("停放不占用 cooldown_until（60s 短冷却语义未被动歪）",
      a.cooldown_until == _before, (a.cooldown_until, _before))
check("手工解除通道存在且生效", (a.clear_billing_limit(), a.ready(MODEL))[1] is True)

# ---- 4) 持久化（重启不复活）----
a.note_billing_limit("billing daily count exceeded (HTTP 403)")
_doc = a.to_dict()
check("to_dict 带 billingUntil", (_doc.get("billingUntil") or 0) > time.time(),
      _doc.get("billingUntil"))
_a2 = A.Account(_doc)
check("from_dict 恢复停放（重启不复活）", _a2.billing_until > time.time()
      and _a2.ready(MODEL) is False)
a.billing_until = 0

# ---- 5) 信封入口：停放而不是 60s ----
_pool5, _accs5 = build(["cn"])
P.POOL = _pool5
b = _accs5[0]
P._handle_envelope_account_cooldown(b, P.UpstreamStatus(403, DETAIL_110_PLAIN),
                                    model=MODEL, session_key=None)
check("信封 110 -> 停放", b.billing_until > time.time())
check("信封 110 -> 不再 60s 短冷却（cooldown_until 未启动）",
      b.cooldown_until <= time.time(), b.cooldown_until)
check("信封 110 -> 模型冷却也不写（停放是账号级）",
      b.model_cooldowns.get(MODEL, 0) <= time.time())
# 排队回归：10605 仍然不惩罚账号
_pool6, _accs6 = build(["cn"])
P.POOL = _pool6
c = _accs6[0]
P._handle_envelope_account_cooldown(c, P.UpstreamStatus(403, DETAIL_QUEUE),
                                    model=MODEL, session_key=None)
check("10605 排队仍不冷却也不停放（#71 不变）",
      c.billing_until <= time.time() and c.cooldown_until <= time.time())

# ---- 6) 重开预算不被 110 烧穿 ----
check("信封 110 不重开上游（should_retry_envelope False）",
      P.should_retry_envelope(P.UpstreamStatus(403, DETAIL_110_PLAIN),
                              False, 0) is False)
check("信封 418 瞬时错仍重开（回归保护）",
      P.should_retry_envelope(P.UpstreamStatus(
          418, '{"type":"provider_error"}'), False, 0) is True)
check("_exc_http_status 认 str 状态码（'403' 不回落 429）",
      P._exc_http_status(P.UpstreamStatus("403", "x"), 429) == 403)


# ---- 7/8) 惰性建流（不依赖真 socket：假 Handler 记录动作）----
class _Fake:
    def __init__(self):
        self.actions = []
        self._chunked = False

    def send_response(self, code):
        self.actions.append(("status", code))

    def send_header(self, k, v):
        self.actions.append(("hdr", k))

    def end_headers(self):
        self.actions.append(("end",))

    def wfile_write(self, b):
        self.actions.append(("body", len(b)))

    def wfile_flush(self):
        pass


# 用真实实现但把 socket 面换掉：只取 _sse_begin/_sse_write/_sse_end 的逻辑
import types
f = _Fake()
f.wfile = types.SimpleNamespace(write=f.wfile_write, flush=f.wfile_flush)
f.realm_used = ""
f.realm_reason = ""
f.path = "/v1/chat/completions"
_P_Handler = P.Handler
# 直接复用方法体（绑定假对象）；_sse_write 内部 self._sse_begin() 也指向真实现
begin = types.MethodType(_P_Handler._sse_begin, f)
write = types.MethodType(_P_Handler._sse_write, f)
end = types.MethodType(_P_Handler._sse_end, f)
f._sse_begin = begin
# 空写不建流
write(b"")
check("惰性建流：空写不发头", f.actions == [] and f._chunked is False, f.actions)
# 首字节建流一次
write(b"data: x\n\n")
_status = [a for a in f.actions if a[0] == "status"]
check("惰性建流：首字节发 200 头一次", _status == [("status", 200)], f.actions)
n_hdr = len([a for a in f.actions if a[0] == "hdr"])
# 后续写入不重复发头
write(b"data: y\n\n")
check("后续写入不重复发头（幂等）",
      len([a for a in f.actions if a[0] == "hdr"]) == n_hdr, f.actions)
check("建流后 _chunked=True，终态分支不再走真实状态码路径",
      f._chunked is True)
end()
end()
# chunked 写法：每帧 = 长度头 + 数据 + \r\n；终止块是单独一行 "0\r\n\r\n"（5 字节）
_term = len([a for a in f.actions if a == ("body", 5)])
check("结束流幂等（终止块只发一次）", _term == 1, f.actions)

print("SUMMARY: TOTAL %d checks, %d passed, %d skipped"
      % (PASS + FAIL, PASS, 0))
print("RESULT: %s (exit %d)" % ("GREEN" if FAIL == 0 else "RED",
                                1 if FAIL else 0))
sys.exit(1 if FAIL else 0)

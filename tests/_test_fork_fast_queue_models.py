"""fork：QD_QUEUE_FAST_MODELS 按模型排队策略（v1.7.11 HTTP 层 + v1.7.12 信封层）。

    python tests/_test_fork_fast_queue_models.py

覆盖：
- 默认（env 未设）：queue_mode() 全局档，任意模型不受影响（向后兼容）
- env 设名单后：命中公开名 / modelKey（大小写不敏感）→ fast；
  未命中模型 → 维持全局档；带通配的条目进 glob 匹配
- 全局 wait 档下命中名单仍强制 fast（名单语义优先于全局档）
- open_upstream 重试循环消费点：fast 模型排队时不进 on_queue 分支、
  直接 raise UpstreamQueued（源码静态断言）
- 三个流式 handler 消费点均带 model 上下文（queue_mode(model...)）
- v1.7.12 信封层：流内 10605 信封（多层转义 JSON）按公开名 / detail 里
  modelKey 命中名单 → 不再睡 retryAfterSeconds×N 轮，立即转 429；
  非名单模型照旧；未设名单行为与 1.7.10 一致；emitted 计费安全优先
- v1.7.16 回退协议头：fast 拒绝（名单命中）统一 503 +
  x-omni-fallback-hint: connection_cooldown —— OmniRoute combos 立即切
  下一腿且 skipProviderBreaker，而不是按 429 锁死模型；非名单模型
  维持 429 口径；UpstreamQueued 三个 handler 快速拒绝点与非流式
  aggregate 调用方的 except RateLimited 兼底
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *          # noqa: F401,F403

P, A = bootstrap()
declare_suite(__file__, ["qoder_proxy.py"])

_ORIG_MODE = P.QUEUE_MODE
_ORIG_FAST = P.QUEUE_FAST_MODELS
_ORIG_GLOBS = P.QUEUE_FAST_MODEL_GLOBS


def _restore():
    P.QUEUE_MODE = _ORIG_MODE
    P.QUEUE_FAST_MODELS = _ORIG_FAST
    P.QUEUE_FAST_MODEL_GLOBS = _ORIG_GLOBS


def _envelope_with(modelkey):
    """真实形态的多层转义信封 detail（线上 09:16 日志同款）。"""
    inner = ('{"isQueued":true,"modelKey":"%s","queueCount":0,'
             '"retryAfterSeconds":30,"subCode":"RESOURCE_LIMIT"}' % modelkey)
    mid = json.dumps({"code": "10605", "message": inner,
                      "subCode": "RESOURCE_LIMIT"})
    outer = json.dumps({"code": "403", "message": mid,
                        "subCode": "RESOURCE_LIMIT"})
    return P.UpstreamStatus(403, outer)


try:
    # ------------------------------------------------------------- 默认兼容
    print("[fork] 默认（未设 QD_QUEUE_FAST_MODELS）：行为不变")
    P.QUEUE_FAST_MODELS = frozenset()
    P.QUEUE_FAST_MODEL_GLOBS = ()
    P.QUEUE_MODE = "visible"
    check("未设名单时 queue_mode() = 全局档",
          P.queue_mode() == "visible", P.queue_mode())
    check("未设名单时任意模型 = 全局档",
          P.queue_mode("DeepSeek-Flash", "dfmodel") == "visible",
          P.queue_mode("DeepSeek-Flash", "dfmodel"))
    P.QUEUE_MODE = "wait"
    check("全局 wait 不受影响",
          P.queue_mode("DeepSeek-Flash", "dfmodel") == "wait",
          P.queue_mode("DeepSeek-Flash", "dfmodel"))

    # ------------------------------------------------------------- 名单命中
    print("[fork] 名单命中：公开名 / modelKey / 大小写 / 通配")
    P.QUEUE_MODE = "visible"
    P.QUEUE_FAST_MODELS = frozenset({"deepseek-flash", "dfmodel"})
    P.QUEUE_FAST_MODEL_GLOBS = ("kimi-*",)
    check("公开名命中（大小写不敏感）",
          P.queue_mode("DeepSeek-Flash", "dfmodel") == "fast",
          P.queue_mode("DeepSeek-Flash", "dfmodel"))
    check("仅 modelKey 命中",
          P.queue_mode("SomeOtherName", "DFMODEL") == "fast",
          P.queue_mode("SomeOtherName", "DFMODEL"))
    check("未命中模型维持全局档",
          P.queue_mode("GLM-5.3-Flash", "glm-flash") == "visible",
          P.queue_mode("GLM-5.3-Flash", "glm-flash"))
    check("通配命中", P.queue_mode("Kimi-K3.1-Pro", "kimi-k3-1") == "fast",
          P.queue_mode("Kimi-K3.1-Pro", "kimi-k3-1"))
    check("通配不误伤", P.queue_mode("GLM-5.3", "glm5") == "visible",
          P.queue_mode("GLM-5.3", "glm5"))
    check("空入参安全", P.queue_mode("", "") == "visible", P.queue_mode("", ""))

    # --------------------------------------------------- 名单语义 > 全局档
    print("[fork] 名单语义优先于全局档")
    P.QUEUE_MODE = "wait"
    check("全局 wait 下命中名单仍 fast",
          P.queue_mode("DeepSeek-Flash", "dfmodel") == "fast",
          P.queue_mode("DeepSeek-Flash", "dfmodel"))
    P.QUEUE_MODE = "fast"
    check("全局 fast 下未命中模型仍是 fast（全局档生效）",
          P.queue_mode("GLM-5.3-Flash", "glm-flash") == "fast",
          P.queue_mode("GLM-5.3-Flash", "glm-flash"))

    # ------------------------------------------------------- 源码静态断言
    print("[fork] 源码断言：消费点带模型上下文")
    _src = open(os.path.join(ROOT, "qoder_proxy.py"), encoding="utf-8").read()
    check("QD_QUEUE_FAST_MODELS 已接入",
          "QD_QUEUE_FAST_MODELS" in _src)
    check("open_upstream 消费点带 (model, model_key)",
          'queue_mode(model, model_key) == "visible"' in _src
          and 'queue_mode(model, model_key) != "wait"' in _src)
    _n_bare = _src.count('queue_mode() != "wait"') + \
        _src.count('queue_mode() != "visible"')
    check("无遗漏的无参消费点（除 /health 展示）", _n_bare == 0, _n_bare)
    check("/health 展示保持无参 queue_mode()",
          '"queue_mode": queue_mode()' in _src)
    check("fast 拒绝时打 WARN 日志（可观测）",
          "fast reject per QD_QUEUE_FAST_MODELS" in _src)

    # ------------------------------------------------- fast 拒绝语义不变
    print("[fork] fast 拒绝语义不变（UpstreamQueued → 503 + Retry-After）")
    _exc = P.UpstreamQueued(30, '{"code":"10605","isQueued":true}', waited=0)
    check("UpstreamQueued.status = 503", _exc.status == 503)
    check("UpstreamQueued.retry_after = 上游秒数", _exc.retry_after == 30)

    # ------------------------------------------------- v1.7.12 信封层
    print("[fork12] 信封层：流内 10605 信封也按名单 fast")
    P.QUEUE_MODE = "visible"
    P.QUEUE_FAST_MODELS = frozenset({"dfmodel", "deepseek-flash"})
    P.QUEUE_FAST_MODEL_GLOBS = ()
    _e_df = _envelope_with("dfmodel")
    check("信封排队秒数解析不变（retryAfterSeconds=30）",
          P._envelope_queue_wait(_e_df) == 30,
          P._envelope_queue_wait(_e_df))
    check("信封层公开名命中 → 不重试（fast）",
          P.should_retry_envelope(_e_df, False, 0, model="DeepSeek-Flash") is False)
    check("信封层仅 modelKey 命中（转义 detail 提取）→ fast",
          P.should_retry_envelope(_e_df, False, 0, model="WhateverName") is False)
    _e_km = _envelope_with("kmodel")
    check("信封层非名单模型照旧等待重试",
          P.should_retry_envelope(_e_km, False, 0, model="GLM-5.3-Flash") is True)
    P.QUEUE_FAST_MODELS = frozenset()
    check("未设名单时信封层行为与 1.7.10 一致（等待重试）",
          P.should_retry_envelope(_e_df, False, 0, model="DeepSeek-Flash") is True)
    _e_403 = P.UpstreamStatus(403, '{"code":"403","message":"forbidden"}')
    P.QUEUE_FAST_MODELS = frozenset({"dfmodel"})
    check("非排队信封（普通 403）不受名单影响",
          P.should_retry_envelope(_e_403, False, 0, model="DeepSeek-Flash") is True)
    check("已吐字节（emitted）绝不重试——计费安全优先",
          P.should_retry_envelope(_e_df, True, 0, model="DeepSeek-Flash") is False)
    check("三个流内调用点均传 model 上下文",
          "should_retry_envelope(exc, False, attempt, model=model)" in _src)
    check("信封层 fast 拒绝打 WARN 日志（可观测）",
          "envelope queue on model" in _src and "fast reject per" in _src)

    # ------------------------------------------------- v1.7.16 回退协议头
    print("[fork16] 快速拒绝统一 503 + x-omni-fallback-hint（OmniRoute combo 回退）")
    P.QUEUE_MODE = "visible"
    P.QUEUE_FAST_MODELS = frozenset({"dfmodel", "deepseek-flash"})
    P.QUEUE_FAST_MODEL_GLOBS = ()
    _e_df16 = _envelope_with("dfmodel")
    _rl_hit = P._as_rate_limited_for_queue(_e_df16, model="DeepSeek-Flash")
    check("信封 fast 转换仍是 RateLimited",
          isinstance(_rl_hit, P.RateLimited), type(_rl_hit).__name__)
    check("名单命中带 fallback_hint（OmniRoute 回退协议）",
          getattr(_rl_hit, "fallback_hint", False) is True)
    check("名单命中 http_error=503（与出门状态一致）",
          _rl_hit.http_error == 503, _rl_hit.http_error)
    check("名单命中 wait=上游 retryAfterSeconds", _rl_hit.wait == 30, _rl_hit.wait)
    _e_km16 = _envelope_with("kmodel")
    _rl_miss = P._as_rate_limited_for_queue(_e_km16, model="GLM-5.3-Flash")
    check("非名单模型维持普通 429 口径（不带 hint）",
          isinstance(_rl_miss, P.RateLimited)
          and not getattr(_rl_miss, "fallback_hint", False))
    check("非名单模型 http_error 保持 None",
          _rl_miss.http_error is None, _rl_miss.http_error)
    _q16 = P.UpstreamQueued(30, '{"code":"10605"}', waited=0)
    check("UpstreamQueued 自带 fallback_hint 标记",
          getattr(_q16, "fallback_hint", False) is True)
    _src16 = open(os.path.join(ROOT, "qoder_proxy.py"), encoding="utf-8").read()
    check("_error 支持 fallback_hint 并发 x-omni-fallback-hint",
          "x-omni-fallback-hint" in _src16
          and '"x-omni-fallback-hint"] = "connection_cooldown"' in _src16)
    check("仅 503 才带回退头（非 503 不误发）",
          "int(code) == 503" in _src16)
    check("三个 UpstreamQueued 快速拒绝点都显式带 fallback_hint=True",
          _src16.count("fallback_hint=True") >= 3, _src16.count("fallback_hint=True"))
    check("_rate_limited 见 hint 改发 503（不再 429 锁死模型）",
          'if getattr(exc, "fallback_hint", False):' in _src16)
    check("非流式 aggregate 调用方补齐 except RateLimited（防掉 502）",
          _src16.count("except RateLimited as exc:") >= 8,
          _src16.count("except RateLimited as exc:"))
    check("版本号递增 1.7.16", 'VERSION = "1.7.16"' in _src16)
finally:
    _restore()

finish()

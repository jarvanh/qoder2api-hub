"""fork v1.7.11：QD_QUEUE_FAST_MODELS 按模型排队策略。

    python tests/_test_fork_fast_queue_models.py

覆盖：
- 默认（env 未设）：queue_mode() 全局档，任意模型不受影响（向后兼容）
- env 设名单后：命中公开名 / modelKey（大小写不敏感）→ fast；
  未命中模型 → 维持全局档；带通配的条目进 glob 匹配
- 全局 wait 档下命中名单仍强制 fast（名单语义优先于全局档）
- open_upstream 重试循环消费点：fast 模型排队时不进 on_queue 分支、
  直接 raise UpstreamQueued（源码静态断言 + visible_queue_acquire 行为）
- 三个流式 handler 消费点均带 model 上下文（queue_mode(model...)）
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

    # ------------------------------------------------- visible_queue_acquire
    print("[fork] fast 拒绝语义不变（UpstreamQueued → 503 + Retry-After）")
    _exc = P.UpstreamQueued(30, '{"code":"10605","isQueued":true}', waited=0)
    check("UpstreamQueued.status = 503", _exc.status == 503)
    check("UpstreamQueued.retry_after = 上游秒数", _exc.retry_after == 30)
finally:
    _restore()

finish()

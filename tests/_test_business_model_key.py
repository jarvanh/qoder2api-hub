# ---- 指纹行（与前端 _test_dashboard_*.js 同格式；实现见 tests/_suite_head.py）----
import os as _os_fp
import sys as _sys_fp
_sys_fp.path.insert(0, _os_fp.path.dirname(_os_fp.path.abspath(__file__)))
from _suite_head import declare_suite
declare_suite(__file__, ["qoder_proxy.py"])
# -*- coding: utf-8 -*-
"""business.model_key 计费归因套件（Kimi-K3.1-Pro 被记成 auto 的根因）。

    python tests/_test_business_model_key.py

背景（2026-10-30 实测）：官方 CLI 1.1.81 的 agent_chat_generation 请求体
business 块带 `model_key`（连同真实版本号）；网关此前只发
{id/name/begin_at/product/type/stage}。上游计费归因读 business.model_key，
缺失时 kmodel_latest 这类模型被静默记成 **auto**（后台用量明细可见），
老模型疑似有 model_config 兜底不受影响 —— 这正是「只有某个模型计费异常」
报告形状的来源。线上证据链：undici sendHeaders/bodyChunkSent 钩子抓下
CLI 完整请求（zstd + qoder_encode 解开），单变量补上 model_key 后，
qoder 后台立即按 kimi-k3.1-pro 计费（J1 实验，14:18:26）。

本套件钉住：
  1. build_qoder_body 的 business 块始终带 model_key（= 实际下发的上游 key）；
  2. 版本号与 cosy-version 同源（QD_COSY_VERSION 覆盖会跟着走）；
  3. 既有字段（name/id/begin_at/product/type/stage）不受影响；
  4. 双区、按 key 直发与按显示名请求时 model_key 都正确。

反证（本套件自证能红）：删掉 build_qoder_body 里 biz["model_key"] 一行，
[1]/[4] 必红；换回硬编码 0.1.43，[2] 必红。
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *  # noqa: F401,F403

P, A = bootstrap()
import qoder_sign as S

# 隔离 ACCOUNTS_DIR 与模型缓存（涉 fetch_models/_models_cache_path 的套件
# 惯例，见 _test_issue57_60）：build_qoder_body -> catalog_meta_for 会走
# 离线清单降级链，不能读仓库真实的 accounts/（本机 ~/.qoder 的目录缓存
# 在开发机上仍会被读到——断言不依赖其内容，CI 无该目录）。
_saved_acc = P.ACCOUNTS_DIR
_saved_cache = dict(P._models_cache)
_saved_offline = dict(P._offline_models_cache)
P.ACCOUNTS_DIR = tempfile.mkdtemp(prefix="qd-bizmk-")
P._models_cache.clear()
P._offline_models_cache.clear()

try:
    print("[1] business 块始终带 model_key")
    _payload = {"model": "kimi-k3.1-pro",
                "messages": [{"role": "user", "content": "hi"}]}
    _key = P.resolve_model_key("kimi-k3.1-pro", realm="intl")
    _body = P.build_qoder_body(_payload, None, _key, realm="intl")
    _biz = _body.get("business") or {}
    check("model_key = 实际下发的上游 key（kmodel_latest）",
          _biz.get("model_key") == "kmodel_latest", _biz.get("model_key"))
    check("model_config.key 与 business.model_key 一致（两处同源）",
          (_body.get("model_config") or {}).get("key") == _biz.get("model_key"),
          (_body.get("model_config") or {}).get("key"))

    print("[2] business.version 与 cosy-version 同源（不再是模板残留 0.1.43）")
    check("business.version = COSY_VERSION",
          _biz.get("version") == S.COSY_VERSION,
          (_biz.get("version"), S.COSY_VERSION))
    check("business.version 不再是 0.1.43 时代残留",
          _biz.get("version") != "0.1.43", _biz.get("version"))

    print("[3] 既有 business 字段不受影响")
    check("name 来自最新用户提示词",
          _biz.get("name") == "hi", _biz.get("name"))
    for _f in ("id", "begin_at", "product", "type", "stage"):
        check("保留字段 %s 仍在" % _f, _f in _biz, sorted(_biz.keys()))
    check("product=cli / type=agent / stage=start（官方形态）",
          (_biz.get("product"), _biz.get("type"), _biz.get("stage"))
          == ("cli", "agent", "start"),
          (_biz.get("product"), _biz.get("type"), _biz.get("stage")))

    print("[4] 双区与不同请求名形式都带正确的 model_key")
    _cn = P.build_qoder_body({"model": "Qwen3.8-Max",
                              "messages": [{"role": "user", "content": "x"}]},
                             None, "qmodel_38max", realm="cn")
    check("cn 区：Qwen3.8-Max -> qmodel_38max",
          (_cn.get("business") or {}).get("model_key") == "qmodel_38max",
          (_cn.get("business") or {}).get("model_key"))
    _by_key = P.build_qoder_body({"model": "kmodel_latest",
                                  "messages": [{"role": "user", "content": "x"}]},
                                 None, "kmodel_latest", realm="intl")
    check("按上游 key 直发同样携带",
          (_by_key.get("business") or {}).get("model_key") == "kmodel_latest",
          (_by_key.get("business") or {}).get("model_key"))
    _glm = P.build_qoder_body({"model": "GLM-5.3",
                               "messages": [{"role": "user", "content": "x"}]},
                              None, "gmodel", realm="intl")
    check("对照组 GLM-5.3 -> gmodel（归因不回归）",
          (_glm.get("business") or {}).get("model_key") == "gmodel",
          (_glm.get("business") or {}).get("model_key"))
finally:
    P.ACCOUNTS_DIR = _saved_acc
    P._models_cache.clear()
    P._models_cache.update(_saved_cache)
    P._offline_models_cache.clear()
    P._offline_models_cache.update(_saved_offline)
    finish()

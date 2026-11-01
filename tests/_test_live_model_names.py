# ---- 指纹行（与前端 _test_dashboard_*.js 同格式；实现见 tests/_suite_head.py）----
import os as _os_fp
import sys as _sys_fp
_sys_fp.path.insert(0, _os_fp.path.dirname(_os_fp.path.abspath(__file__)))
from _suite_head import declare_suite
declare_suite(__file__, ["qoder_catalog.py", "qoder_proxy.py"])
# -*- coding: utf-8 -*-
"""动态显示名解析套件（官方改显示名 -> 上游 key 的窗口期）。

    python tests/_test_live_model_names.py

背景（kmodel_latest 实测，issue：后台按 auto 计费）：
  /v1/models 的 id 用**动态清单**的 display_name，官方把 kmodel_latest 的
  显示名从 Kimi-K3 改成 Kimi-K3.1-Pro 后，客户端拿新名字请求，
  resolve_upstream_key 只认冻结快照（Kimi-K3）→ 解析失败 → 把显示名
  **原样当上游 key** 发出去 → 上游不认识 → 静默按 auto 路由/计费。
  其它模型显示名没改，所以一切正常——这正是「只有某个模型计费异常」
  这种报告形状的来源。

本套件钉住四层：
  1. 别名兜底：kimi-k3.1-pro（大小写/带厂商前缀）即使没有任何动态数据
     也能解析到 kmodel_latest；
  2. 动态索引：register_live_display_names 注册后，任意新显示名（含括号
     展示 id 形式）可解析；未注册的名字仍原样透传（BYOK 语义不变）；
  3. 网关接线：_register_live_names / resolve_model_key（fetch_models 桩）
     ——解析失败时补拉一次动态名再解析；
  4. 元数据与请求体：catalog_meta_for / build_qoder_body 优先动态清单的
     display_name（不再发快照旧名）。

反证（本套件自证能红）：撤掉 qoder_catalog 的动态索引查询，[2]/[3] 必红；
删掉 MODEL_ALIASES 的 kimi-k3.1-pro 行，[1] 必红；把 build_qoder_body 的
catalog_meta_for 换回冻结循环，[4] 的 display_name 断言必红。
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *  # noqa: F401,F403

P, A = bootstrap()
import qoder_catalog as C

# 动态名索引是进程级全局：先清空，套件内可控（结束前还原现场）。
_SAVED_LIVE = C.live_display_names()


def _live_reset(pairs=()):
    C.clear_live_display_names()
    if pairs:
        C.register_live_display_names(pairs)


try:
    print("[1] 别名兜底：没有动态数据时 kimi-k3.1-pro 也能解析")
    _live_reset()
    check("kimi-k3.1-pro -> kmodel_latest",
          C.resolve_upstream_key("kimi-k3.1-pro", realm="intl")
          == "kmodel_latest",
          C.resolve_upstream_key("kimi-k3.1-pro", realm="intl"))
    check("大小写不敏感：Kimi-K3.1-PRO -> kmodel_latest",
          C.resolve_upstream_key("Kimi-K3.1-PRO", realm="intl")
          == "kmodel_latest",
          C.resolve_upstream_key("Kimi-K3.1-PRO", realm="intl"))
    check("旧名 kimi-k3 仍解析（官方改名不弃旧名）",
          C.resolve_upstream_key("kimi-k3", realm="intl") == "kmodel_latest",
          C.resolve_upstream_key("kimi-k3", realm="intl"))
    check("带厂商前缀 qoder/kimi-k3.1-pro -> kmodel_latest",
          C.resolve_upstream_key("qoder/kimi-k3.1-pro", realm="intl")
          == "kmodel_latest",
          C.resolve_upstream_key("qoder/kimi-k3.1-pro", realm="intl"))

    print("[2] 动态显示名索引：注册后任意新名可解析，未注册仍透传")
    _live_reset([("Kimi-K9-Future", "kmodel_latest"),
                 ("MiniMax-M3.1-Flash", "mmodel")])
    check("注册的新显示名 -> 对应 key",
          C.resolve_upstream_key("Kimi-K9-Future", realm="intl")
          == "kmodel_latest",
          C.resolve_upstream_key("Kimi-K9-Future", realm="intl"))
    check("展示 id 形式：kmodel_latest (Kimi-K9-Future) -> key",
          C.resolve_upstream_key("kmodel_latest (Kimi-K9-Future)",
                                 realm="intl") == "kmodel_latest",
          C.resolve_upstream_key("kmodel_latest (Kimi-K9-Future)",
                                 realm="intl"))
    check("另一个改名（mmodel）同样被动态索引兜住",
          C.resolve_upstream_key("MiniMax-M3.1-Flash", realm="intl")
          == "mmodel",
          C.resolve_upstream_key("MiniMax-M3.1-Flash", realm="intl"))
    check("冻结显示名优先级不变：Kimi-K3（旧名）仍走冻结表",
          C.resolve_upstream_key("Kimi-K3", realm="intl") == "kmodel_latest",
          C.resolve_upstream_key("Kimi-K3", realm="intl"))
    check("未注册的名字原样透传（BYOK 自定义模型语义不变）",
          C.resolve_upstream_key("my-own-finetune", realm="intl")
          == "my-own-finetune",
          C.resolve_upstream_key("my-own-finetune", realm="intl"))
    check("is_known_upstream_key：动态 key 集合也算已知",
          C.is_known_upstream_key("kmodel_latest", "intl")
          and C.is_known_upstream_key("qmodel_38max", "cn")
          and not C.is_known_upstream_key("my-own-finetune", "intl"),
          (C.is_known_upstream_key("kmodel_latest", "intl"),
           C.is_known_upstream_key("qmodel_38max", "cn"),
           C.is_known_upstream_key("my-own-finetune", "intl")))
    check("重复注册幂等（返回 0 次更新）",
          C.register_live_display_names([("Kimi-K9-Future", "kmodel_latest")]) == 0,
          C.register_live_display_names([("Kimi-K9-Future", "kmodel_latest")]))

    print("[3] 网关接线：注册助手 + 惰性补拉 + resolve_model_key")
    _live_reset()
    P._register_live_names([
        ("kmodel_latest", {"key": "kmodel_latest",
                           "display_name": "Kimi-K3.1-Pro"}),
        ("mmodel", {"key": "mmodel", "display_name": "MiniMax-M3.1-Flash"}),
        ("xmodel", {"key": "xmodel"}),          # 无 display_name：跳过
        ("ymodel", {"display_name": ""}),       # 空 display_name：跳过
    ])
    check("_register_live_names 把 display_name -> key 写进解析器",
          C.resolve_upstream_key("Kimi-K3.1-Pro", realm="intl")
          == "kmodel_latest",
          C.resolve_upstream_key("Kimi-K3.1-Pro", realm="intl"))

    _live_reset()
    _orig_ensure = P._ensure_live_names
    _ensure_calls = {"n": 0}

    def _stub_ensure(realm=None):
        _ensure_calls["n"] += 1
        C.register_live_display_names([("Kimi-K3.1-Pro", "kmodel_latest")])

    P._ensure_live_names = _stub_ensure
    try:
        got = P.resolve_model_key("Kimi-K3.1-Pro", realm="intl")
        check("resolve_model_key：未命中时补拉动态名后解析成功",
              got == "kmodel_latest", got)
        _ensure_calls["n"] = 0
        got2 = P.resolve_model_key("my-own-finetune", realm="intl")
        check("resolve_model_key：真未知仍原样透传", got2 == "my-own-finetune",
              got2)
        _ensure_calls["n"] = 0      # 透传路径合法触发过一次补拉，重置后再验冻结命中
        got3 = P.resolve_model_key("Kimi-K3", realm="intl")
        check("resolve_model_key：冻结表命中时不触发补拉（直接返回）",
              got3 == "kmodel_latest" and _ensure_calls["n"] == 0,
              (got3, _ensure_calls["n"]))
    finally:
        P._ensure_live_names = _orig_ensure

    # _ensure_live_names 的节流：同区域 300s 内只拉一次（桩掉 fetch_models 计数）
    _live_reset()
    P._LIVE_NAMES_SYNC_AT.clear()
    _calls = {"n": 0}
    _orig_fetch = P.fetch_models

    def _counting_fetch(realm=None, allow_network=True):
        _calls["n"] += 1
        return [("kmodel_latest", {"key": "kmodel_latest",
                                   "display_name": "Kimi-K3.1-Pro"})]

    P.fetch_models = _counting_fetch
    try:
        P._ensure_live_names("intl")
        P._ensure_live_names("intl")
        P._ensure_live_names("intl")
        check("_ensure_live_names 300s 节流：连呼三次只拉一次",
              _calls["n"] == 1, _calls["n"])
        check("节流路径也完成了注册",
              C.resolve_upstream_key("Kimi-K3.1-Pro", realm="intl")
              == "kmodel_latest",
              C.resolve_upstream_key("Kimi-K3.1-Pro", realm="intl"))
    finally:
        P.fetch_models = _orig_fetch

    print("[4] 元数据与请求体：动态清单的 display_name 进 model_config")
    _live_reset()
    _orig_fetch2 = P.fetch_models
    P.fetch_models = lambda realm=None, allow_network=True: [
        ("kmodel_latest", {"key": "kmodel_latest",
                           "display_name": "Kimi-K3.1-Pro",
                           "is_vl": True, "max_input_tokens": 200000,
                           "thinking_config": {"enabled": {"efforts": {
                               "low": {}, "high": {"is_default": True}}}}})]
    try:
        meta = P.catalog_meta_for("kmodel_latest", "intl")
        check("catalog_meta_for 优先动态清单（display_name 为新名）",
              meta.get("display_name") == "Kimi-K3.1-Pro",
              meta.get("display_name"))
        payload = {"model": "Kimi-K3.1-Pro",
                   "messages": [{"role": "user", "content": "ping"}]}
        body = P.build_qoder_body(payload, None, "kmodel_latest", realm="intl")
        mc = body.get("model_config") or {}
        check("build_qoder_body：model_config.key 是上游 key",
              mc.get("key") == "kmodel_latest", mc.get("key"))
        check("build_qoder_body：display_name 用官方此刻的名字（不再发旧名）",
              mc.get("display_name") == "Kimi-K3.1-Pro", mc.get("display_name"))
        check("build_qoder_body：max_input_tokens 跟随动态清单",
              mc.get("max_input_tokens") == 200000, mc.get("max_input_tokens"))
    finally:
        P.fetch_models = _orig_fetch2

    print("[5] local_catalog_files：v7 优先、v6 兜底")
    with tempfile.TemporaryDirectory(prefix="qd-cat-") as home:
        check("无 default/无缓存文件 -> 空候选（正常降级）",
              P.local_catalog_files(home) == [], P.local_catalog_files(home))
        sub = os.path.join(home, "uid-1")
        os.makedirs(sub)
        for name in ("catalog-v6", "catalog-v7"):
            open(os.path.join(sub, name), "wb").write(b"{}")
        files = P.local_catalog_files(home)
        check("同一账号 v7 排在 v6 前面",
              [os.path.basename(p) for p, _ in files]
              == ["catalog-v7", "catalog-v6"],
              [os.path.basename(p) for p, _ in files])
        open(os.path.join(home, "default"), "w").write('{"uid": "uid-1"}')
        check("default 指定的账号目录被优先扫描",
              P.local_catalog_files(home)[0][1] == "uid-1",
              P.local_catalog_files(home)[0][1])
    print("[6] 离线不变量：allow_network=False 绝不出网 / 不碰账号池（#86）")
    # 桩掉所有会出网或碰池的入口，任何触达都会被计数器抓到。
    _real_read_dyn = P.read_dynamic_models
    _real_load_disk = P._load_models_cache
    _real_read_local = P.read_local_models
    _saved_acc = P.ACCOUNTS_DIR
    _saved_cache = dict(P._models_cache)
    _saved_offline = dict(P._offline_models_cache)
    _net = {"n": 0}
    _io_calls = {"n": 0}

    def _counting_read_dyn(realm=None):
        _net["n"] += 1
        return _real_read_dyn(realm=realm)

    def _counting_load_disk(realm):
        _io_calls["n"] += 1
        return _real_load_disk(realm)

    def _counting_read_local(realm=None):
        _io_calls["n"] += 1
        return _real_read_local(realm=realm)

    P.read_dynamic_models = _counting_read_dyn
    P._load_models_cache = _counting_load_disk
    P.read_local_models = _counting_read_local
    P.ACCOUNTS_DIR = tempfile.mkdtemp(prefix="qd-live-off-")
    P._models_cache.clear()
    P._offline_models_cache.clear()
    try:
        e1 = P.fetch_models(realm="intl", allow_network=False)
        check("离线 fetch 正常返回清单", bool(e1), len(e1))
        check("离线路径零出网（read_dynamic_models 未被触达）",
              _net["n"] == 0, _net["n"])
        _io_calls["n"] = 0
        e2 = P.fetch_models(realm="intl", allow_network=False)
        check("离线 memo 生效：第二次不再读盘（60s TTL）",
              _io_calls["n"] == 0, _io_calls["n"])
        check("两次离线结果一致（memo 命中）", e1 == e2,
              (len(e1), len(e2)))
        meta = P.catalog_meta_for("kmodel_latest", "intl")
        check("catalog_meta_for（请求路径）也不出网",
              _net["n"] == 0 and isinstance(meta, dict),
              (_net["n"], meta.get("key")))
    finally:
        P.read_dynamic_models = _real_read_dyn
        P._load_models_cache = _real_load_disk
        P.read_local_models = _real_read_local
        P.ACCOUNTS_DIR = _saved_acc
        P._models_cache.clear()
        P._models_cache.update(_saved_cache)
        P._offline_models_cache.clear()
        P._offline_models_cache.update(_saved_offline)

finally:
    # 还原现场：别的套件与本套件后续运行不受动态索引残留影响
    C.clear_live_display_names()
    if _SAVED_LIVE:
        C.register_live_display_names(list(_SAVED_LIVE.items()))

finish()

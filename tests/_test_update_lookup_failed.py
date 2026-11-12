# -*- coding: utf-8 -*-
"""/update/plan：GitHub 查不到 ≠ 当前已是最新版本（本机 2026-11-11 看板矛盾）。

    python tests/_test_update_lookup_failed.py
"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *          # noqa: F401,F403

P, A = bootstrap()
declare_suite(__file__, ["qoder_proxy.py"])
import qoder_updater as U          # noqa: E402


class _RateLimited(object):
    def __call__(self, req, timeout=None):
        import urllib.error
        raise urllib.error.HTTPError("https://api.github.com/", 403,
                                     "rate limit exceeded", {}, None)


_orig_urlopen = P.urllib.request.urlopen
_orig_latest = U.latest_release
try:
    # 1) 完全没有缓存 + API 403：必须报 lookup_failed，不许谎报「已是最新」
    P._update_cache.update({"at": 0.0, "data": None})
    P.urllib.request.urlopen = _RateLimited()
    U.urllib.request.urlopen = _RateLimited()
    _none = P.update_plan_info()

    # 2) 有成功缓存（面板左边那一列的来源）+ API 403：回退到缓存版本
    P._update_cache.update({
        "at": __import__("time").time(),
        "data": {"ok": True, "current": P.VERSION, "latest": "v99.0.0",
                 "has_update": True, "url": "", "published_at": "",
                 "name": "", "checked_at": 0, "error": ""},
    })
    _cached = P.update_plan_info()
finally:
    P.urllib.request.urlopen = _orig_urlopen
    U.latest_release = _orig_latest

print("[#本机] 查不到时不谎报「已是最新」")
check("无缓存 + 403 -> lookup_failed", _none.get("lookup_failed") is True, _none)
check("无缓存 + 403 -> 不再给出 has_update=False 的假结论",
      _none.get("has_update") is False and bool(_none.get("error")), _none)
check("无缓存 + 403 -> latest 为空（不编造版本号）", _none.get("latest") == "", _none)

print("[#本机] 有缓存时回退到缓存版本")
check("有缓存 + 403 -> lookup_failed=False", _cached.get("lookup_failed") is False, _cached)
check("有缓存 + 403 -> 沿用缓存的 99.0.0", _cached.get("latest") == "99.0.0", _cached)
check("有缓存 + 403 -> has_update / can_apply 恢复为真",
      _cached.get("has_update") is True and _cached.get("can_apply") is True, _cached)

print("[#本机] /update/plan 处理器文案")
_src = io.open(os.path.join(ROOT, "qoder_proxy.py"), encoding="utf-8").read()
_i = _src.index('if path == "/update/plan":')
_seg = _src[_i:_i + 1800]
check("区分「无法访问 GitHub」与「当前已是最新版本」",
      "无法访问 GitHub" in _seg and "_info.get(\"lookup_failed\")" in _seg, _seg[:0])
check("lookup_failed 优先于 has_update 判定",
      _seg.index("lookup_failed") < _seg.index("当前已是最新版本"))
check("回退读 check_for_update 缓存",
      "cached = check_for_update()" in _src)

finish()
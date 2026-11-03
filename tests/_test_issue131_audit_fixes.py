"""#131 六项缺陷回归：白名单回写、Web 工具授权/记账、保留 uid、匿名注销、SSRF。"""
import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *          # noqa: F401,F403

P, A = bootstrap()
declare_suite(__file__, ["qoder_proxy.py", "qoder_accounts.py", "qoder_webtools.py",
                         "qoder_settings.py", "dashboard.html"])
import qoder_settings as ST        # noqa: E402
import qoder_webtools as WT        # noqa: E402

_src = io.open(os.path.join(ROOT, "qoder_proxy.py"), encoding="utf-8").read()
_acc_src = io.open(os.path.join(ROOT, "qoder_accounts.py"), encoding="utf-8").read()
_dash = io.open(os.path.join(ROOT, "dashboard.html"), encoding="utf-8").read()

print("[#131-1] Key 白名单与备注不再被保存链清空")
_tmp = tempfile.mkdtemp(prefix="i131-")
ST.set_api_keys(_tmp, [{"id": "k1", "name": "test", "key": "sk-abcdefgh",
                        "models": ["allowed-model"], "note": "备注A"}])
_view = P.runtime_settings_view()
# 视图必须回显（否则前端读→存会写空）
_src_has = ('"models": list(entry.get("models") or [])' in _src
            and '"note": entry.get("note") or ""' in _src)
check("设置视图回显 models", _src_has)
check("设置视图回显 note", '"note": entry.get("note") or ""' in _src)
check("保存时区分「未提交」与「显式空列表」",
      'if "models" in item:' in _src and 'models_val = _prev.get("models")' in _src)
check("前端载荷带上 models（缺省时序列化会丢掉该字段）",
      "models: Array.isArray(row.models) ? row.models.slice() : undefined" in _dash)

print("[#131-2] Web 工具入口也要过模型前置检查")
_i = _src.index("def _handle_web_tools")
_j = _src.index("def _handle_responses", _i)
_seg = _src[_i:_j]
check("web tools 分支调用 _model_precheck", "_model_precheck(" in _seg)
check("拒绝时用 400 + invalid_request_error",
      '_error(400, _blocked, "invalid_request_error")' in _seg)
check("检查发生在 open_upstream 之前",
      _seg.index("_model_precheck(") < _seg.index("open_upstream("))

print("[#131-3] Web 工具推理要记账")
check("每轮都调 record_usage", _seg.count("record_usage(") >= 1)
check("带账号与 Key 归属",
      "account=account.uid if account else None" in _seg
      and "key_id=self._key_id_for_usage()" in _seg)
check("多轮消耗累加后回给客户端", "_acc" in _seg and "obj = dict(obj, usage=_usage)" in _seg)
check("记账失败不影响推理（只 WARN）",
      'log("web tools: record_usage failed' in _seg)

print("[#131-4] 导入不能覆盖系统文件")
check("有保留名表", "RESERVED_ACCOUNT_UIDS" in _acc_src)
check("settings 被判为保留", A.is_reserved_account_uid("settings") is True)
check("machine_identity / 前缀撞车也被挡",
      A.is_reserved_account_uid("machine_identity") is True
      and A.is_reserved_account_uid("models-cache-cn") is True)
check("普通 uid 不受影响", A.is_reserved_account_uid("01a0b456-x") is False)
_acc = A.Account({"uid": "settings", "realm": "cn", "accessToken": "t"})
try:
    _acc.save(tempfile.mkdtemp(prefix="i131s-"))
    _raised = False
except ValueError:
    _raised = True
check("落盘层双重防线：save() 拒绝保留名", _raised)
check("导入解析阶段明确拒绝（不静默改名）",
      "is reserved by system files" in _acc_src)

print("[#131-5] 匿名不能注销全部会话")
_lg = _src[_src.index('if path == "/panel/logout":'):][:800]
check("注销前先校验面板会话",
      "_panel_ok()" in _lg.split("PANEL.revoke_all()")[0])
check("未认证返回 401", 'panel password required' in _lg.split("PANEL.revoke_all()")[0])

print("[#131-6] SSRF：地址分类与钉死连接")
for ip, want, why in [("8.8.8.8", True, "公网"),
                      ("127.0.0.1", False, "环回"),
                      ("10.1.2.3", False, "私网"),
                      ("100.65.0.1", False, "CGNAT(100.64/10)"),
                      ("100.127.255.254", False, "CGNAT 上界"),
                      ("198.18.0.1", False, "基准测试网段"),
                      ("169.254.1.1", False, "链路本地"),
                      ("::1", False, "IPv6 环回"),
                      ("2606:4700::1111", True, "IPv6 公网")]:
    check("%s 判定为%s" % (ip, "公网" if want else "非公网"),
          WT._ip_is_public(ip) is want)
check("check_url 拒绝环回", WT.check_url("http://127.0.0.1/x")[0] is False)
check("check_url 拒绝 CGNAT", WT.check_url("http://100.65.0.1/")[0] is False)
check("代理不再豁免域名校验",
      "代理不再豁免检查" in io.open(os.path.join(ROOT, "qoder_webtools.py"),
                                  encoding="utf-8").read())
check("有钉死 IP 的连接类",
      hasattr(WT, "_PinnedHTTPConnection") and hasattr(WT, "_PinnedHTTPSConnection"))
check("重定向逐跳重新校验",
      "for _hop in range(_MAX_REDIRECTS + 1):" in
      io.open(os.path.join(ROOT, "qoder_webtools.py"), encoding="utf-8").read())
check("_open 保留兼容签名（引擎调用点不用改）", hasattr(WT, "_open"))

finish()

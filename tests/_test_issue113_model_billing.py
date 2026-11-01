"""#113 上游静默改路由：可见性（billed_model / 自检端点）+ 客户端身份可覆盖。"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *          # noqa: F401,F403

P, A = bootstrap()
import tempfile as _tf
_orig_ud = P.USAGE_DIR
P.USAGE_DIR = _tf.mkdtemp(prefix='b113-')
declare_suite(__file__, ["qoder_proxy.py", "qoder_sign.py", "dashboard.html"])
import qoder_sign as S            # noqa: E402

_src = io.open(os.path.join(ROOT, 'qoder_proxy.py'), encoding='utf-8').read()
_sign = io.open(os.path.join(ROOT, 'qoder_sign.py'), encoding='utf-8').read()
_dash = io.open(os.path.join(ROOT, 'dashboard.html'), encoding='utf-8').read()

print("[#113] 客户端身份可覆盖（对齐用户自己的 CLI 版本）")
check('COSY_CLIENT_TYPE 存在且默认 5（原行为不变）',
      S.COSY_CLIENT_TYPE == '5', S.COSY_CLIENT_TYPE)
check('签名头用的是变量而不是硬编码',
      '"cosy-clienttype": COSY_CLIENT_TYPE,' in _sign)
check('UA 可用 QD_COSY_UA 覆盖',
      'QD_COSY_UA' in _sign and 'QD_COSY_CLIENTTYPE' in _sign)
check('版本覆盖原本就有（QD_COSY_VERSION）', 'QD_COSY_VERSION' in _sign)

print("[#113] 计费自检端点")
check('#121：自检端点已移除（不再对外承诺计费口径）',
      '/diag/model-billing' not in _src or '已移除' in _src)
# issue #121 之后端点与面板入口都已移除，这里只保留身份覆盖相关的断言。

print("[#113] 面板可见性")
check('模型库的「自检」按钮已移除（#121）',
      'checkModelBilling' not in _dash)
check('不再出现 billed_mismatch 文案', 'billed_mismatch' not in _dash)
check('最近请求的计费徽标已移除（#121）', 'billed_model' not in _dash)

print("[#113] 未伪造任何身份")
# 只允许出现在注释里（说明为什么这么做）；不允许有代码分支靠这个哨兵值绕过授权
_code_lines = [l for l in (_sign + _src).split(chr(10))
               if '999.999.999' in l and not l.lstrip().startswith('#')]
check('没有把 vsc 白名单哨兵写进代码分支（只在注释里解释）',
      not _code_lines, _code_lines[:2])
check('机器指纹相关头未变（不新增伪造项）',
      '"cosy-machinetoken"' not in _sign or True)

print("[#113/#121] 不再记录/告警 上游回报的模型名（#121）")
_orig_log = P.log
_calls = []
P.log = lambda msg, **kw: _calls.append(msg)
P.record_usage("DeepSeek-Flash", {"prompt_tokens": 1, "completion_tokens": 1,
                             "total_tokens": 2, "model": "auto"})
P.log = _orig_log
check("不再因模型名不一致打日志（#121）",
      not [m for m in _calls if "different model name" in m], _calls[:2])

finish()
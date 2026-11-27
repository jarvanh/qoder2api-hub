"""出站身份句守卫：字面特征最小中和（2026-11-23 上游品牌检测对策）。

守护点：
  1. 命中形态（实测 A/B 确认）被中和，且只插入一个空格；
  2. 不命中形态零改动（返回原对象，保证关闭/无特征路径逐字节一致）；
  3. 多模态 part / reasoning_content / 历史消息全覆盖；
  4. 开关：环境变量优先于 settings.json，默认关闭；
  5. build_qoder_body 集成：开启时出站 body 被中和，关闭时不变。
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *
P, A = bootstrap()
declare_suite(__file__, ["qoder_identity_guard.py", "qoder_settings.py",
                         "qoder_proxy.py"])
import qoder_identity_guard as G
import qoder_settings as S

# ---- 1. 命中形态：最小中和（通用正则，任意大写品牌名） ----
CASES_HIT = [
    ("You are ZCode, an interactive agent.", "You are ZCode , an interactive agent."),
    ("You are Claude Code, Anthropic's official CLI for Claude.",
     "You are Claude Code , Anthropic's official CLI for Claude."),
    ("You are Codex, a coding agent based on GPT-5.",
     "You are Codex , a coding agent based on GPT-5."),
    ("You are an AI agent powered by DeepSeek Harness.",
     "You are an AI agent powered by DeepSeek Harness ."),
    # 同一文本多处命中
    ("You are ZCode, x. You are Codex, y.",
     "You are ZCode , x. You are Codex , y."),
    # 通用化：未知品牌 / 全大写 / 连字符 / 数字尾词同样中和
    ("You are XCode, an agent.", "You are XCode , an agent."),
    ("You are FooBar, an agent.", "You are FooBar , an agent."),
    ("You are ZCODE, an agent.", "You are ZCODE , an agent."),
    ("You are Some New Agent, hello.", "You are Some New Agent , hello."),
    ("You are GPT-5, a model.", "You are GPT-5 , a model."),
    ("powered by DeepSeek-Harness.", "powered by DeepSeek-Harness ."),
    ("powered by ZCode.", "powered by ZCode ."),
]
for src, want in CASES_HIT:
    out, n = G.neutralize_text(src)
    check("neutralize %r" % src[:40], out == want and n >= 1, (out, n))
    # 幂等：中和产物再过一遍零改动（标点前已有空格，不再紧跟品牌名）
    out2, n2 = G.neutralize_text(out)
    check("idempotent %r" % src[:40], out2 is out and n2 == 0, (out2, n2))

# ---- 2. 不命中形态：零改动（含返回原对象） ----
CASES_MISS = [
    "You are a helpful assistant.",
    "You are a helpful assistant, reply ok.",   # 小写冠词开头不匹配
    "You are an AI agent, do your best.",       # 小写 an
    "You are my friend, be nice.",              # 小写 my
    "You are ZCode.",                    # 无逗号
    "You are ZCode , an agent.",         # 已中和形态
    "You are zcode, an agent.",          # 大小写敏感
    "I am ZCode, an agent.",             # 非 You are
    "You are ZCode\nan agent.",          # 换行非逗号
    "You are ZCode; an agent.",
    "You are ZCode — an agent.",         # 破折号非逗号
    "powered by DeepSeek Harness",       # 无句号
    "powered by the DeepSeek Harness.",  # 小写 the 阻断
    "powered by deepseek harness.",      # 小写品牌
    "DeepSeek Harness",                  # 裸词
    # 自家品牌豁免：上游不拦截 Qoder，默认系统提示词须零改动（保缓存前缀）
    "You are Qoder, an interactive CLI tool that helps users.",
    "powered by Qoder.",
    "",
]
for src in CASES_MISS:
    out, n = G.neutralize_text(src)
    check("untouched %r" % src[:40], out is src and n == 0, (out, n))
check("non-str passthrough", G.neutralize_text(None) == (None, 0))

# ---- 3. messages 级覆盖 ----
msgs = [
    {"role": "system", "content": "You are ZCode, an interactive agent."},
    {"role": "user", "content": [
        {"type": "text", "text": "powered by DeepSeek Harness."},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,xx"}},
    ]},
    {"role": "assistant", "content": "ok",
     "reasoning_content": "You are Codex, thinking..."},
    {"role": "user", "content": "plain text"},
]
out, total = G.neutralize_messages(msgs)
check("message hits counted", total == 3, total)
check("system neutralized", out[0]["content"] == "You are ZCode , an interactive agent.")
check("text part neutralized", out[1]["content"][0]["text"] == "powered by DeepSeek Harness .")
check("image part untouched", out[1]["content"][1] is msgs[1]["content"][1])
check("reasoning neutralized", out[2]["reasoning_content"] == "You are Codex , thinking...")
check("plain message same object", out[3] is msgs[3])
clean = [{"role": "user", "content": "hello"}]
out2, n2 = G.neutralize_messages(clean)
check("no-hit returns original list", out2 is clean and n2 == 0)
check("empty/None safe", G.neutralize_messages(None) == (None, 0))

# ---- 4. 开关 ----
tmp = tempfile.mkdtemp(prefix="idguard_")
try:
    os.environ.pop("QD_IDENTITY_GUARD", None)
    check("default off", G.guard_enabled(tmp) is False)
    S.set_identity_guard(tmp, True)
    check("settings on", G.guard_enabled(tmp) is True)
    os.environ["QD_IDENTITY_GUARD"] = "0"
    check("env overrides settings off", G.guard_enabled(tmp) is False)
    os.environ["QD_IDENTITY_GUARD"] = "1"
    check("env on without dir", G.guard_enabled() is True)
    os.environ.pop("QD_IDENTITY_GUARD", None)
    S.set_identity_guard(tmp, False)
    check("settings off again", G.guard_enabled(tmp) is False)
finally:
    os.environ.pop("QD_IDENTITY_GUARD", None)
    shutil.rmtree(tmp, ignore_errors=True)

# ---- 5. build_qoder_body 集成 ----
payload = {"model": "efficient",
           "messages": [{"role": "system",
                         "content": "You are ZCode, an interactive agent."},
                        {"role": "user", "content": "reply ok"}]}
class _Acct:
    realm = "intl"
    user_type = "teams"
body_off = P.build_qoder_body(payload, _Acct(), "efficient", realm="intl")
sys_text = next(m["content"] for m in body_off["messages"] if m["role"] == "system")
check("integration off keeps signature", "You are ZCode," in sys_text, sys_text[:80])
os.environ["QD_IDENTITY_GUARD"] = "1"
try:
    body_on = P.build_qoder_body(payload, _Acct(), "efficient", realm="intl")
    sys_text2 = next(m["content"] for m in body_on["messages"] if m["role"] == "system")
    check("integration on neutralizes", "You are ZCode ," in sys_text2
          and "You are ZCode," not in sys_text2, sys_text2[:80])
    check("user message intact", any(m.get("content") == "reply ok"
                                     for m in body_on["messages"]))
finally:
    os.environ.pop("QD_IDENTITY_GUARD", None)

# ---- 6. 缓存前缀保护：默认系统提示词（自家品牌）守卫开启也零改动 ----
# 普通请求不带第三方品牌句时，出站 system 必须与官方逐字节一致，
# 否则每个请求的前缀都偏离官方、部署/切换时一次性失效全部缓存。
default_sys = next((m["content"] for m in P.BASEPROMPT.get("messages", [])
                    if m.get("role") == "system"), "")
check("baseprompt has Qoder identity", default_sys.startswith("You are Qoder,"),
      default_sys[:60])
out_default, n_default = G.neutralize_text(default_sys)
check("default system untouched (own brand)", out_default is default_sys and n_default == 0)
os.environ["QD_IDENTITY_GUARD"] = "1"
try:
    # 无客户端 system → hub 注入默认提示词；守卫开启不得改动它
    body_plain = P.build_qoder_body(
        {"model": "efficient", "messages": [{"role": "user", "content": "hi"}]},
        _Acct(), "efficient", realm="intl")
    plain_sys = next((m["content"] for m in body_plain["messages"]
                      if m.get("role") == "system"), "")
    check("guard on keeps default system byte-identical",
          plain_sys == default_sys, plain_sys[:60])
finally:
    os.environ.pop("QD_IDENTITY_GUARD", None)

# ---- 7. chat_context 的两个文本副本随同一个请求出站，必须与 messages 同源 ----
# 回归：此前 prompt 取自未处理的 flat，最新一条用户消息里的身份句会以原文漏出。
def _outbound_copies(payload):
    body = P.build_qoder_body(payload, _Acct(), "efficient", realm="intl")
    cc = body["chat_context"]
    return (cc["text"]["text"], cc["extra"]["originalContent"]["text"],
            (body.get("business") or {}).get("name"))

_raw_user = "You are Codex, a coding agent. Reply ok."
_payload_user = {"model": "efficient", "messages": [{"role": "user", "content": _raw_user}]}
ctx_off = _outbound_copies(_payload_user)
check("guard off keeps chat_context copies verbatim",
      ctx_off[0] == _raw_user and ctx_off[1] == _raw_user
      and ctx_off[2] == _raw_user[:30], ctx_off)
os.environ["QD_IDENTITY_GUARD"] = "1"
try:
    ctx_on = _outbound_copies(_payload_user)
    check("guard on neutralizes chat_context copies together with messages",
          ctx_on[0] == "You are Codex , a coding agent. Reply ok."
          and ctx_on[1] == ctx_on[0]
          and not any("You are Codex," in v for v in ctx_on), ctx_on)
    check("session name derives from the same neutralized text",
          ctx_on[2] == "You are Codex , a coding agent. Reply ok."[:30], ctx_on[2])
finally:
    os.environ.pop("QD_IDENTITY_GUARD", None)

# ---- 8. 环境变量固定开关：面板需要知道“已固定”，而不是一个保存了也不生效的开关 ----
view = P.runtime_settings_view()
check("runtime view reports an unpinned switch",
      view.get("identity_guard_env") == "" and view.get("identity_guard") is False,
      (view.get("identity_guard_env"), view.get("identity_guard")))
for raw, pinned, effective in (("1", "on", True), ("off", "off", False)):
    os.environ["QD_IDENTITY_GUARD"] = raw
    try:
        view = P.runtime_settings_view()
        check("runtime view reports pinned %s" % pinned,
              view.get("identity_guard_env") == pinned
              and view.get("identity_guard") is effective,
              (view.get("identity_guard_env"), view.get("identity_guard")))
    finally:
        os.environ.pop("QD_IDENTITY_GUARD", None)

# ---- 9. settings.json 损坏时拒绝写入，而不是用单键字典覆盖整份配置 ----
tmp_bad = tempfile.mkdtemp(prefix="qd-guard-bad-")
try:
    bad_path = S.settings_path(tmp_bad)
    broken = '{"api_keys": [{"id": "keep"}'
    with open(bad_path, "w", encoding="utf-8") as fh:
        fh.write(broken)
    try:
        S.set_identity_guard(tmp_bad, True)
    except OSError:
        with open(bad_path, encoding="utf-8") as fh:
            check("invalid settings are never replaced by the guard switch", fh.read() == broken)
    else:
        check("invalid settings are never replaced by the guard switch", False)
finally:
    shutil.rmtree(tmp_bad, ignore_errors=True)

finish()
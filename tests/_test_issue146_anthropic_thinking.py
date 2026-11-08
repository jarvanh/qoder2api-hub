"""Issue #146: Anthropic thinking block safe empty mode criteria and multi-granularity overrides."""
import os, sys, json, tempfile, shutil
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *
P, A = bootstrap()
import qoder_anthropic as AN
import qoder_settings as QS

declare_suite(__file__, ['qoder_proxy.py', 'qoder_anthropic.py', 'qoder_settings.py'])

# 1. qoder_settings 存取与有效值校验
with tempfile.TemporaryDirectory() as td:
    check("默认全局 thinking 策略为空串（跟随环境变量）", QS.anthropic_thinking(td) == "")
    check("设置 empty 生效", QS.set_anthropic_thinking(td, "empty") == "empty" and QS.anthropic_thinking(td) == "empty")
    check("设置 off 生效", QS.set_anthropic_thinking(td, "off") == "off" and QS.anthropic_thinking(td) == "off")
    check("清空回到空串", QS.set_anthropic_thinking(td, "") == "" and QS.anthropic_thinking(td) == "")
    check("非法值清空", QS.set_anthropic_thinking(td, "invalid") == "" and QS.anthropic_thinking(td) == "")

# 2. Key 维度保留与读取
with tempfile.TemporaryDirectory() as td:
    keys = [
        {"id": "k1", "key": "sk-1", "anthropic_thinking": "empty"},
        {"id": "k2", "key": "sk-2", "anthropic_thinking": "off"},
        {"id": "k3", "key": "sk-3"},
        {"id": "k4", "key": "sk-4", "anthropic_thinking": "unknown"}
    ]
    stored = QS.set_api_keys(td, keys)
    check("Key 1 保留 empty", stored[0]["anthropic_thinking"] == "empty")
    check("Key 2 保留 off", stored[1]["anthropic_thinking"] == "off")
    check("Key 3 默认为空串", stored[2]["anthropic_thinking"] == "")
    check("Key 4 非法值规整为空串", stored[3]["anthropic_thinking"] == "")
    
    re_read = QS.api_keys(td)
    check("磁盘重新读取 Key 1 保持 empty", re_read[0]["anthropic_thinking"] == "empty")
    check("磁盘重新读取 Key 2 保持 off", re_read[1]["anthropic_thinking"] == "off")
    check("磁盘重新读取 Key 3 保持空串", re_read[2]["anthropic_thinking"] == "")

# 3. 优先级决策链：Key > 面板 > 环境变量 > 默认 off
orig_env = os.environ.get("QD_ANTHROPIC_THINKING")
try:
    if "QD_ANTHROPIC_THINKING" in os.environ:
        del os.environ["QD_ANTHROPIC_THINKING"]
    
    with tempfile.TemporaryDirectory() as td:
        orig_accounts_dir = P.ACCOUNTS_DIR
        P.ACCOUNTS_DIR = td
        try:
            check("全默认时判定为 off", P.anthropic_thinking_mode() == "off")
            
            # 环境变量 empty
            os.environ["QD_ANTHROPIC_THINKING"] = "empty"
            check("环境变量 empty 判定为 empty", P.anthropic_thinking_mode() == "empty")
            
            # 面板设置 off 覆盖环境变量 empty
            QS.set_anthropic_thinking(td, "off")
            check("面板设置 off 覆盖环境变量 empty", P.anthropic_thinking_mode() == "off")
            
            # Key 维度 empty 覆盖面板设置 off
            key_empty = {"anthropic_thinking": "empty"}
            check("Key 覆盖面板 off -> empty", P.anthropic_thinking_mode(key_entry=key_empty) == "empty")
            
            # 面板设置 empty，Key 维度 off 覆盖面板
            QS.set_anthropic_thinking(td, "empty")
            key_off = {"anthropic_thinking": "off"}
            check("Key 覆盖面板 empty -> off", P.anthropic_thinking_mode(key_entry=key_off) == "off")
            
            # Key 维度未指定（空串）时跟随面板
            key_none = {"anthropic_thinking": ""}
            check("Key 未指定时跟随面板 empty", P.anthropic_thinking_mode(key_entry=key_none) == "empty")
        finally:
            P.ACCOUNTS_DIR = orig_accounts_dir
finally:
    if orig_env is not None:
        os.environ["QD_ANTHROPIC_THINKING"] = orig_env
    elif "QD_ANTHROPIC_THINKING" in os.environ:
        del os.environ["QD_ANTHROPIC_THINKING"]

# 4. chat_to_messages 覆盖
chat_obj = {
    "choices": [{
        "message": {
            "role": "assistant",
            "content": "hello",
            "reasoning_content": "my thoughts"
        }
    }]
}
res_off = AN.chat_to_messages(chat_obj, model="m", thinking_mode_override="off")
check("chat_to_messages override off 不含 thinking block",
      all(b.get("type") != "thinking" for b in res_off["content"]))

res_empty = AN.chat_to_messages(chat_obj, model="m", thinking_mode_override="empty")
th_blocks = [b for b in res_empty["content"] if b.get("type") == "thinking"]
check("chat_to_messages override empty 输出空签名 thinking block",
      len(th_blocks) == 1 and th_blocks[0]["thinking"] == "my thoughts" and th_blocks[0]["signature"] == "")

# 5. stream_anthropic_events 覆盖
def _chunk(delta):
    return b"data: " + json.dumps({"choices": [{"delta": delta}]}).encode("utf-8") + b"\n\n"

lines = [_chunk({"reasoning_content": "streaming thoughts"}), _chunk({"content": "hi"})]
frames_off = list(AN.stream_anthropic_events(lines, model="m", thinking_mode_override="off"))
all_off = "".join(f.decode("utf-8") for f in frames_off)
check("stream_anthropic_events override off 事件流不包含 thinking",
      '"type": "thinking"' not in all_off)

frames_empty = list(AN.stream_anthropic_events(lines, model="m", thinking_mode_override="empty"))
all_empty = "".join(f.decode("utf-8") for f in frames_empty)
check("stream_anthropic_events override empty 事件流包含 thinking",
      '"type": "thinking"' in all_empty)

finish()

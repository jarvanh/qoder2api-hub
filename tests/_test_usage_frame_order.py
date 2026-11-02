"""流式出口 usage 帧顺序 + cached_tokens 读取位（OpenAI Chat 格式）。

背景（kimi-k3.1-pro 实测）：上游流尾原生顺序是 [finish] -> [usage]，
但回读守卫链把 finish 帧扣到流末、usage 帧先行透传，客户端出口变成
[usage] -> [finish] -> [DONE] —— 按 OpenAI 规范「末帧携带 usage」解析的
调用方读不到输出/缓存 tokens（输入侧常由客户端自估，所以看起来
「只收到输入 tokens」）。修复：usage 垫底帧到达且无回读缓冲时先放行
被扣的 finish 帧。另：kimi 原生把缓存写在顶层 ``cached_tokens``，
四处 usage 提取器的候选链都补上这一位。
"""
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *          # noqa: F401,F403

P, A = bootstrap()
import qoder_anthropic as ANT      # noqa: E402

declare_suite(__file__, ["qoder_proxy.py", "qoder_anthropic.py"])


def chunk(delta, finish=None, **top):
    obj = {"id": "chatcmpl-t", "object": "chat.completion.chunk",
           "created": 1, "model": "auto",
           "choices": [{"index": 0, "delta": delta}]}
    if finish:
        obj["choices"][0]["finish_reason"] = finish
    obj.update(top)
    return ("data: " + json.dumps(obj, ensure_ascii=False)).encode("utf-8")


def usage_chunk(usage):
    obj = {"id": "chatcmpl-t", "object": "chat.completion.chunk",
           "created": 1, "model": "auto", "choices": [], "usage": usage}
    return ("data: " + json.dumps(obj, ensure_ascii=False)).encode("utf-8")


def run(frames, **kw):
    out = []
    for raw in P.recover_leaked_tool_calls(iter(frames), **kw):
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        data = P.strip_data_prefix(text)
        if not data or data == "[DONE]":
            continue
        try:
            out.append(json.loads(data))
        except Exception:
            pass
    return out


def kinds(chunks):
    """帧序签名：content / reasoning / FINISH(x) / USAGE / other。"""
    sig = []
    for f in chunks:
        if f.get("usage"):
            sig.append("USAGE")
            continue
        ch = (f.get("choices") or [{}])[0]
        if ch.get("finish_reason"):
            sig.append("FINISH(%s)" % ch["finish_reason"])
        elif (ch.get("delta") or {}).get("content"):
            sig.append("content")
        elif (ch.get("delta") or {}).get("reasoning_content"):
            sig.append("reasoning")
        else:
            sig.append("other")
    return sig


KIMI_USAGE = {"prompt_tokens": 2531, "completion_tokens": 192,
              "completion_tokens_details": {"reasoning_tokens": 159},
              "cached_tokens": 2304,
              "prompt_tokens_details": {"cached_tokens": 2304},
              "total_tokens": 2723}

print("[1] 纯文本流：出口顺序必须是 content -> FINISH(stop) -> USAGE")
frames = [
    chunk({"role": "assistant"}),
    chunk({"content": "你好"}),
    chunk({"content": "！"}),
    chunk({}, finish="stop"),
    usage_chunk(KIMI_USAGE),
]
out = run(frames)
sig = kinds(out)
check("finish 先于 usage（标准顺序）",
      sig == ["other", "content", "content", "FINISH(stop)", "USAGE"], sig)
u = next((f.get("usage") for f in out if f.get("usage")), None)
check("usage 三项齐全",
      u and u.get("prompt_tokens") == 2531
      and u.get("completion_tokens") == 192
      and (u.get("prompt_tokens_details") or {}).get("cached_tokens") == 2304,
      u)

print("[2] 工具调用流：finish(tool_calls) 仍先于 usage，增量不被顺序修复吞掉")
def tc_delta(args, name=None, id=None):
    entry = {"index": 0, "type": "function", "function": {"arguments": args}}
    if name:
        entry["id"] = id or "call_1"
        entry["function"]["name"] = name
    return {"tool_calls": [entry]}

frames = [
    chunk(tc_delta('{"city":"', name="get_weather")),
    chunk(tc_delta('北京')),
    chunk(tc_delta('"}')),
    chunk({}, finish="tool_calls"),
    usage_chunk(KIMI_USAGE),
]
out = run(frames, allowed_names={"get_weather"})
sig = kinds(out)
check("tool_calls -> FINISH(tool_calls) -> USAGE",
      sig == ["other", "other", "other", "FINISH(tool_calls)", "USAGE"], sig)
names = [tc.get("function", {}).get("name")
         for f in out for ch in f.get("choices") or []
         for tc in (ch.get("delta") or {}).get("tool_calls") or []]
first_tc = next((tc for f in out for ch in f.get("choices") or []
                 for tc in (ch.get("delta") or {}).get("tool_calls") or []),
                None)
check("工具名与 id 完整（#47/#124 不回归）",
      bool(first_tc) and names[0] == "get_weather"
      and first_tc.get("id") == "call_1", names)

print("[3] 泄漏回读流：hold 未清空时 finish 仍扣到流末并改写（语义不回归）")
marker = P.LEAK_MARKER
call_json = json.dumps([{"name": "get_weather",
                         "arguments": '{"city":"北京"}'}])
frames = [
    chunk({"content": marker + call_json[:5]}),
    chunk({"content": call_json[5:]}),
    chunk({}, finish="stop"),
    usage_chunk(KIMI_USAGE),
]
out = run(frames, allowed_names={"get_weather"})
sig = kinds(out)
check("回读成功：恢复块 -> FINISH(tool_calls) -> USAGE（垫底补发，不再被吞）",
      sig == ["other", "FINISH(tool_calls)", "USAGE"], sig)
u = next((f.get("usage") for f in out if f.get("usage")), None)
check("回读轮 usage 三项齐全",
      u and u.get("completion_tokens") == 192
      and (u.get("prompt_tokens_details") or {}).get("cached_tokens") == 2304,
      u)

print("[3b] DSML 回读流：缓冲期间 usage 不吞、补发在收尾帧之后（标准顺序）")
dsml = ('<|DSML|tool_calls> <|DSML|invoke name="get_weather"> '
        '<|DSML|parameter name="city">北京</|DSML|parameter> '
        '</|DSML|invoke> </|DSML|tool_calls>')
frames = [
    chunk({"content": dsml[:30]}),
    chunk({"content": dsml[30:]}),
    chunk({}, finish="stop"),
    usage_chunk(KIMI_USAGE),
]
out = run(frames, allowed_names={"get_weather"})
sig = kinds(out)
check("DSML 恢复块 -> FINISH(tool_calls) -> USAGE",
      sig == ["other", "FINISH(tool_calls)", "USAGE"], sig)

print("[4] 顶层 cached_tokens 候选位（kimi 原生字段）")
only_top = {"prompt_tokens": 100, "completion_tokens": 20,
            "cached_tokens": 64, "total_tokens": 120}
norm = P.normalize_usage_for_client(json.loads(json.dumps(only_top)))
check("normalize：顶层 cached_tokens 搬进标准位",
      (norm.get("prompt_tokens_details") or {}).get("cached_tokens") == 64
      and norm.get("cached_tokens") == 64, norm)
fields = P._extract_usage(json.loads(json.dumps(only_top)))
check("_extract_usage：记账读到顶层 cached_tokens",
      fields["cached_tokens"] == 64, fields)
ru = P._responses_usage(json.loads(json.dumps(only_top)))
check("_responses_usage：input_tokens_details 读顶层",
      (ru.get("input_tokens_details") or {}).get("cached_tokens") == 64, ru)
au = ANT.chat_usage_to_anthropic(json.loads(json.dumps(only_top)))
check("chat_usage_to_anthropic：cache_read 读顶层",
      au["cache_read_input_tokens"] == 64 and au["input_tokens"] == 36, au)

print("[5] 各家惯例字段优先级不变（DeepSeek/Qwen 形态）")
ds = {"prompt_tokens": 100, "completion_tokens": 20,
      "prompt_cache_hit_tokens": 55}
check("DeepSeek prompt_cache_hit_tokens 仍被采纳",
      P._extract_usage(dict(ds))["cached_tokens"] == 55)
qw = {"prompt_tokens": 100, "completion_tokens": 20,
      "completion_tokens_details": {"cached_tokens": 33}}
check("Qwen completion_tokens_details.cached_tokens 仍被采纳",
      P._extract_usage(dict(qw))["cached_tokens"] == 33)

print("[6] clean_chunk 出口：usage 帧保持标准位缓存字段")
usage_only = {"prompt_tokens": 10, "completion_tokens": 5,
              "cached_tokens": 7, "total_tokens": 15}
cleaned = P.clean_chunk(json.dumps(
    {"id": "chatcmpl-t", "object": "chat.completion.chunk", "created": 1,
     "model": "auto", "choices": [], "usage": usage_only}, ensure_ascii=False))
obj = json.loads(P.strip_data_prefix("data: " + cleaned))
check("clean_chunk 归一后标准位有值",
      (obj.get("usage", {}).get("prompt_tokens_details") or {})
      .get("cached_tokens") == 7, obj.get("usage"))

finish()

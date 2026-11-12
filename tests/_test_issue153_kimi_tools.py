# -*- coding: utf-8 -*-
"""#153 Kimi 原生工具历史与稳定协议提示；全部使用本地假数据，不调用上游。

    python tests/_test_issue153_kimi_tools.py
"""
import copy
import json
import os
import sys
import tempfile
from contextlib import ExitStack
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import bootstrap, check, declare_suite, finish

_TMP = tempfile.TemporaryDirectory(prefix="issue153-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP.name, "accounts")
os.environ["QD_PROXY_USAGE_DIR"] = os.path.join(_TMP.name, "usage")
P, A = bootstrap()
declare_suite(__file__, ["qoder_proxy.py", "qoder_anthropic.py"])
AN = P.qoder_anthropic
SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "TARGET path"},
        "content": {"type": "string"},
        "options": {"type": "object", "properties": {"lines": {"type": "array", "items": {"type": "integer"}}}},
    },
    "required": ["path", "content"],
    "additionalProperties": False,
}
TOOL = {"type": "function", "function": {
    "name": "Write", "description": "Write fixture content", "parameters": SCHEMA}}
ARGS = {"path": "TARGET/中文.txt", "content": 'line 1\n"quoted"\t\\path\nline 2',
        "options": {"lines": [1, 2]}}


def history(steps=10):
    messages = [{"role": "system", "content": "TARGET stable system"},
                {"role": "user", "content": "Write TARGET fixture"}]
    for i in range(steps):
        arguments = dict(ARGS, path="TARGET/step-%02d.txt" % i)
        messages.extend([
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_fixture_%02d" % i, "type": "function",
                "function": {"name": "Write", "arguments": json.dumps(arguments, ensure_ascii=False)}}]},
            {"role": "tool", "tool_call_id": "call_fixture_%02d" % i,
             "name": "Write", "content": "fixture result %02d" % i},
        ])
    return messages


def request(model="Kimi-K3.1-Pro", steps=10):
    return {"model": model, "messages": history(steps), "tools": [copy.deepcopy(TOOL)],
            "temperature": 0.37, "top_p": 0.81, "tool_choice": "auto"}


def build(payload, key="kmodel", realm="cn"):
    return P.build_qoder_body(payload, None, key, realm=realm)


def systems(body):
    return [m for m in body["messages"] if m.get("role") == "system"]


def calls(body):
    return [tc for m in body["messages"] for tc in m.get("tool_calls", [])]


def native(body, count=10):
    return (len(calls(body)) == count
            and len([m for m in body["messages"] if m.get("role") == "tool"]) == count)


def protocol_count(body):
    return sum(str(m.get("content") or "").count(P.KIMI_TOOL_PROTOCOL_PROMPT)
               for m in body["messages"])


def frame(delta, finish_reason=None):
    return {"id": "chatcmpl-fixture", "model": "Kimi-K3.1-Pro",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}


def sse(obj):
    return ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8")


def leaked_frames(arguments, size):
    text = P.LEAK_MARKER + json.dumps([{"name": "Write", "arguments": arguments}], ensure_ascii=False)
    return [sse(frame({"content": text[i:i + size]})) for i in range(0, len(text), size)] + [sse(frame({}, "stop")), b"data: [DONE]\n\n"]


def envelope_frames(arguments):
    text = P.LEAK_MARKER + json.dumps([{"name": "Write", "arguments": arguments}], ensure_ascii=False)
    return [sse({"statusCodeValue": 200, "body": json.dumps(frame({"content": text}, "stop"), ensure_ascii=False)}),
            sse({"statusCodeValue": 200, "body": "[DONE]"})]


def decode_chunks(raws):
    result = []
    for raw in raws:
        data = P.strip_data_prefix(raw.decode("utf-8"))
        if data and data != "[DONE]":
            result.append(json.loads(data))
    return result


with ExitStack() as stack:
    stack.enter_context(patch.dict(os.environ, {
        "QD_STRUCTURED_TOOL_HISTORY": "auto", "QD_STRUCTURED_TOOL_HISTORY_REALMS": "cn,intl",
        "QD_TOOL_RESULT_SCOPE": "all", "QD_TOOL_RESULT_MAX_CHARS": "0"}))
    # 回归套件即便未来误触网络也必须失败，不得读取真实凭证或连接服务。
    stack.enter_context(patch("urllib.request.urlopen", side_effect=AssertionError("offline fixture: network forbidden")))
    stack.enter_context(patch("socket.create_connection", side_effect=AssertionError("offline fixture: network forbidden")))
    stack.enter_context(patch.object(P, "catalog_meta_for", return_value={"display_name": "TARGET", "max_input_tokens": 180000}))
    stack.enter_context(patch.object(P, "model_context_override", return_value=None))

    print("[#153] 模型识别与结构化自动模式")
    for model, key in [("", "kmodel"), ("", "kmodel_latest"), ("KMODEL", ""),
                       ("", "kimi-k2"), ("TARGET alias", "kimi-k3"),
                       ("Kimi-K3.1-Pro", "alias"), ("kimi-k2", "alias"),
                       ("vendor/Kimi-K3", "alias"), ("Display Kimi K3", "alias"),
                       ("  Kimi-K3.1-Pro  ", " KMODEL_LATEST ")]:
        check("Kimi alias %r/%r" % (model, key), P.is_kimi_model(model, key))
        body = build(request(model), key)
        check("Kimi alias 实际 body %r/%r" % (model, key), native(body) and protocol_count(body) == 1)
    for model, key in [("Other", "kmodel_unrelated"), ("notkimi", "other"),
                       ("skimix", "other"), ("Qwen", "qwen"), ("", "")]:
        check("非 Kimi 不误匹配 %r/%r" % (model, key), not P.is_kimi_model(model, key))

    for realm in ("cn", "intl"):
        payload = request()
        before = copy.deepcopy(payload)
        base_before = copy.deepcopy(P.BASEPROMPT)
        body = build(payload, realm=realm)
        check(realm + "：十步工具历史保持原生调用与结果", native(body), body["messages"])
        check(realm + "：assistant 工具消息 content 始终为空串而非 null",
              all(m.get("content") == "" for m in body["messages"] if m.get("tool_calls")))
        check(realm + "：十步调用 ID 与参数值完整",
              [tc["id"] for tc in calls(body)] == ["call_fixture_%02d" % i for i in range(10)]
              and all(json.loads(tc["function"]["arguments"])["content"] == ARGS["content"] for tc in calls(body)))
        check(realm + "：原生历史不含文本信封 marker", P.LEAK_MARKER not in json.dumps(body["messages"], ensure_ascii=False))
        check(realm + "：保留原 system 并追加一次固定协议",
              len(systems(body)) == 1 and systems(body)[0]["content"] == "TARGET stable system\n\n" + P.KIMI_TOOL_PROTOCOL_PROMPT)
        check(realm + "：schema 不变、不添加 strict", body["tools"] == before["tools"] and "strict" not in body["tools"][0]["function"])
        check(realm + "：客户端输入及 BASEPROMPT 不变", payload == before and P.BASEPROMPT == base_before)
        check(realm + "：不强制调用、不更改采样参数",
              payload["tool_choice"] == "auto" and payload["temperature"] == 0.37
              and body.get("tool_choice") not in ("required", "Write")
              and "temperature" not in body.get("parameters", {}))

    # 对照同一输入的 Kimi / 非 Kimi body，避免把既有参数策略当成新特性。
    kimi_body = build(request())
    other_body = build(request("Sonus"), "sonus")
    check("Kimi 不改变 body.parameters 与非 Kimi 相比的采样参数", kimi_body["parameters"] == other_body["parameters"])
    check("Kimi 不强制改变出站工具选择", kimi_body.get("tool_choice") == other_body.get("tool_choice"))

    print("[#153] 回退守卫、非 Kimi 隔离")
    for mode in ("off", "on"):
        with patch.dict(os.environ, {"QD_STRUCTURED_TOOL_HISTORY": mode}):
            body = build(request())
            check("模式 %s 保留既有历史选择" % mode, native(body) if mode == "on" else not calls(body))
            check("模式 %s 不取消 Kimi 生成端协议约束" % mode, protocol_count(body) == 1)
    for missing in ("assistant", "tool"):
        payload = request()
        if missing == "assistant":
            payload["messages"][2]["tool_calls"][0].pop("id")
        else:
            payload["messages"][3].pop("tool_call_id")
        for mode in ("auto", "on"):
            with patch.dict(os.environ, {"QD_STRUCTURED_TOOL_HISTORY": mode}):
                body = build(payload)
                check("缺 %s id / %s 不发送畸形原生历史" % (missing, mode), not calls(body)
                      and not any(m.get("role") == "tool" for m in body["messages"]))
    with patch.dict(os.environ, {"QD_STRUCTURED_TOOL_HISTORY_REALMS": "cn"}):
        check("realm 配置排除 intl 时恢复文本历史", not native(build(request(), realm="intl")))
    payload = request()
    payload["_qd_structured"] = False
    check("显式桥接决定 false 不被生成端覆盖", not calls(build(payload)))
    for model, key, expected in [("Qwen", "qwen", True), ("GLM", "gm5", True),
                                 ("DeepSeek", "dfmodel", True), ("Sonus", "sonus", False)]:
        body = build(request(model), key)
        check("非 Kimi %s 保持历史行为" % model, native(body) == expected)
        check("非 Kimi %s 没有协议追加" % model, protocol_count(body) == 0 and systems(body)[0]["content"] == "TARGET stable system")

    print("[#153] 工具归一化、system 与缓存稳定性")
    for choice in ("none", {"type": "none"}):
        payload = request()
        payload["tool_choice"] = choice
        body = build(payload)
        check("tool_choice=%r 不追加协议" % choice, protocol_count(body) == 0)
        check("tool_choice=%r 不变更声明 schema" % choice, body["tools"] == payload["tools"])
    for tools in (None, [], [{"type": "web_search"}]):
        payload = request()
        payload["tools"] = tools
        body = build(payload)
        check("归一化后无工具 %r 不追加协议" % tools, not body["tools"] and protocol_count(body) == 0)
    payload = request()
    payload["tools"] = [{"type": "function", "name": "Write", "description": "flat fixture", "parameters": copy.deepcopy(SCHEMA), "strict": False}]
    before = copy.deepcopy(payload)
    body = build(payload)
    check("平铺工具先归一化、后追加协议", body["tools"][0]["function"]["name"] == "Write" and protocol_count(body) == 1)
    check("显式 strict=false 与原 schema 保持、不改输入", body["tools"][0]["function"]["strict"] is False
          and body["tools"][0]["function"]["parameters"] == SCHEMA and payload == before)
    a = build(request(steps=9))
    b = build(request(steps=10))
    encoded = lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    check("九步到十步历史的共同消息字节前缀稳定", encoded(a["messages"]) == encoded(b["messages"][:len(a["messages"])]))
    check("不同请求协议 system 字节完全相同", encoded(systems(a)) == encoded(systems(b)))
    payload = request()
    payload["messages"][0]["content"] += "\n\n" + P.KIMI_TOOL_PROTOCOL_PROMPT
    check("客户端已经携带协议时不重复追加", protocol_count(build(payload)) == 1)
    template = copy.deepcopy(P.BASEPROMPT)
    template["messages"] = []
    payload = request()
    payload["messages"] = [{"role": "user", "content": "TARGET"}]
    with patch.object(P, "BASEPROMPT", template), patch.object(P, "SYSTEM_PROMPT", ""):
        body = build(payload)
    check("无模板与客户端 system 时在首位插入协议", body["messages"][0] == {"role": "system", "content": P.KIMI_TOOL_PROTOCOL_PROMPT})
    prompt = P.KIMI_TOOL_PROTOCOL_PROMPT.lower()
    check("协议要求原生调用且不强制调用", "native tool_calls" in prompt and "does not require calling" in prompt)
    check("协议要求单一合法 JSON 对象及转义", "one complete valid json object" in prompt and "escape" in prompt
          and "backslashes" in prompt and "newlines" in prompt)
    check("协议保留 schema 且不得伪造执行", "declared parameters" in prompt and "value types" in prompt
          and "do not claim execution until a tool result arrives" in prompt)

    print("[#153] 三协议桥接至同一真实 build 函数")
    responses_input = [{"role": "user", "content": "TARGET fixture"}]
    anthropic_messages = [{"role": "user", "content": "TARGET fixture"}]
    for i in range(10):
        cid = "call_fixture_%02d" % i
        responses_input.extend([
            {"type": "function_call", "call_id": cid, "name": "Write", "arguments": json.dumps(ARGS, ensure_ascii=False)},
            {"type": "function_call_output", "call_id": cid, "output": "fixture result %02d" % i}])
        anthropic_messages.extend([
            {"role": "assistant", "content": [{"type": "tool_use", "id": cid, "name": "Write", "input": copy.deepcopy(ARGS)}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": cid, "content": "fixture result %02d" % i}]}])
    response_req = {"model": "Kimi-K3.1-Pro", "instructions": "TARGET stable system", "input": responses_input,
                    "tools": [{"type": "function", "name": "Write", "description": "fixture", "parameters": copy.deepcopy(SCHEMA)}]}
    anthropic_req = {"model": "Kimi-K3.1-Pro", "system": "TARGET stable system", "max_tokens": 128,
                     "messages": anthropic_messages,
                     "tools": [{"name": "Write", "description": "fixture", "input_schema": copy.deepcopy(SCHEMA)}]}
    for name, raw, convert in [("Responses", response_req, P.responses_to_chat),
                               ("Anthropic", anthropic_req, AN.messages_to_chat)]:
        original = copy.deepcopy(raw)
        chat = convert(raw)
        before = copy.deepcopy(chat)
        body = build(chat)
        check(name + " 十步桥接保持原生调用/结果", native(body))
        check(name + " 协议只进首个 system 一次", protocol_count(body) == 1
              and systems(body)[0]["content"].startswith("TARGET stable system"))
        check(name + " schema 与输入保持不变", body["tools"][0]["function"]["parameters"] == SCHEMA and raw == original and chat == before)
        check(name + " 复杂字符串经桥接仍可解析为原值", all(json.loads(c["function"]["arguments"]) == ARGS for c in calls(body)))
        raw_none = copy.deepcopy(raw)
        raw_none["tool_choice"] = {"type": "none"} if name == "Anthropic" else "none"
        check(name + " none 不追加协议", protocol_count(build(convert(raw_none))) == 0)
        raw_empty = copy.deepcopy(raw)
        raw_empty.pop("tools")
        check(name + " 无工具不追加协议", protocol_count(build(convert(raw_empty))) == 0)
        for mode in ("off", "auto"):
            bridged = convert(copy.deepcopy(raw))
            if mode == "auto":
                next(m for m in bridged["messages"] if m.get("role") == "tool").pop("tool_call_id")
            with patch.dict(os.environ, {"QD_STRUCTURED_TOOL_HISTORY": mode}):
                check(name + " fallback " + mode + " 不发结构化历史", not calls(build(bridged)))

    print("[#153] 非法参数仍 fail-closed、不自动收费重试")
    dirty = '{"path":"TARGET-private.txt","content":'
    for size in (1, 7, 9999):
        emitted = []
        raised = None
        try:
            for out in P.recover_leaked_tool_calls(iter(leaked_frames(dirty, size)), {"Write"}):
                emitted.append(out)
        except P.InvalidToolArguments as exc:
            raised = exc
        chunks = decode_chunks(emitted)
        finish_reasons = [c.get("finish_reason") for obj in chunks for c in obj.get("choices", [])]
        check("非法 JSON 流式 size=%d 明确失败" % size, raised is not None)
        check("非法 JSON 流式 size=%d 不释放调用或正常完成" % size,
              not any((c.get("delta") or {}).get("tool_calls") for obj in chunks for c in obj.get("choices", []))
              and not any(f in ("stop", "tool_calls") for f in finish_reasons))
        check("非法 JSON 流式 size=%d 不泄漏 marker 或参数" % size,
              P.LEAK_MARKER.encode() not in b"".join(emitted) and b"TARGET-private" not in b"".join(emitted))
    raised = None
    try:
        P.aggregate_stream(iter(envelope_frames(dirty)), "Kimi-K3.1-Pro", holder={}, allowed_names={"Write"})
    except P.InvalidToolArguments as exc:
        raised = exc
    check("非流式聚合非法 JSON 仍抛 InvalidToolArguments", raised is not None)
    check("错误信息不包含原始参数", raised is not None and "TARGET-private" not in str(raised))
    check("非法参数禁止任何次数自动重试", all(P.should_retry_envelope(P.InvalidToolArguments(), sent, attempt) is False
          for sent, attempt in [(0, 0), (0, 1), (1024, 0), (0, 99)]))
    # 原生 tool_calls 的坏参数同样不得有成功收尾（参数增量允许流式先发）。
    native_bad = [sse(frame({"tool_calls": [{"index": 0, "id": "call_fixture_bad", "type": "function",
                    "function": {"name": "Write", "arguments": dirty}}]})),
                  sse(frame({}, "tool_calls")), b"data: [DONE]\n\n"]
    emitted, native_error = [], None
    try:
        for out in P.recover_leaked_tool_calls(iter(native_bad), {"Write"}):
            emitted.append(out)
    except P.InvalidToolArguments as exc:
        native_error = exc
    native_fins = [c.get("finish_reason") for obj in decode_chunks(emitted) for c in obj.get("choices", [])]
    check("原生坏 JSON 不因已有 tool_calls 而正常完成", native_error is not None
          and not any(f in ("stop", "tool_calls") for f in native_fins))
    native_envelope = [sse({"statusCodeValue": 200, "body": P.strip_data_prefix(raw.decode()).strip()}) for raw in native_bad]
    native_aggregate_error = None
    try:
        P.aggregate_stream(iter(native_envelope), "Kimi-K3.1-Pro", holder={}, allowed_names={"Write"})
    except P.InvalidToolArguments as exc:
        native_aggregate_error = exc
    check("原生坏 JSON 聚合仍 fail-closed", native_aggregate_error is not None)
    good = json.dumps(ARGS, ensure_ascii=False)
    recovered = decode_chunks(list(P.recover_leaked_tool_calls(iter(leaked_frames(good, 7)), {"Write"})))
    recovered_calls = [tc for obj in recovered for c in obj.get("choices", []) for tc in (c.get("delta") or {}).get("tool_calls", [])]
    check("对照：合法复杂字符串工具调用仍正常恢复", recovered_calls
          and json.loads(recovered_calls[0]["function"]["arguments"]) == ARGS)
    aggregate = P.aggregate_stream(iter(envelope_frames(good)), "Kimi-K3.1-Pro", holder={}, allowed_names={"Write"})
    check("对照：合法参数非流式仍返回 tool_calls", aggregate["choices"][0]["finish_reason"] == "tool_calls"
          and json.loads(aggregate["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]) == ARGS)

_TMP.cleanup()
finish()

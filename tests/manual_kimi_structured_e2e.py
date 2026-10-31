# -*- coding: utf-8 -*-
"""Kimi 结构化工具历史透传的端到端验证（手动脚本；不进 run_all，发真实上游流量）。

背景：Kimi 反复把 `[assistant 请求调用工具] [...]` 文本回声复述成坏 JSON（#37）。
治本手段是 QD_STRUCTURED_TOOL_HISTORY（结构化 tool_calls/tool 历史，不发文本化
marker）——但 auto 白名单一直把 kimi 排除在外，因为「待补端到端验证」。本脚本
就是那个验证：给真实 Kimi 发一条带结构化历史的会话，看上游是否正常消化。

跑法（三选一，按账号配额情况自选）：
  1) 主网关（auto 白名单不含 kimi，等于没开——仅对照用）:
       python tests/manual_kimi_structured_e2e.py http://127.0.0.1:8790 qd-<key> Kimi-K3.1-Pro
  2) 测试容器（强制结构化，推荐——不影响主网关其它流量）:
       docker run -d --name q2api-kimi-test --rm \
         -e QD_STRUCTURED_TOOL_HISTORY=on \
         -v /dropbox/self-hosted/qoder2api/accounts:/app/accounts \
         -v /dropbox/self-hosted/qoder2api/usage:/app/usage \
         ghcr.io/jarvanh/qoder2api-hub:<版本>
       docker inspect q2api-kimi-test --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'
       python tests/manual_kimi_structured_e2e.py http://<容器IP>:8790 qd-<key> Kimi-K3.1-Pro
  3) 主网关临时开全局 on（影响所有模型，验证完记得撤）:
       docker ... -e QD_STRUCTURED_TOOL_HISTORY=on ...

判定（三组请求全部满足才算「可用」）：
  - HTTP 200 且 finish 正常；
  - 回复正文里**不出现**工具协议 marker（LEAK_MARKER / 尖括号变体）；
  - 回复消费了 tool 结果（第一组：报出结果时间；第二组：复述文件内容）。
全部通过 -> 把 "kimi" 加进 qoder_proxy.py 的 STRUCTURED_TOOL_HISTORY_WHITELIST，
并同步更新 _test_issue60_followup.py 的「kimi 仍排除」断言 + CHANGELOG。
跑失败（400/403 上游拒格式）-> 保持白名单现状，把 upstream 详情记进 issue。
"""
import json
import sys
import urllib.error
import urllib.request

TOOL_CALL_ID = "call_kimi_e2e_1"
TIME_RESULT = "2026-11-01T00:00:00+08:00"
FILE_RESULT = "浅草才能没马蹄"


def build_request():
    return {
        "model": "Kimi-K3.1-Pro",
        "stream": False,
        "max_tokens": 400,
        "messages": [
            {"role": "system",
             "content": "You are a terse assistant. Answer with the exact "
                        "minimal output requested, nothing else."},
            {"role": "user", "content": "Call the get_time tool now."},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": TOOL_CALL_ID, "type": "function",
                 "function": {"name": "get_time",
                              "arguments": "{\"tz\": \"Asia/Shanghai\"}"}}]},
            {"role": "tool", "tool_call_id": TOOL_CALL_ID,
             "content": TIME_RESULT},
        ],
        "tools": [
            {"type": "function", "function": {
                "name": "get_time", "description": "Get current time",
                "parameters": {"type": "object",
                               "properties": {"tz": {"type": "string"}},
                               "required": ["tz"]}}},
            {"type": "function", "function": {
                "name": "read_file", "description": "Read a text file",
                "parameters": {"type": "object",
                               "properties": {"path": {"type": "string"}},
                               "required": ["path"]}}},
        ],
    }


def post(base, key, payload, timeout=120):
    req = urllib.request.Request(
        base.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8", "replace"))


def check(name, payload, want):
    ok, blob = post(BASE, KEY, payload)
    if ok != 200:
        print("  [%s] HTTP %s: %s" % (name, ok, json.dumps(blob, ensure_ascii=False)[:220]))
        return None
    msg = (blob.get("choices") or [{}])[0].get("message") or {}
    text = msg.get("content") or ""
    fin = ((blob.get("choices") or [{}])[0].get("finish_reason"))
    markers = ("[assistant", "<assistant", "请求调用工具")
    leaked = any(m in text for m in markers)
    consumed = want in text
    print("  [%s] finish=%s leaked_marker=%s consumed=%s text=%r"
          % (name, fin, leaked, consumed, text[:120]))
    return (not leaked) and consumed and fin in ("stop", "tool_calls")


def main():
    global BASE, KEY
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    BASE, KEY = sys.argv[1], sys.argv[2]
    results = []
    results.append(check("T1 时间结果", build_request(), TIME_RESULT.split("T")[1][:5]))
    p2 = build_request()
    p2["messages"] = [
        {"role": "system", "content": p2["messages"][0]["content"]},
        {"role": "user", "content": "Read /tmp/poem.txt then call get_time twice."},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_kimi_e2e_2a", "type": "function",
             "function": {"name": "read_file", "arguments": "{\"path\": \"/tmp/poem.txt\"}"}},
            {"id": "call_kimi_e2e_2b", "type": "function",
             "function": {"name": "get_time", "arguments": "{\"tz\": \"Asia/Shanghai\"}"}}]},
        {"role": "tool", "tool_call_id": "call_kimi_e2e_2a", "content": FILE_RESULT},
        {"role": "tool", "tool_call_id": "call_kimi_e2e_2b", "content": TIME_RESULT},
    ]
    results.append(check("T2 多工具", p2, FILE_RESULT))
    results.append(check("T3 复跑稳定性", build_request(), TIME_RESULT.split("T")[1][:5]))
    done = [r for r in results if r is not None]
    print()
    if results and all(results):
        print("VERDICT: 可用 —— 可把 kimi 加入 STRUCTURED_TOOL_HISTORY_WHITELIST")
        sys.exit(0)
    if done and not any(results):
        print("VERDICT: 上游拒绝结构化历史 —— 白名单维持现状，留 upstream 详情")
        sys.exit(1)
    print("VERDICT: 结果不一致（网络/配额干扰）—— 复跑后再判")
    sys.exit(3)


if __name__ == "__main__":
    main()

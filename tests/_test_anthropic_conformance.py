"""Offline Anthropic thinking conformance: handler, blocks and usage."""
import io
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import bootstrap, check, declare_suite, finish

P, _ = bootstrap()
import qoder_anthropic as AN

declare_suite(__file__, ["qoder_proxy.py", "qoder_anthropic.py", "qoder_anthropic_web.py"])
ENV = "QD_ANTHROPIC_THINKING"
LEVELS = ["low", "high", "max"]
META = {"thinking_config": {"enabled": {"efforts": {e: {} for e in LEVELS}}}}
ENABLED = {"thinking": {"type": "enabled", "budget_tokens": 2048}}
DISABLED = {"thinking": {"type": " Disabled "}, "output_config": {"effort": "high"}}
U = {"prompt_tokens": 10, "completion_tokens": 6,
     "completion_tokens_details": {"reasoning_tokens": 2}}
DETAILS = {"thinking_tokens": 2, "reasoning_tokens": 2}


def chunk(delta=None, finish_reason=None, **extra):
    return {"choices": [{"index": 0, "delta": delta or {},
                         "finish_reason": finish_reason}] if delta or finish_reason else [], **extra}


def data(chunk):
    return ("data: " + json.dumps(chunk) + "\n\n").encode()


def events(frames):
    return [json.loads(line[6:]) for frame in frames for line in frame.splitlines()
            if line.startswith(b"data: ")]


def block_events(items):
    return [(event["type"], event["index"],
             (event.get("content_block") or event.get("delta") or {}).get("type"))
            for event in items if event["type"].startswith("content_block_")]


def well_formed(items):
    current, next_index = None, 0
    for event, index, _ in block_events(items):
        if event == "content_block_start":
            if current is not None or index != next_index:
                return False
            current, next_index = index, next_index + 1
        elif index != current:
            return False
        elif event == "content_block_stop":
            current = None
    return current is None


def request(hosted, stream, params, *, key=None, panel="", env=None, chunks=None):
    frames = []
    fake = SimpleNamespace(headers={}, key_entry=key,
                           _request_realm=lambda: None,
                           _key_id_for_usage=lambda: "fixture",
                           _apply_context_header=lambda req: None,
                           _model_precheck=lambda *_: None,
                           _json=lambda status, body: (status, body),
                           _error=lambda status, message, *_: (status, message),
                           _sse_begin=lambda: None, _sse_write=frames.append,
                           _sse_end=lambda: None)
    fake._handle_web_tools = P.Handler._handle_web_tools.__get__(fake)
    payload = dict(model="fixture", max_tokens=100, stream=stream,
                   messages=[{"role": "user", "content": "hello"}], **params)
    if hosted:
        payload.update(tools=[{"type": "web_search_20250305", "name": "web_search"}],
                       tool_choice={"type": "none"})
    if chunks is None:
        chunks = [chunk({"reasoning_content": "reason"}),
                  chunk({"content": "answer"}, "stop"), chunk(usage=U),
                  chunk(usage={}), chunk(usage=["invalid"])]
    upstream = io.BytesIO(b"".join(data({"body": json.dumps(c)}) for c in chunks)
                          + data({"body": "[DONE]"}))
    with patch.dict(os.environ), patch.object(P.qoder_settings, "anthropic_thinking", return_value=panel), \
            patch.object(P, "resolve_model_key", return_value="fixture"), \
            patch.object(P, "catalog_meta_for", return_value=META), \
            patch.object(P, "sse_with_heartbeat", side_effect=lambda lines, _: lines), \
            patch.object(P, "open_upstream", return_value=(upstream, SimpleNamespace(uid="fixture"), None)) as sent, \
            patch.object(P, "record_usage"), patch.object(P, "record_error"):
        os.environ.pop(ENV, None)
        if env is not None:
            os.environ[ENV] = env
        out = P.Handler._handle_anthropic(fake, "/v1/messages", payload)
    if stream:
        assert out is None, out
        out = events(frames)
    else:
        assert out[0] == 200, out
        out = out[1]
    return out, sent.call_args.args[0].get("reasoning_effort")


for label, params, want, effort in (
    ("default", {}, False, None),
    ("adaptive", {"thinking": {"type": "adaptive"}}, True, None),
    ("enabled budget", ENABLED, True, "low"),
    ("explicit disabled beats effort", DISABLED, False, "none"),
    ("explicit none beats lower precedence", {"thinking": {"effort": " none "},
      "output_config": {"effort": "high"}}, False, "none"),
    ("output effort alone", {"output_config": {"effort": "high"}}, True, "high"),
    ("unknown output effort", {"output_config": {"effort": "bogus"}}, False, None),
    ("thinking effort wins", {"thinking": {"effort": "low", "budget_tokens": 25000},
      "output_config": {"effort": "high"}}, True, "low"),
    ("level alias fallback", {"thinking": {"effort": "typo", "level": "high"}}, True, "high"),
    ("invalid budget and effort", {"thinking": {"budget_tokens": True},
      "output_config": {"effort": []}}, False, None),
):
    actual = AN.wants_thinking(params), AN.effort_from_anthropic(params, supported=LEVELS)
    check(label, actual == (want, effort), actual)
check("explicit none survives an empty effort table",
      AN.effort_from_anthropic(DISABLED, supported=[]) == "none")

for hosted in (False, True):
    for stream in (False, True):
        for label, params, expect, effort, overrides in (
            ("request-driven", ENABLED, True, "low", {}),
            ("default", {}, False, None, {}),
            ("disabled", DISABLED, False, "none", {}),
            ("env off", ENABLED, False, "low", {"env": "off"}),
            ("panel off beats env empty", ENABLED, False, "low", {"panel": "off", "env": "empty"}),
            ("key off beats panel empty", ENABLED, False, "low", {
                "key": {"anthropic_thinking": "off"}, "panel": "empty"}),
            ("key empty beats panel off", {}, True, None, {
                "key": {"anthropic_thinking": "empty"}, "panel": "off"}),
        ):
            output, sent = request(hosted, stream, params, **overrides)
            if stream:
                content = [e["content_block"] for e in output if e["type"] == "content_block_start"]
                usage = output[-2]["usage"]
                ok = well_formed(output) and output[-1]["type"] == "message_stop"
                if expect:
                    ok = ok and block_events(output)[:5] == [
                        ("content_block_start", 0, "thinking"),
                        ("content_block_delta", 0, "thinking_delta"),
                        ("content_block_delta", 0, "signature_delta"),
                        ("content_block_stop", 0, None),
                        ("content_block_start", 1, "text")]
                    ok = ok and output[3]["delta"]["signature"] == ""
            else:
                content, usage = output["content"], output["usage"]
                ok = not expect or content[0]["thinking"] == "reason"
            check("hosted=%s stream=%s %s" % (hosted, stream, label),
                  ok and [b["type"] for b in content] == (["thinking", "text"] if expect else ["text"])
                  and (not expect or content[0]["signature"] == "") and sent == effort
                  and (usage["input_tokens"], usage["output_tokens"], usage["output_tokens_details"])
                  == (10, 6, DETAILS), (content, usage, sent))


def direct(*chunks):
    return events(AN.stream_anthropic_events(map(data, chunks), thinking_mode_override="empty"))


def reasons(output):
    return [e["delta"]["thinking"] for e in output if e.get("delta", {}).get("type") == "thinking_delta"]


late = direct(chunk({"content": "visible"}), chunk({"reasoning_content": "late"}, "stop"))
check("late reasoning never starts a block after text", well_formed(late) and not reasons(late), late)
only = direct(chunk({"reasoning_content": "one"}), chunk({"reasoning_content": "two"}, "stop"))
check("thinking-only closes with an empty signature before message_delta",
      well_formed(only) and reasons(only) == ["one", "two"] and block_events(only)[-2:] == [
          ("content_block_delta", 0, "signature_delta"), ("content_block_stop", 0, None)], only)
call = {"index": 0, "id": "tool-0", "function": {"name": "test", "arguments": "{}"}}
parallel = {"index": 1, "id": "tool-1", "function": {"name": "other", "arguments": "{}"}}
tooled = direct(chunk({"reasoning_content": "early"}), chunk({"tool_calls": [call, parallel]}),
                chunk({"reasoning_content": "late"}), chunk({"content": "after tools"}, "tool_calls"),
                chunk(usage=U), chunk(usage={}))
check("tool streaming closes thinking first and never overlaps blocks", well_formed(tooled)
      and reasons(tooled) == ["early"] and [e[2] for e in block_events(tooled) if e[0].endswith("start")]
      == ["thinking", "tool_use", "tool_use", "text"]
      and tooled[-2]["usage"]["output_tokens_details"] == DETAILS, tooled)
zeros = direct(chunk(usage=U), chunk(usage={"prompt_tokens": 0, "completion_tokens": 0}), chunk(usage={}))
check("valid zero-valued usage is not an empty frame",
      zeros[-2]["usage"] == AN.chat_usage_to_anthropic({}), zeros)
finish()

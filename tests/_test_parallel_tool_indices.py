import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *

P, A = bootstrap()
declare_suite(__file__, ["qoder_proxy.py", "qoder_anthropic.py"])
import qoder_anthropic as AN


def frame(calls=None, finish=None):
    payload = {"id": "chatcmpl-test", "model": "ultimate", "choices": [{
        "index": 0, "delta": {"tool_calls": calls} if calls else {},
        "finish_reason": finish}]}
    return ("data: " + json.dumps(payload) + "\n\n").encode()


def call(call_id="", name=None, arguments="", index=0):
    function = {"arguments": arguments}
    if name is not None:
        function["name"] = name
    result = {"id": call_id, "type": "function", "function": function}
    if index is not None:
        result["index"] = index
    return result


def stream(count, mode, same_tool):
    frames = []
    for position in range(count):
        index = position if mode == "valid" else None if mode == "missing" else 0
        name = "echo" if same_tool else "echo_" + str(position)
        frames.append(frame([call("call_" + str(position), name, index=index)]))
        arguments = json.dumps({"text": str(position)})
        frames.append(frame([call(arguments=arguments[:5], index=index)]))
        frames.append(frame([call(arguments=arguments[5:], index=index)]))
    return frames + [frame(finish="tool_calls"), b"data: [DONE]\n\n"]


def events_of(frames):
    events = []
    for raw in AN.stream_anthropic_events(P.recover_leaked_tool_calls(iter(frames)),
                                        model="ultimate", holder={}):
        for part in raw.decode().split("\n\n"):
            if "data: " in part:
                events.append(json.loads(part.split("data: ", 1)[1]))
    return events


for count in (1, 2, 3, 4):
    for mode in ("valid", "zero", "missing"):
        for same_tool in (False, True):
            label = "%s calls %s indices same_tool=%s" % (count, mode, same_tool)
            events = events_of(stream(count, mode, same_tool))
            starts = [event for event in events if event["type"] == "content_block_start"]
            check(label + " count", len(starts) == count, starts)
            check(label + " names", [event["content_block"]["name"] for event in starts] == [
                "echo" if same_tool else "echo_" + str(position) for position in range(count)])
            check(label + " IDs", [event["content_block"]["id"] for event in starts] == [
                "call_" + str(position) for position in range(count)])
            for start in starts:
                arguments = "".join(event["delta"].get("partial_json", "") for event in events
                                    if event["type"] == "content_block_delta" and event["index"] == start["index"])
                check(label + " arguments " + str(start["index"]),
                      json.loads(arguments) == {"text": str(start["index"])}, arguments)
            endings = [event for event in events if event["type"] == "message_delta"]
            check(label + " stop", endings[-1]["delta"]["stop_reason"] == "tool_use", endings)

for label, frames, kind in [
    ("missing name", [frame([call("orphan", arguments='{"text":"A"}')]), frame(finish="tool_calls")], "missing_tool_name"),
    ("truncated arguments", [frame([call("call_a", "echo", '{"text":')]), frame(finish="tool_calls")], "truncated_arguments"),
    ("second nameless call", [frame([call("call_a", "echo", '{}')]), frame([call("call_b", arguments='{}')]),
                             frame(finish="tool_calls")], "missing_tool_name"),
]:
    failure = None
    try:
        events_of(frames)
    except P.EmptyToolEcho as error:
        failure = error.detail
    check(label + " fails closed", failure is not None and kind in failure, failure)

late = [frame([call("call_a", arguments='{"text":')]),
        frame([call("call_a", "echo", '"A"}')]), frame(finish="tool_calls")]
check("late name works", len([event for event in events_of(late)
                              if event["type"] == "content_block_start"]) == 1)

late_id = [frame([call(arguments='{"text":')]),
           frame([call("call_a", "echo", '"A"}')]), frame(finish="tool_calls")]
check("late ID preserves arguments", len([event for event in events_of(late_id)
                                         if event["type"] == "content_block_start"]) == 1)

for label, frames in [
    ("same frame missing indices", [frame([call("call_a", "echo_a", '{}', None),
                                          call("call_b", "echo_b", '{}', None)]), frame(finish="tool_calls")]),
    ("interleaved valid indices", [frame([call("call_a", "echo_a", '{"text":', 0)]),
                                   frame([call("call_b", "echo_b", '{"text":', 1)]),
                                   frame([call(arguments='"A"}', index=0)]),
                                   frame([call(arguments='"B"}', index=1)]), frame(finish="tool_calls")]),
    ("interleaved duplicate index explicit IDs", [frame([call("call_a", "echo_a", '{"text":')]),
                                                frame([call("call_b", "echo_b", '{"text":')]),
                                                frame([call("call_a", arguments='"A"}')]),
                                                frame([call("call_b", arguments='"B"}')]), frame(finish="tool_calls")]),
]:
    check(label, len([event for event in events_of(frames) if event["type"] == "content_block_start"]) == 2)

for raw in (frame(), b"data: [DONE]\n\n", b": ping\n\n", b"data: invalid\n\n",
            frame([call("call_a", "echo", "{}", 7)])):
    check("unchanged valid frame", list(P._sse_normalize_tool_call_indices([raw])) == [raw])

envelopes = [("data: " + json.dumps({"statusCodeValue": 200, "body": P.strip_data_prefix(raw.decode())}) + "\n\n").encode()
             for raw in stream(4, "zero", False)]
completion = P.aggregate_stream(envelopes, model="ultimate")
calls = completion["choices"][0]["message"]["tool_calls"]
check("nonstream duplicate indices preserve all calls", len(calls) == 4, calls)
check("nonstream names preserved", [item["function"]["name"] for item in calls] == ["echo_" + str(position) for position in range(4)])
check("nonstream arguments preserved", [json.loads(item["function"]["arguments"]) for item in calls] == [
    {"text": str(position)} for position in range(4)])

finish()

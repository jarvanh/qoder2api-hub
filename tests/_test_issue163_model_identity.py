"""Issue163: unknown model policy and truthful upstream identity (offline only)."""
import io
import json
import os
import socket
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import bootstrap, check, declare_suite, finish

_TMP = tempfile.TemporaryDirectory(prefix="qd-issue163-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP.name, "accounts")
os.environ["QD_PROXY_USAGE_DIR"] = os.path.join(_TMP.name, "usage")
os.environ["QD_NATIVE_IDENTITY"] = "0"
os.environ.pop("QD_UNKNOWN_MODEL", None)
P, A = bootstrap()
declare_suite(__file__, ["qoder_proxy.py", "qoder_catalog.py"])


def envelope(chunk):
    return ("data: " + json.dumps({"statusCodeValue": 200,
            "body": json.dumps(chunk)}) + "\n\n").encode()


def frames(chunks, holder=None):
    holder = holder if holder is not None else {}
    lines = [("data: " + json.dumps(c) + "\n\n").encode() for c in chunks]
    out = []
    for frame in P.stream_responses_events(lines, "CUSTOM_MODEL", holder):
        text = frame.decode() if isinstance(frame, bytes) else frame
        out.append(json.loads(next(l[6:] for l in text.splitlines()
                                   if l.startswith("data: "))))
    return out


def chunk(model=None, finish=None, usage=None):
    obj = {"choices": [{"index": 0, "delta": {"content": "TARGET"},
                        "finish_reason": finish}]}
    if model is not None:
        obj["model"] = model
    if usage:
        obj["usage"] = usage
    return obj


usage = {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}
with mock.patch.object(socket.socket, "connect", side_effect=AssertionError("offline fixture")), \
     mock.patch.object(P, "read_local_models", return_value=[]), \
     mock.patch.object(P, "read_dynamic_models", side_effect=AssertionError("no discovery requests")):
    P.qoder_catalog.clear_live_display_names()
    entries = [("dynamic_key_163", {"display_name": "Dynamic Display 163"}),
               ("key_without_display_163", {})]
    # Simulate startup/model-list cache, not a frozen alias table.
    P._models_cache["cn"] = {"at": P.time.time(), "data": entries}
    P._LIVE_NAMES_SYNC_AT.clear()
    for name, expected in [("dynamic_key_163", "dynamic_key_163"),
                           ("DYNAMIC DISPLAY 163", "dynamic_key_163"),
                           ("dynamic_key_163 (Dynamic Display 163)", "dynamic_key_163"),
                           ("key_without_display_163", "key_without_display_163"),
                           ("auto", "auto"), (None, "auto"),
                           ("kimi-k3.1-pro", "kmodel_latest")]:
        with mock.patch.dict(os.environ, {"QD_UNKNOWN_MODEL": "error"}):
            check("strict accepts advertised/alias %r" % name,
                  P.resolve_request_model(name, "cn") == expected)
    P._UNKNOWN_MODEL_WARN_AT = None
    with mock.patch.object(P, "log") as logs:
        for n in range(100):
            check("default preserves BYOK %d" % n,
                  P.resolve_request_model("CUSTOM_%d" % n, "cn") == "CUSTOM_%d" % n)
        check("unknown warning is process-wide bounded", logs.call_count == 1)
        check("warning has WARN level", logs.call_args.kwargs.get("level") == "WARN")
    with mock.patch.dict(os.environ, {"QD_UNKNOWN_MODEL": "auto"}):
        check("explicit auto normalizes unknown", P.resolve_request_model("CUSTOM_MODEL", "cn") == "auto")
    with mock.patch.dict(os.environ, {"QD_UNKNOWN_MODEL": "invalid"}):
        try:
            P.resolve_request_model("auto", "cn")
            check("invalid policy rejected", False)
        except ValueError:
            check("invalid policy rejected", True)
    with mock.patch.dict(os.environ, {"QD_UNKNOWN_MODEL": "error"}), \
         mock.patch.object(P, "POOL", None), \
         mock.patch.object(P, "build_qoder_body", side_effect=AssertionError("no billable body")), \
         mock.patch.object(P.urllib.request, "urlopen", side_effect=AssertionError("no billed request")) as send:
        try:
            P.open_upstream({"model": "CUSTOM_MODEL", "messages": []}, target_realm="cn")
            check("strict rejects before billed request", False)
        except P.UpstreamStatus as exc:
            check("strict rejects before billed request", exc.status == 400 and "unknown model" in exc.detail)
        check("strict sends zero requests", send.call_count == 0)
        handler = object.__new__(P.Handler)
        handler._cross_realm_error = lambda *a: None
        handler._banned_model_error = lambda *a: None
        handler._key_model_error = lambda *a: None
        check("shared handler precheck rejects", "unknown model" in handler._model_precheck("CUSTOM_MODEL", "cn"))

    account = A.Account({"uid": "TARGET", "realm": "cn", "accessToken": "FAKE_TOKEN"})
    for policy, expected in (("pass", "CUSTOM_MODEL"), ("auto", "auto")):
        wire_resp = io.BytesIO(envelope(chunk("auto", "stop", usage)))
        ctx = {}
        with mock.patch.dict(os.environ, {"QD_UNKNOWN_MODEL": policy}), \
             mock.patch.object(P, "POOL", None), \
             mock.patch.object(P, "build_qoder_body", return_value={"messages": [], "parameters": {}}) as build, \
             mock.patch.object(P, "assign_prefix_session"), \
             mock.patch.object(P, "gateway_candidates", return_value=["https://TARGET.invalid"]), \
             mock.patch.object(P, "validate_public_http_url", side_effect=lambda url: url), \
             mock.patch.object(P.SESSIONS, "get") as session, \
             mock.patch.object(P.urllib.request, "urlopen", return_value=wire_resp), \
             mock.patch.object(P, "_team_acquire", return_value=None):
            session.return_value.headers.return_value = {}
            opened, used, _ = P.open_upstream({"model": "CUSTOM_MODEL", "messages": []}, target_realm="cn", only_account=account, usage_ctx=ctx)
            check(policy + " submits correct upstream key", build.call_args.args[2] == expected and session.return_value.headers.call_args.kwargs["model_key"] == expected)
            check(policy + " actual remains null before consumption", ctx["resolved_model"] is None)
            list(P.iter_inner_sse(opened))
            check(policy + " real open attaches capture context", ctx["requested_model"] == "CUSTOM_MODEL" and ctx["upstream_model_key"] == expected and ctx["resolved_model"] == "auto")

    audit = {"requested_model": "CUSTOM_MODEL", "upstream_model_key": "CUSTOM_MODEL", "resolved_model": None}
    upstream = io.BytesIO(envelope(chunk("auto", "stop", usage)))
    upstream._qd_model_audit = audit
    holder = {}
    obj = P.aggregate_stream(upstream, "CUSTOM_MODEL", holder=holder)
    check("actual upstream model reaches aggregate", obj["model"] == "auto" and obj["resolved_model"] == "auto")
    check("actual model updates both holder and audit", holder["resolved_model"] == audit["resolved_model"] == "auto")
    response = P.chat_to_response(obj, "CUSTOM_MODEL")
    check("nonstream Responses reports actual, preserves requested", response["model"] == response["resolved_model"] == "auto" and response["requested_model"] == "CUSTOM_MODEL")
    rows = []
    with mock.patch.object(P, "_persist_usage", side_effect=lambda row, *a: rows.append(row)):
        P.record_usage("CUSTOM_MODEL", usage, session_trace=audit)
        P.record_usage("CUSTOM_MODEL", usage)
    check("usage stores requested and actual separately", rows[0]["requested_model"] == "CUSTOM_MODEL" and rows[0]["resolved_model"] == "auto")
    check("usage stores outgoing key, keeps legacy grouping", rows[0]["upstream_model_key"] == rows[0]["model"] == "CUSTOM_MODEL")
    check("missing identity is null not requested", rows[1]["resolved_model"] is None)

    unknown = P.aggregate_stream(io.BytesIO(envelope(chunk(finish="stop", usage=usage))), "CUSTOM_MODEL")
    check("aggregate does not invent actual", unknown["resolved_model"] is None)
    folded = P.chat_to_response(unknown, "CUSTOM_MODEL")
    check("Responses missing actual remains explicit", folded["model"] == "" and folded["resolved_model"] is None)
    events = frames([chunk(), chunk("auto", "stop", usage)])
    check("created/in_progress do not claim requested actual", all(e["response"]["model"] == "" and e["response"]["resolved_model"] is None for e in events[:2]))
    check("model discovery emits updated in_progress", any(e["type"] == "response.in_progress" and e["response"]["model"] == "auto" for e in events))
    check("completed actual identity", events[-1]["type"] == "response.completed" and events[-1]["response"]["resolved_model"] == "auto")
    check("requested preserved throughout", all(e["response"]["requested_model"] == "CUSTOM_MODEL" for e in events if "response" in e))
    check("sequence numbers stay monotonic", [e["sequence_number"] for e in events] == list(range(1, len(events)+1)))
    no_model = frames([chunk(finish="stop")])[-1]["response"]
    check("stream missing model never invents actual", no_model["model"] == "" and no_model["resolved_model"] is None)
    late = frames([chunk("FIRST"), chunk("ACTUAL", "stop")])[-1]["response"]
    check("latest explicit upstream model wins", late["model"] == "ACTUAL")
    known = frames([chunk("auto", "stop")], {"resolved_model": "auto"})
    check("created uses actual when already captured", known[0]["response"]["model"] == "auto")
    quiet = P._responses_completed_quiet({"resolved_model": "auto"}, "CUSTOM_MODEL")
    check("lenient completed uses actual too", '"resolved_model": "auto"' in quiet and '"model": "auto"' in quiet)
    replay = list(P._chat_chunks(unknown, "CUSTOM_MODEL"))
    check("web replay does not manufacture identity", all(c["model"] == "" for c in replay))

_TMP.cleanup()
finish()

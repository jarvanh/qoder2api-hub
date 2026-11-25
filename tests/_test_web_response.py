"""Behavioral tests for hosted web tools and Responses wire semantics (offline)."""
import contextlib
import copy
import http.client
import http.server
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_tmp = tempfile.TemporaryDirectory(prefix="qd-web-response-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_tmp.name, "accounts")
os.environ["QD_PROXY_USAGE_DIR"] = os.path.join(_tmp.name, "usage")
import qoder_accounts as A
import qoder_official_web as O
import qoder_proxy as P
import qoder_web_response as R
import qoder_webtools as W

# Importing qoder_proxy does not run main(), so env alone cannot bind its directory.
# Keep real HTTP auth/settings reads inside this suite, even in a configured checkout.
P.ACCOUNTS_DIR = os.environ["ACCOUNTS_DIR"]


def reply(text="", calls=None, usage=None):
    return {"choices": [{"index": 0, "message": {"role": "assistant", "content": text,
             **({"tool_calls": calls} if calls else {})},
             "finish_reason": "tool_calls" if calls else "stop"}], "usage": usage or {}}


def call(name="web_search", args=None, id="call-1"):
    return {"id": id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(args or {"query": "news"})}}


def search_data():
    return {"pageItems": [{"title": "<em>Official</em> &amp; announcement",
                          "link": "https://example.com/news/announcement", "snippet": "<b>A fact</b>",
                          "publishedTime": "2026-11-16"}]}


class EvidenceTests(unittest.TestCase):
    def test_domain_matching_boundaries_and_idna(self):
        self.assertTrue(R.matches_domain("https://sub.example.com/x", ["example.com"]))
        self.assertFalse(R.matches_domain("https://evilexample.com/x", ["example.com"]))
        self.assertFalse(R.matches_domain("https://example.com.evil.test", ["example.com"]))
        self.assertFalse(R.matches_domain("https://a.example.com", ["example.com"], ["a.example.com"]))
        self.assertEqual(R.domains(["BÜCHER.de."]), ["xn--bcher-kva.de"])
        for value in [["https://example.com"], ["*.example.com"], ["example.com:443"], "example.com", [None]]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                R.domains(value)

    def test_ambiguous_or_unsupported_request_options_rejected(self):
        for tools in [[{"type": "web_search", "filters": "bad"}],
                      [{"type": "web_search", "external_web_access": False}],
                      [{"type": "web_search", "search_content_types": ["image"]}],
                      [{"type": "web_search", "filters": {"allowed_domains": ["a.test"]}},
                       {"type": "web_search", "filters": {"allowed_domains": ["b.test"]}}]]:
            with self.assertRaises(ValueError):
                R.WebEvidence(tools)

    def test_source_ids_stable_and_request_scoped(self):
        e = R.WebEvidence()
        first = e.remember("https://example.com/x#one", "Title")
        second = e.remember("https://example.com/x#two", "Title", fetched=True)
        self.assertIs(first, second)
        self.assertTrue(first["fetched"])
        other = R.WebEvidence()
        self.assertEqual(other.sources, {})
        self.assertIsNone(e.remember("http://127.0.0.1/", "private"))

    def test_official_cache_scope_includes_realm_origin_and_credentials(self):
        evidence = R.WebEvidence()
        account = SimpleNamespace(uid="one", realm="cn", access_token="first")

        def key(origin="https://one.example"):
            return evidence.cache_key("web_search", {"query": "docs"}, account, "qoder", origin)

        before = key()
        self.assertEqual(key(), before)
        self.assertNotEqual(key("https://two.example"), before)
        account.realm = "intl"
        self.assertNotEqual(key(), before)
        account.realm = "cn"
        account.access_token = "second"
        self.assertNotEqual(key(), before)

    def test_only_observed_sources_are_annotated(self):
        e = R.WebEvidence()
        e.remember("https://example.com/x", "原始来源")
        text = e.render("中文 [S1]；不要编造 [S9]。https://unseen.example/x")
        self.assertIn("[原始来源](https://example.com/x)", text)
        self.assertIn("[S9]", text)
        annotations = e.annotations(text)
        self.assertEqual(len(annotations), 1)
        a = annotations[0]
        self.assertEqual(text[a["start_index"]:a["end_index"]], "[原始来源](https://example.com/x)")
        self.assertEqual(a["url"], "https://example.com/x")

    def test_local_filter_never_expands_domain_scope(self):
        e = R.WebEvidence([{"type": "web_search", "filters": {"allowed_domains": ["example.com"]}}])
        result = e.observe("web_search", {"ok": True, "results": [
            {"url": "https://example.com/x", "title": "ok"},
            {"url": "https://notexample.com/x", "title": "bad"}]})
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(len(e.sources), 1)

    def test_redirect_cannot_bypass_domain_filter(self):
        e = R.WebEvidence([{"type": "web_search", "filters": {"allowed_domains": ["example.com"]}}])
        result = e.observe("web_fetch", {"ok": True, "url": "https://example.com/x",
            "final_url": "https://outside.test/x", "text": "hidden"})
        self.assertFalse(result["ok"])
        self.assertNotIn("text", result)
        self.assertFalse(e.sources)

    def test_model_can_narrow_but_not_widen_request_domain_filters(self):
        e = R.WebEvidence([{"type": "web_search", "filters": {"allowed_domains": ["example.com"]}}])
        with mock.patch.object(W, "search", return_value={"ok": True, "engine": "local", "results": [
                {"url": "https://a.example.com/x"}, {"url": "https://b.example.com/x"},
                {"url": "https://outside.test/x"}]}), \
                mock.patch.dict(os.environ, {"QD_WEB_TOOLS_BACKEND": "local"}):
            results = []
            W.run_tool("web_search", {"query": "test", "allowed_domains": ["a.example.com"]},
                       context=e, on_result=results.append)
        self.assertEqual([r["url"] for r in results[0]["results"]], ["https://a.example.com/x"])


class HostedWebHandlerTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"QD_WEB_TOOLS_BACKEND": "qoder"})
        self.env.start(); self.addCleanup(self.env.stop)
        self.acc = SimpleNamespace(uid="test-scope", access_token="fake-test-token", realm="cn",
            realm_config=lambda: {"gateway": "https://gateway.qoder.com.cn", "vpc_instance": ""})
        self.transport = mock.patch.object(A, "http_json", return_value=search_data())
        self.http = self.transport.start(); self.addCleanup(self.transport.stop)

    def run_handler(self, responses, payload=None, protocol="responses", stream=False, upstream_error=None, rounds=3):
        frames = []; requests = []; first_model_observed = []
        handler = object.__new__(P.Handler)
        handler.headers = {}; handler.key_entry = None
        handler._model_precheck = lambda *a: None
        handler._request_realm = lambda: "cn"
        handler._key_id_for_usage = lambda: "test-key"
        handler._json = lambda status, obj: (status, obj)
        handler._error = lambda status, message, *a: (status, {"error": message})
        handler._sse_begin = lambda: None
        handler._sse_write = frames.append
        handler._rate_limited = lambda e: (429, {})
        def open_upstream(req, **kwargs):
            requests.append(copy.deepcopy(req))
            first_model_observed.append(len(frames))
            if upstream_error:
                raise upstream_error
            return contextlib.nullcontext(), self.acc, None
        request = {"model": "test-model", "stream": stream,
                   "tools": [{"type": "web_search"}],
                   "include": ["web_search_call.action.sources"], "metadata": {"test": "preserved"}}
        request.update(payload or {})
        path = "/v1/responses" if protocol == "responses" else "/v1/chat/completions"
        with mock.patch.object(P, "open_upstream", side_effect=open_upstream), \
                mock.patch.object(P, "aggregate_stream", side_effect=copy.deepcopy(responses)), \
                mock.patch.object(P, "record_usage"), mock.patch.object(P, "WEB_TOOLS_MAX_ROUNDS", rounds):
            result = handler._handle_web_tools(path, request, P.builtin_web_tool_names(request["tools"]))
        events = [json.loads(f.decode().split("\ndata: ", 1)[1]) for f in frames]
        return result, events, requests, first_model_observed

    def test_stream_emits_real_progress_before_final_and_preserves_ids(self):
        result, events, requests, first_model = self.run_handler(
            [reply(calls=[call()]), reply("中文，见 [S1]。")], stream=True)
        self.assertGreater(first_model[0], 0)  # created/in_progress before model inference
        kinds = [e["type"] for e in events]
        self.assertIn("response.web_search_call.searching", kinds)
        self.assertIn("response.web_search_call.completed", kinds)
        self.assertIn("response.output_text.annotation.added", kinds)
        self.assertLess(kinds.index("response.web_search_call.completed"), kinds.index("response.output_text.delta"))
        sequences = [e["sequence_number"] for e in events]
        self.assertEqual(sequences, list(range(1, len(events) + 1)))
        final = events[-1]["response"]
        self.assertEqual(final["id"], events[0]["response"]["id"])
        self.assertEqual([x["type"] for x in final["output"]], ["web_search_call", "message"])
        self.assertEqual(final["output"][0]["action"]["sources"], [
            {"type": "url", "url": "https://example.com/news/announcement"}])
        message = final["output"][1]
        self.assertEqual(message["content"][0]["annotations"][0]["type"], "url_citation")
        done = [e["item"] for e in events if e["type"] == "response.output_item.done"]
        self.assertEqual(done, final["output"])
        self.assertEqual(final["tools"], [{"type": "web_search"}])
        self.assertEqual(final["metadata"], {"test": "preserved"})
        self.assertEqual(events[-1]["type"], "response.completed")

    def test_nonstream_has_sources_citations_and_no_internal_function_calls(self):
        (status, obj), events, _, _ = self.run_handler([reply(calls=[call()]), reply("See [S1]")])
        self.assertEqual(status, 200)
        self.assertEqual(events, [])
        self.assertEqual([x["type"] for x in obj["output"]], ["web_search_call", "message"])
        self.assertEqual(len(obj["output"][1]["content"][0]["annotations"]), 1)

    def test_search_alone_allows_open_page_with_official_fetch(self):
        self.http.return_value = {"results": [{"statusCode": 200, "markdown": "Page contents",
                                               "metadata": {"title": "Primary source"}}]}
        with mock.patch.object(W, "check_url", return_value=(True, "")):
            (status, obj), _, requests, _ = self.run_handler([
                reply(calls=[call("web_fetch", {"url": "https://example.com/announcement"})]), reply("Read [S1]")])
        self.assertEqual(status, 200)
        self.assertIn("web_fetch", [t["function"]["name"] for t in requests[0]["tools"]])
        self.assertEqual(obj["output"][0]["action"]["type"], "open_page")
        self.assertIn(O.FETCH_PATH, self.http.call_args.args[0])

    def test_last_tool_round_is_followed_by_synthesis_and_usage_summed(self):
        u1 = {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12,
              "prompt_tokens_details": {"cached_tokens": 4},
              "completion_tokens_details": {"reasoning_tokens": 1}}
        u2 = {"prompt_tokens": 20, "completion_tokens": 3, "total_tokens": 23,
              "prompt_tokens_details": {"cached_tokens": 5},
              "completion_tokens_details": {"reasoning_tokens": 2}}
        first = dict(reply(calls=[call()], usage=u1), resolved_model="search-model")
        first["choices"][0]["message"]["reasoning_content"] = "Tool-round reasoning"
        final = dict(reply("Summary [S1]", usage=u2), resolved_model="synthesis-model")
        (status, obj), _, requests, _ = self.run_handler([first, final], rounds=1)
        self.assertEqual(status, 200)
        self.assertEqual(obj["resolved_model"], "synthesis-model")
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[1]["tools"], [])
        self.assertEqual(requests[1]["tool_choice"], "none")
        self.assertEqual(requests[1]["messages"][-2]["reasoning_content"], "Tool-round reasoning")
        self.assertIn("Published: 2026-11-16", requests[1]["messages"][-1]["content"])
        self.assertEqual(obj["usage"]["input_tokens"], 30)
        self.assertEqual(obj["usage"]["output_tokens"], 5)
        self.assertEqual(obj["usage"]["input_tokens_details"]["cached_tokens"], 9)
        self.assertEqual(obj["usage"]["output_tokens_details"]["reasoning_tokens"], 3)

    def test_duplicate_calls_cached_only_within_request(self):
        calls = [call(id="one"), call(id="two")]
        self.run_handler([reply(calls=calls), reply("done")])
        self.assertEqual(self.http.call_count, 1)
        self.run_handler([reply(calls=[call()]), reply("done")])
        self.assertEqual(self.http.call_count, 2)

    def test_domain_filters_enforced_even_when_official_ignores_them(self):
        self.http.return_value = {"pageItems": [
            {"title": "bad", "link": "https://badexample.com/x", "snippet": "bad"},
            {"title": "ok", "link": "https://sub.example.com/x", "snippet": "good"}]}
        (status, obj), _, requests, _ = self.run_handler([reply(calls=[call()]), reply("[S1]")],
            {"tools": [{"type": "web_search", "filters": {"allowed_domains": ["example.com"]}}]})
        content = requests[1]["messages"][-1]["content"]
        self.assertNotIn("badexample.com", content)
        self.assertIn("sub.example.com", content)
        self.assertEqual(len(obj["output"][0]["action"]["sources"]), 1)

    def test_fetch_cannot_bypass_request_domain_scope(self):
        (status, obj), _, _, _ = self.run_handler([
            reply(calls=[call("web_fetch", {"url": "https://outside.test/x"})]), reply("Unable to verify")],
            {"tools": [{"type": "web_search", "filters": {"allowed_domains": ["example.com"]}}]})
        self.http.assert_not_called()
        self.assertEqual(obj["output"][0]["status"], "failed")

    def test_tool_budget_caps_calls_and_keeps_all_tool_call_ids_satisfied(self):
        (status, obj), _, requests, _ = self.run_handler(
            [reply(calls=[call(id="one"), call(args={"query": "different"}, id="two")]), reply("done")],
            {"max_tool_calls": 1})
        self.assertEqual(self.http.call_count, 1)
        tools = [m for m in requests[1]["messages"] if m["role"] == "tool"]
        self.assertEqual([m["tool_call_id"] for m in tools], ["one", "two"])
        self.assertIn("budget exhausted", tools[1]["content"])
        self.assertEqual(obj["output"][1]["status"], "failed")

    def test_none_or_zero_budget_cannot_execute_tools(self):
        for options in [{"tool_choice": "none"}, {"max_tool_calls": 0}]:
            (status, obj), _, requests, _ = self.run_handler([reply("No search")], options)
            self.assertEqual(status, 200)
            self.assertEqual(requests[0]["tools"], [])
        self.http.assert_not_called()

    def test_upstream_ignoring_disabled_tools_fails_closed(self):
        (status, body), _, _, _ = self.run_handler([reply(calls=[call()])], {"tool_choice": "none"})
        self.assertEqual(status, 502)
        self.http.assert_not_called()

    def test_none_remains_none_with_client_tools(self):
        tools = [{"type": "web_search"}, {"type": "function", "name": "client_tool", "parameters": {}}]
        (status, _), _, requests, _ = self.run_handler([reply("done")],
            {"tools": tools, "tool_choice": "none"})
        self.assertEqual(status, 200)
        self.assertEqual(requests[0]["tool_choice"], "none")
        self.http.assert_not_called()

    def test_application_instructions_and_explicit_parallel_choice_preserved(self):
        instructions = "Use short French sentences. Search only if needed."
        tools = [{"type": "web_search"}, {"type": "function", "name": "client_tool", "parameters": {}}]
        (status, _), _, requests, _ = self.run_handler([reply("Bonjour.")],
            {"instructions": instructions, "input": "Hello", "tools": tools,
             "parallel_tool_calls": True})
        self.assertEqual(status, 200)
        self.assertEqual(requests[0]["messages"], [
            {"role": "system", "content": instructions}, {"role": "user", "content": "Hello"}])
        self.assertTrue(requests[0]["parallel_tool_calls"])
        self.http.assert_not_called()

    def test_chat_completion_token_limit_translated(self):
        (status, _), _, requests, _ = self.run_handler([reply("done")],
            {"max_completion_tokens": 123}, protocol="chat")
        self.assertEqual(status, 200)
        self.assertEqual(requests[0]["max_tokens"], 123)
        self.assertNotIn("max_completion_tokens", requests[0])

    def test_forced_search_aliases_are_honored_on_first_turn(self):
        for kind in W.SEARCH_DECL_TYPES:
            (status, _), _, requests, _ = self.run_handler([reply(calls=[call()]), reply("done")],
                {"tools": [{"type": kind}], "tool_choice": {"type": kind}})
            self.assertEqual(status, 200)
            self.assertEqual(requests[0]["tool_choice"], "web_search")
            self.assertEqual(requests[1]["tool_choice"], "auto")

    def test_backend_changes_do_not_affect_an_in_flight_request(self):
        def remote_call(*args, **kwargs):
            os.environ["QD_WEB_TOOLS_BACKEND"] = "local"
            return search_data()
        with mock.patch.object(O, "_request", side_effect=remote_call) as official, \
                mock.patch.object(W, "search", side_effect=AssertionError("request backend changed")):
            (status, _), _, _, _ = self.run_handler([
                reply(calls=[call(args={"query": "one"})]),
                reply(calls=[call(args={"query": "two"})]), reply("done")])
        self.assertEqual(status, 200)
        self.assertEqual(official.call_count, 2)

    def test_upstream_error_after_sse_start_has_failed_terminal_only(self):
        _, events, _, _ = self.run_handler([], stream=True,
            upstream_error=urllib.error.HTTPError("https://upstream", 401, "denied", {}, io.BytesIO(b"")))
        self.assertEqual(events[0]["type"], "response.created")
        self.assertEqual(events[-1]["type"], "response.failed")
        self.assertNotIn("response.completed", [e["type"] for e in events])

    def test_openai_responses_preserves_rate_limit_after_stream_start(self):
        _, events, _, _ = self.run_handler([], stream=True,
            upstream_error=urllib.error.HTTPError("https://upstream", 429, "limited", {}, io.BytesIO(b"")))
        self.assertEqual(events[-1]["type"], "response.failed")
        self.assertEqual(events[-1]["response"]["error"]["code"], "rate_limit_exceeded")

    def test_responses_forced_hosted_tool_without_a_call_fails(self):
        _, events, _, _ = self.run_handler([reply("Unverified answer")],
            {"tool_choice": {"type": "web_search"}}, stream=True)
        self.assertEqual(events[-1]["type"], "response.failed")
        self.assertNotIn("Unverified answer", str(events))
        self.http.assert_not_called()

    def test_responses_none_stays_disabled_when_normalizer_drops_declarations(self):
        with mock.patch.object(P, "TOOLCHOICE_NONE_DROPS_TOOLS", True):
            (status, _), _, requests, _ = self.run_handler([reply("Done")], {"tool_choice": "none"})
        self.assertEqual(status, 200)
        self.assertEqual(requests[0]["tools"], [])
        self.assertEqual(requests[0]["tool_choice"], "none")
        self.http.assert_not_called()

    def test_official_tool_error_is_not_reported_as_completed_search(self):
        self.http.side_effect = TimeoutError("private-token-should-never-leak")
        _, events, _, _ = self.run_handler([reply(calls=[call()]), reply("Could not verify")], stream=True)
        self.assertNotIn("response.web_search_call.completed", [e["type"] for e in events])
        self.assertEqual(events[-1]["response"]["output"][0]["status"], "failed")
        self.assertNotIn("private-token", str(events))

    def test_invalid_options_rejected_before_inference(self):
        for options in [{"max_tool_calls": -1}, {"max_tool_calls": True},
                        {"previous_response_id": "resp_old"}, {"background": True},
                        {"tools": [{"type": "web_search", "filters": {"allowed_domains": ["https://bad"]}}]}]:
            (status, obj), _, requests, _ = self.run_handler([], options)
            self.assertEqual(status, 400)
            self.assertEqual(requests, [])

    def test_ordinary_function_returned_to_client_without_execution(self):
        tools = [{"type": "web_search"}, {"type": "function", "name": "client_shell", "parameters": {}}]
        (status, obj), _, requests, _ = self.run_handler([
            reply(calls=[call("client_shell", {"command": "echo hi"})])], {"tools": tools})
        self.assertEqual(status, 200)
        self.assertEqual(obj["output"][0]["type"], "function_call")
        self.assertEqual(obj["output"][0]["name"], "client_shell")
        self.http.assert_not_called()

    def test_responses_mixed_batch_searches_then_returns_original_client_call(self):
        ordinary = call("lookup_order", {"order": 17}, "call_order")
        tools = [{"type": "web_search"}, {"type": "function", "name": "lookup_order",
                  "parameters": {"type": "object"}}]
        _, events, requests, _ = self.run_handler([reply(calls=[call(), ordinary])], {"tools": tools}, stream=True)
        items = events[-1]["response"]["output"]
        self.assertEqual([item["type"] for item in items], ["web_search_call", "function_call"])
        self.assertEqual((items[1]["call_id"], items[1]["name"], json.loads(items[1]["arguments"])),
                         ("call_order", "lookup_order", {"order": 17}))
        self.assertEqual(events[-1]["type"], "response.completed")
        self.assertEqual(len(requests), 1)
        self.http.assert_called_once()

    def test_chat_annotations_use_chat_schema(self):
        (status, obj), _, _, _ = self.run_handler([reply(calls=[call()]), reply("[S1]")], protocol="chat")
        annotation = obj["choices"][0]["message"]["annotations"][0]
        self.assertEqual(annotation["type"], "url_citation")
        self.assertEqual(annotation["url_citation"]["url"], "https://example.com/news/announcement")


class HostedWebHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), P.Handler)
        cls.port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        cls.addClassCleanup(server.server_close)
        cls.addClassCleanup(thread.join)
        cls.addClassCleanup(server.shutdown)

    def post(self, payload, deltas, finish, path="/v1/responses", *, done=True,
             stream_factory=io.BytesIO, expected_status=200, upstream_model=None,
             inner_chunks=None):
        chunks = (copy.deepcopy(inner_chunks) if inner_chunks is not None else
                  [{"choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
                   for delta in deltas])
        if inner_chunks is None and finish is not None:
            chunks.append({"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
        if upstream_model is not None:
            chunks[0]["model"] = upstream_model
        envelopes = [{"body": json.dumps(chunk)} for chunk in chunks]
        if done:
            envelopes.append({"body": "[DONE]"})
        wire = "".join("data: " + json.dumps(envelope) + "\n\n" for envelope in envelopes)
        account = SimpleNamespace(uid="test-http", realm="cn")
        upstream = (stream_factory(wire.encode("utf-8")), account, None)
        with mock.patch.dict(os.environ, {"QD_WEB_TOOLS": "on", "QD_WEB_TOOLS_BACKEND": "qoder"}), \
                mock.patch.object(P, "API_KEY", "web-http-key"), \
                mock.patch.object(P, "open_upstream", return_value=upstream) as infer, \
                mock.patch.object(P, "record_usage"), \
                mock.patch.object(W, "run_tool") as run_tool:
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            try:
                conn.request("POST", path, body=json.dumps(payload), headers={
                    "Content-Type": "application/json", "Authorization": "Bearer web-http-key"})
                with conn.getresponse() as resp:
                    self.assertEqual(resp.status, expected_status)
                    body = resp.read().decode("utf-8")
            finally:
                conn.close()
        infer.assert_called_once()
        run_tool.assert_not_called()
        return body, infer.call_args.args[0]

    def post_anthropic(self, payload, rounds, *, expected_status=200, fetched=None, account=None,
                       headers=None, search_backend="qoder"):
        return self.post_hosted("/v1/messages", payload, rounds, expected_status=expected_status,
                                fetched=fetched, account=account, headers=headers, search_backend=search_backend)

    def post_hosted(self, path, payload, rounds, *, expected_status=200, fetched=None, account=None,
                    headers=None, search_backend="qoder"):
        account = account or SimpleNamespace(uid="test-scope", access_token="fake-test-token", realm="cn",
            realm_config=lambda: {"gateway": "https://gateway.qoder.com.cn", "vpc_instance": ""})
        upstreams = []
        for items in rounds:
            wire = "".join("data: " + json.dumps({"body": json.dumps(item)}) + "\n\n" for item in items)
            wire += 'data: {"body":"[DONE]"}\n\n'
            upstreams.append((io.BytesIO(wire.encode()), account, None))
        with mock.patch.dict(os.environ, {"QD_WEB_TOOLS": "on", "QD_WEB_TOOLS_BACKEND": search_backend}), \
                mock.patch.object(P, "API_KEY", "web-http-key"), \
                mock.patch.object(P, "open_upstream", side_effect=upstreams) as infer, \
                mock.patch.object(P, "record_usage"), mock.patch.object(W, "check_url", return_value=(True, "")), \
                mock.patch.object(A, "http_json", return_value=fetched if fetched is not None else search_data()) as web:
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            try:
                conn.request("POST", path, body=json.dumps(payload), headers={
                    "Content-Type": "application/json", "x-api-key": "web-http-key",
                    "anthropic-version": "2023-06-01", **(headers or {})})
                with conn.getresponse() as resp:
                    body = resp.read().decode()
                    self.assertEqual(resp.status, expected_status, body)
            finally:
                conn.close()
        return body, [entry.args[0] for entry in infer.call_args_list], web.call_count

    @staticmethod
    def anthropic_round(delta=None, finish="stop"):
        return [{"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}]

    @staticmethod
    def hosted_payload(path, stream=False):
        search, ordinary = {"type": "web_search"}, {"type": "function", "name": "lookup_order",
            "parameters": {"type": "object"}}
        payload = {"model": "test-model", "stream": stream, "tools": [search, ordinary],
                   "messages": [{"role": "user", "content": "Find the order."}]}
        if path.endswith("/messages"):
            payload.update(max_tokens=64, tools=[{"type": "web_search_20250305", "name": "web_search"},
                                                {"name": "lookup_order", "input_schema": {"type": "object"}}])
        elif path.endswith("/responses"):
            payload["input"] = payload.pop("messages")
        else:
            payload["tools"][1] = {"type": "function", "function": ordinary}
        return payload

    HOSTED_PATHS = ("/v1/chat/completions", "/v1/responses", "/v1/messages")

    def test_truncated_tool_arguments_preserve_length_for_all_protocols(self):
        native = dict(call(), index=0)
        native["function"]["arguments"] = '{"query":'
        for path in self.HOSTED_PATHS:
            for stream in (False, True):
                with self.subTest(path=path, stream=stream):
                    body, requests, count = self.post_hosted(path, self.hosted_payload(path, stream),
                        [self.anthropic_round({"tool_calls": [native]}, "length")])
                    self.assertEqual((len(requests), count), (1, 0))
                    if path.endswith("/responses"):
                        obj = json.loads(body) if not stream else [json.loads(line[6:])
                            for line in body.splitlines() if line.startswith("data: ")][-1]["response"]
                        self.assertEqual((obj["status"], obj["incomplete_details"]["reason"]),
                                         ("incomplete", "max_output_tokens"))
                    elif path.endswith("/messages"):
                        self.assertIn('"stop_reason": "max_tokens"', body)
                    else:
                        self.assertIn('"finish_reason": "length"', body)
                    self.assertNotIn('"type": "server_tool_use"', body)

    def test_legacy_argument_only_delta_is_preserved_for_all_protocols(self):
        chunks = (self.anthropic_round({"function_call": {"name": "lookup_order", "arguments": '{"order":'}}, None)
                  + self.anthropic_round({"function_call": {"arguments": "17}"}}, "function_call"))
        for path in self.HOSTED_PATHS:
            with self.subTest(path=path):
                body, _, count = self.post_hosted(path, self.hosted_payload(path), [chunks])
                obj = json.loads(body)
                if path.endswith("/responses"):
                    tool, = obj["output"]
                    args = json.loads(tool["arguments"])
                elif path.endswith("/messages"):
                    tool, = obj["content"]
                    args = tool["input"]
                else:
                    item, = obj["choices"][0]["message"]["tool_calls"]
                    tool = item["function"]
                    args = json.loads(tool["arguments"])
                self.assertEqual((tool["name"], args, count), ("lookup_order", {"order": 17}, 0))

    def test_backend_socket_failure_has_a_terminal_response_for_all_protocols(self):
        native = dict(call(), index=0)
        rounds = [self.anthropic_round({"tool_calls": [native]}, "tool_calls"),
                  self.anthropic_round({"content": "Search unavailable."})]
        for path in self.HOSTED_PATHS:
            with self.subTest(path=path), mock.patch.object(W, "run_tool",
                    side_effect=ConnectionResetError("private fixture text")) as backend:
                body, requests, _ = self.post_hosted(path, self.hosted_payload(path, stream=True), rounds)
                backend.assert_called_once()
                self.assertEqual(json.loads(requests[1]["messages"][-1]["content"]),
                                 {"error": "web tool execution failed"})
                self.assertIn("Search unavailable.", body)
                self.assertNotIn("private fixture text", body)
                self.assertIn({"/v1/chat/completions": "data: [DONE]", "/v1/responses": "event: response.completed",
                               "/v1/messages": "event: message_stop"}[path], body)

    def test_invalid_hosted_inputs_never_reach_the_backend(self):
        invalid = dict(call(), index=0)
        invalid["function"]["arguments"] = '{"query": ["invalid"]}'
        rounds = [self.anthropic_round({"tool_calls": [invalid]}, "tool_calls"),
                  self.anthropic_round({"content": "Invalid search."})]
        for path in self.HOSTED_PATHS:
            with self.subTest(path=path), mock.patch.object(W, "run_tool") as backend:
                _, requests, _ = self.post_hosted(path, self.hosted_payload(path), rounds)
                backend.assert_not_called()
                self.assertEqual(json.loads(requests[1]["messages"][-1]["content"]), {"error": "invalid_tool_input"})

    def test_undeclared_call_cannot_trigger_web_or_reach_the_client(self):
        mixed = self.anthropic_round({"tool_calls": [dict(call(), index=0),
            dict(call("undeclared_tool", {"value": 17}, "call_unknown"), index=1)]}, "tool_calls")
        for path in self.HOSTED_PATHS:
            with self.subTest(path=path):
                body, requests, count = self.post_hosted(path, self.hosted_payload(path), [mixed], expected_status=502)
                self.assertEqual((len(requests), count), (1, 0))
                self.assertIn("undeclared tool call", body)
                self.assertNotIn("call_unknown", body)

    def test_conflicting_or_late_output_after_terminal_never_runs_tools(self):
        late = self.anthropic_round({"tool_calls": [dict(call(), index=0)]}, None)
        cases = (self.anthropic_round({"content": "Partial"}, "length") + self.anthropic_round(),
                 self.anthropic_round({"content": "Completed"}) + late,
                 self.anthropic_round(finish="function_call"))
        for path in self.HOSTED_PATHS:
            for chunks in cases:
                with self.subTest(path=path, chunks=chunks):
                    body, requests, count = self.post_hosted(path, self.hosted_payload(path), [chunks],
                                                             expected_status=502)
                    self.assertEqual((len(requests), count), (1, 0))
                    self.assertIn("upstream stream failed", body)

    def test_usage_after_terminal_is_preserved_for_all_protocols(self):
        chunks = self.anthropic_round({"content": "Done"}) + [{"choices": [], "usage": {
            "prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}}]
        for path in self.HOSTED_PATHS:
            with self.subTest(path=path):
                body, _, count = self.post_hosted(path, self.hosted_payload(path), [chunks])
                usage = json.loads(body)["usage"]
                expected = "prompt_tokens" if path.endswith("/completions") else "input_tokens"
                self.assertEqual((usage[expected], count), (10, 0))

    def test_rejected_post_closes_the_connection_on_all_protocols(self):
        with mock.patch.object(P, "API_KEY", "required-test-key"), mock.patch.object(P, "log") as logs:
            for path in self.HOSTED_PATHS:
                with self.subTest(path=path):
                    conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
                    try:
                        conn.request("POST", path, json.dumps({"fixture": "body-marker"}),
                                     headers={"Content-Type": "application/json"})
                        with conn.getresponse() as response:
                            self.assertEqual(response.status, 401)
                            self.assertEqual(response.getheader("Connection"), "close")
                            response.read()
                    finally:
                        conn.close()
            self.assertNotIn("body-marker", str(logs.call_args_list))

    def test_anthropic_web_search_stream_and_non_stream_enable_native_tool(self):
        tool = {"type": "web_search_20250305", "name": "web_search", "max_uses": 1}
        payload = {"model": "test-model", "max_tokens": 100, "tools": [tool],
                   "messages": [{"role": "user", "content": "Search for an announcement."}],
                   "tool_choice": {"type": "tool", "name": "web_search"}}
        rounds = [self.anthropic_round({"tool_calls": [dict(call(), index=0)]}, "tool_calls"),
                  self.anthropic_round({"content": "See [S1]"})]
        body, requests, count = self.post_anthropic(payload, rounds)
        message = json.loads(body)
        call_block, result, text = message["content"]
        self.assertEqual(call_block["type"], "server_tool_use")
        self.assertEqual(call_block["input"], {"query": "news"})
        self.assertEqual(result["type"], "web_search_tool_result")
        self.assertEqual(result["tool_use_id"], call_block["id"])
        self.assertEqual(text["citations"][0]["url"], result["content"][0]["url"])
        self.assertEqual(count, 1)
        self.assertEqual(message["usage"]["server_tool_use"], {"web_search_requests": 1})
        self.assertIn("query", requests[0]["tools"][0]["function"]["parameters"]["properties"])
        self.assertEqual(requests[0]["tool_choice"], "web_search")
        self.assertIn("A fact", requests[1]["messages"][-1]["content"])
        self.assertNotIn("web_search", [d["function"]["name"] for d in requests[1]["tools"]])
        body, _, count = self.post_anthropic(dict(payload, stream=True), rounds)
        events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
        self.assertEqual([e["index"] for e in events if e["type"] == "content_block_start"], [0, 1, 2])
        self.assertIn("citations_delta", [e["delta"]["type"] for e in events if e["type"] == "content_block_delta"])
        self.assertEqual(events[-2]["delta"]["stop_reason"], "end_turn")
        self.assertEqual(events[-1]["type"], "message_stop")
        self.assertEqual(count, 1)

        replay = dict(payload, tools=[], tool_choice={"type": "auto"}, messages=payload["messages"] + [
            {"role": "assistant", "content": message["content"]}, {"role": "user", "content": "Continue."}])
        _, requests, count = self.post_anthropic(replay, [self.anthropic_round({"content": "Done"})])
        history = requests[0]["messages"]
        restored, = [row for row in history if row["role"] == "tool"]
        self.assertEqual(restored["tool_call_id"], call_block["id"])
        self.assertIn("A fact", restored["content"])
        self.assertEqual(count, 0)
        result["content"][0]["encrypted_content"] = "modified"
        body, requests, count = self.post_anthropic(replay, [], expected_status=400)
        self.assertEqual(json.loads(body)["error"]["type"], "invalid_request_error")
        self.assertEqual((requests, count), ([], 0))

    def test_anthropic_web_fetch_requires_url_in_context_and_returns_document(self):
        url = "https://example.com/announcement"
        tool = {"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": 1}
        payload = {"model": "test-model", "max_tokens": 90, "tools": [tool], "messages": [
            {"role": "user", "content": "Read " + url}]}
        rounds = [self.anthropic_round({"tool_calls": [dict(call("web_fetch", {"url": url}), index=0)]}, "tool_calls"),
                  self.anthropic_round({"content": "Done"})]
        body, _, count = self.post_anthropic(payload, rounds, fetched={"results": [
            {"statusCode": 200, "markdown": "Page contents", "metadata": {"title": "Primary source"}}]})
        block = json.loads(body)["content"][1]
        self.assertEqual(block["type"], "web_fetch_tool_result")
        self.assertEqual(block["content"]["content"]["source"]["data"], "Page contents")
        self.assertEqual(count, 1)
        payload["messages"][0]["content"] = "Read the page."
        body, _, count = self.post_anthropic(payload, rounds)
        self.assertEqual(json.loads(body)["content"][1]["content"]["error_code"], "url_not_in_prior_context")
        self.assertEqual(count, 0)

    def test_anthropic_hosted_choices_enforced_before_dispatch(self):
        payload = self.hosted_payload("/v1/messages")
        ordinary = self.anthropic_round({"tool_calls": [
            dict(call("lookup_order", {"order": 17}, "call_order"), index=0)]}, "tool_calls")
        search = self.anthropic_round({"tool_calls": [dict(call(), index=0)]}, "tool_calls")
        text = self.anthropic_round({"content": "Done"})
        forced = dict(payload, tool_choice={"type": "tool", "name": "lookup_order"})
        body, requests, count = self.post_anthropic(forced, [ordinary])
        self.assertEqual((json.loads(body)["stop_reason"], count), ("tool_use", 0))
        self.assertEqual(requests[0]["tool_choice"], "lookup_order")
        self.assertEqual([t["function"]["name"] for t in requests[0]["tools"]], ["lookup_order"])
        for wrong in (text, search):
            _, requests, count = self.post_anthropic(forced, [wrong], expected_status=502)
            self.assertEqual((len(requests), count), (1, 0))
        forced_search = dict(payload, tool_choice={"type": "tool", "name": "web_search"})
        truncated = self.anthropic_round(ordinary[0]["choices"][0]["delta"], "length")
        body, _, count = self.post_anthropic(forced_search, [truncated])
        message = json.loads(body)
        self.assertEqual((message["stop_reason"], message["content"], count), ("max_tokens", [], 0))
        missing = dict(payload, tool_choice={"type": "tool", "name": "missing"})
        _, requests, count = self.post_anthropic(missing, [], expected_status=400)
        self.assertEqual((requests, count), ([], 0))

        disabled = dict(payload, tool_choice={"type": "none"})
        # Even a misbehaving upstream must neither execute nor expose a tool.
        for result, status in ((text, 200), (ordinary, 502), (search, 502)):
            _, requests, count = self.post_anthropic(disabled, [result], expected_status=status)
            self.assertEqual((requests[0]["tools"], requests[0]["tool_choice"], count), ([], "none", 0))
        with mock.patch.object(P, "web_tools_enabled", return_value=False), \
                mock.patch.object(W, "backend_config", side_effect=AssertionError("none must not load the backend")):
            self.post_anthropic(disabled, [text])

        required = dict(payload, tool_choice={"type": "any"})
        _, requests, count = self.post_anthropic(required, [text], expected_status=502)
        self.assertEqual((requests[0]["tool_choice"], count), ("required", 0))
        body, _, count = self.post_anthropic(required, [ordinary])
        self.assertEqual((json.loads(body)["stop_reason"], count), ("tool_use", 0))
        _, requests, count = self.post_anthropic(required, [search, text])
        self.assertEqual(([req["tool_choice"] for req in requests], count), (["required", "auto"], 1))
        single = dict(payload, tool_choice={"type": "any", "disable_parallel_tool_use": True})
        mixed = self.anthropic_round({"tool_calls": [dict(call(), index=0),
            dict(call("lookup_order", {"order": 17}, "call_order"), index=1)]}, "tool_calls")
        _, requests, count = self.post_anthropic(single, [mixed], expected_status=502)
        self.assertIs(requests[0]["parallel_tool_calls"], False)
        self.assertEqual(count, 0)

    def test_anthropic_hosted_rounds_share_max_tokens_and_stop_at_zero(self):
        payload = dict(self.hosted_payload("/v1/messages"), max_tokens=20)
        first = self.anthropic_round({"tool_calls": [dict(call(), index=0)]}, "tool_calls")
        first += [{"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 7}}]
        second = self.anthropic_round({"tool_calls": [
            dict(call(args={"query": "other"}), index=0)]}, "tool_calls")
        second += [{"choices": [], "usage": {"prompt_tokens": 13, "completion_tokens": 4}}]
        final = self.anthropic_round({"content": "Done"})
        final += [{"choices": [], "usage": {"prompt_tokens": 15, "completion_tokens": 3}}]
        body, requests, count = self.post_anthropic(payload, [first, second, final])
        self.assertEqual(([req["max_tokens"] for req in requests], count), ([20, 13, 9], 2))
        self.assertEqual(json.loads(body)["usage"]["output_tokens"], 14)

        body, requests, count = self.post_anthropic(dict(payload, max_tokens=7), [first])
        self.assertEqual((json.loads(body)["stop_reason"], len(requests), count), ("max_tokens", 1, 0))
        self.assertEqual(json.loads(body)["content"], [])
        _, requests, count = self.post_anthropic(payload | {"max_tokens": 64}, [first[:1], final])
        self.assertLess(requests[1]["max_tokens"], requests[0]["max_tokens"])
        self.assertEqual(count, 1)

    def test_anthropic_rejects_reported_output_over_remaining_budget(self):
        payload = dict(self.hosted_payload("/v1/messages"), max_tokens=10)
        search = self.anthropic_round({"tool_calls": [dict(call(), index=0)]}, "tool_calls")
        search += [{"choices": [], "usage": {"completion_tokens": 7}}]
        final = self.anthropic_round({"content": "Done"})
        final += [{"choices": [], "usage": {"completion_tokens": 4}}]
        for stream in (False, True):
            with self.subTest(stream=stream):
                body, requests, count = self.post_anthropic(dict(payload, stream=stream), [search, final],
                    expected_status=200 if stream else 502)
                self.assertEqual(([req["max_tokens"] for req in requests], count), ([10, 3], 1))
                self.assertIn("outside the remaining token budget", body)
                self.assertNotIn("message_stop", body)

    def test_anthropic_rejects_missing_named_choice_and_invalid_explicit_usage(self):
        payload = self.hosted_payload("/v1/messages")
        for choice in ({"type": "tool"}, {"type": "tool", "name": ""},
                       {"type": "tool", "name": 123}):
            _, requests, count = self.post_anthropic(dict(payload, tool_choice=choice), [], expected_status=400)
            self.assertEqual((requests, count), ([], 0))
        search = self.anthropic_round({"tool_calls": [dict(call(), index=0)]}, "tool_calls")
        for value in (None, True, "4", -1):
            malformed = search + [{"choices": [], "usage": {"completion_tokens": value}}]
            body, requests, count = self.post_anthropic(payload, [malformed], expected_status=502)
            self.assertEqual((len(requests), count), (1, 0))
            self.assertIn("outside the remaining token budget", body)

    def test_anthropic_zero_use_tool_is_not_advertised_or_executed(self):
        payload = self.hosted_payload("/v1/messages")
        payload["tools"][0]["max_uses"] = 0
        payload["tools"].append({"type": "web_fetch_20250910", "name": "web_fetch"})
        search = self.anthropic_round({"tool_calls": [dict(call(), index=0)]}, "tool_calls")
        for choice in ({"type": "tool", "name": "web_search"}, {"type": "auto"}):
            forced = choice["type"] == "tool"
            _, requests, count = self.post_anthropic(dict(payload, tool_choice=choice), [] if forced else [search],
                expected_status=400 if forced else 502)
            self.assertEqual(count, 0)
            if not forced:
                self.assertNotIn("web_search", [tool["function"]["name"] for tool in requests[0]["tools"]])

    def test_anthropic_preserves_preambles_and_mixed_call_order_in_json_and_sse(self):
        payload = self.hosted_payload("/v1/messages")
        search = self.anthropic_round({"content": "Checking sources first.", "tool_calls": [
            dict(call(), index=0)]}, "tool_calls")
        search += [{"choices": [], "usage": {"completion_tokens": 10}}]
        mixed = self.anthropic_round({"content": "Checking the order next.", "tool_calls": [
            dict(call("lookup_order", {"order": 17}, "call_order"), index=0),
            dict(call(args={"query": "another"}), index=1),
            dict(call("lookup_order", {"order": 18}, "call_other"), index=2)]}, "tool_calls")
        mixed += [{"choices": [], "usage": {"completion_tokens": 20}}]
        expected = ["text", "server_tool_use", "web_search_tool_result", "text",
                    "tool_use", "server_tool_use", "tool_use"]
        for stream in (False, True):
            with self.subTest(stream=stream):
                body, _, count = self.post_anthropic(dict(payload, stream=stream), [search, mixed])
                if stream:
                    events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
                    blocks = [event["content_block"] for event in events if event["type"] == "content_block_start"]
                    texts = [event["delta"]["text"] for event in events if event["type"] == "content_block_delta"
                             and event["delta"]["type"] == "text_delta"]
                    self.assertEqual(events[-2]["delta"]["stop_reason"], "tool_use")
                    self.assertEqual(events[-1]["type"], "message_stop")
                else:
                    message = json.loads(body)
                    blocks = message["content"]
                    texts = [block["text"] for block in blocks if block["type"] == "text"]
                    self.assertEqual(message["stop_reason"], "tool_use")
                self.assertEqual([block["type"] for block in blocks], expected)
                self.assertEqual(texts, ["Checking sources first.", "Checking the order next."])
                self.assertEqual([block["id"] for block in blocks if block["type"] == "tool_use"],
                                 ["call_order", "call_other"])
                self.assertEqual(count, 1)

    @staticmethod
    def resume_messages(payload, reply, client_id="call_order"):
        return dict(payload, messages=payload["messages"] + [
            {"role": "assistant", "content": reply["content"]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": client_id, "content": "Found"}]},
        ])

    def test_anthropic_deferred_batch_counts_toward_round_and_token_limits(self):
        payload = dict(self.hosted_payload("/v1/messages"), max_tokens=30)
        mixed = self.anthropic_round({"tool_calls": [dict(call(), index=0),
            dict(call("lookup_order", {"order": 17}, "call_order"), index=1)]}, "tool_calls")
        mixed += [{"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 9}}]
        final = self.anthropic_round({"content": "Done"})
        with mock.patch.object(P, "WEB_TOOLS_MAX_ROUNDS", 1):
            body, _, count = self.post_anthropic(payload, [mixed])
            self.assertEqual(count, 0)
            resume = self.resume_messages(payload, json.loads(body))
            body, requests, count = self.post_anthropic(resume, [final])
            self.assertEqual((requests[0]["max_tokens"], count), (21, 1))
            self.assertEqual([tool["function"]["name"] for tool in requests[0]["tools"]], ["lookup_order"])
            self.assertEqual([block["type"] for block in json.loads(body)["content"]],
                             ["web_search_tool_result", "text"])
            body, requests, count = self.post_anthropic(dict(resume, max_tokens=6), [final])
            self.assertEqual((requests[0]["max_tokens"], count), (6, 0))

    def test_anthropic_deferred_batch_rechecks_unpinned_realm_and_account_scope(self):
        payload = self.hosted_payload("/v1/messages")
        mixed = self.anthropic_round({"tool_calls": [dict(call(), index=0),
            dict(call("lookup_order", {"order": 17}, "call_order"), index=1)]}, "tool_calls")
        final = self.anthropic_round({"content": "Done"})
        cn = A.Account({"uid": "resume-cn", "realm": "cn", "accessToken": "fake-cn"})
        intl = A.Account({"uid": "resume-intl", "realm": "intl", "accessToken": "fake-intl"})
        with tempfile.TemporaryDirectory() as directory:
            pool = A.AccountPool(directory, log=lambda *a, **k: None)
            pool.accounts = [cn, intl]
            with mock.patch.multiple(P, ACCOUNTS_DIR=directory, POOL=pool, CURRENT_REALM="cn"):
                body, _, _ = self.post_anthropic(payload, [mixed], account=cn)
                automatic = self.resume_messages(payload, json.loads(body))
                body, _, _ = self.post_anthropic(payload, [mixed], account=cn, headers={"X-Realm": "cn"})
                pinned = self.resume_messages(payload, json.loads(body))
                P.qoder_routing.save_rules(directory, [
                    {"id": "changed-region", "model": "*", "realm": "intl", "enabled": True}])
                body, requests, count = self.post_anthropic(automatic, [], account=cn, expected_status=403)
                self.assertIn("outside routing scope", body)
                self.assertEqual((requests, count), ([], 0))
                _, requests, count = self.post_anthropic(pinned, [final], account=cn, headers={"X-Realm": "cn"})
                self.assertEqual((len(requests), count), (1, 1))

                P.qoder_routing.save_rules(directory, [
                    {"id": "changed-accounts", "model": "*", "accounts": [intl.uid], "enabled": True}])
                _, requests, count = self.post_anthropic(automatic, [], account=cn, expected_status=403)
                self.assertEqual((requests, count), ([], 0))
                P.qoder_routing.save_rules(directory, [])
                with mock.patch.multiple(P, CURRENT_REALM="intl"), \
                        mock.patch.object(P, "detect_model_realm", return_value="intl"):
                    _, requests, count = self.post_anthropic(automatic, [], account=cn, expected_status=403)
                self.assertEqual((requests, count), ([], 0))

    def test_custom_fetch_is_used_in_all_protocols_independently_of_official_search(self):
        import qoder_external_web

        url = "https://example.com/reference"
        fetch = dict(call("web_fetch", {"url": url}), index=0)
        rounds = [self.anthropic_round({"tool_calls": [fetch]}, "tool_calls"),
                  self.anthropic_round({"content": "Got it [S1]."})]
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(P, "ACCOUNTS_DIR", directory):
            P.qoder_settings.set_web_tools_config(directory, {"enabled": True, "fetch_backend": "external",
                "external_fetch": {"endpoint": "http://fetcher:8080/fetch", "text_path": "data.markdown"}})
            for path, stream in ((path, stream) for path in self.HOSTED_PATHS for stream in (False, True)):
                with self.subTest(path=path, stream=stream), mock.patch.object(qoder_external_web, "_json_request",
                        return_value={"data": {"markdown": "Fetched by the configured service"}, "final_url": url}) as remote:
                    payload = self.hosted_payload(path, stream=stream)
                    history = payload.get("messages") or payload["input"]
                    history[0]["content"] = "Read " + url
                    payload["tools"][0] = ({"type": "web_fetch_20250910", "name": "web_fetch"}
                                            if path.endswith("/messages") else {"type": "web_fetch"})
                    body, inference, official_calls = self.post_hosted(path, payload, rounds)
                    self.assertEqual(official_calls, 0)
                    self.assertIn("Fetched by the configured service", inference[-1]["messages"][-1]["content"])
                    self.assertIn("Got it", body)
                    self.assertEqual(remote.call_args.args[:3], ("http://fetcher:8080/fetch", "POST", {"url": url}))
                    remote.assert_called_once()

    def test_anthropic_unused_fetch_backend_does_not_block_search_or_named_client_tool(self):
        payload = self.hosted_payload("/v1/messages")
        fetch = {"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": 0}
        payload["tools"].append(fetch)
        search = self.anthropic_round({"tool_calls": [dict(call(), index=0)]}, "tool_calls")
        final = self.anthropic_round({"content": "Done"})
        ordinary = self.anthropic_round({"tool_calls": [
            dict(call("lookup_order", {"order": 17}, "call_order"), index=0)]}, "tool_calls")
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(P, "ACCOUNTS_DIR", directory):
            P.qoder_settings.set_web_tools_config(directory, {"fetch_backend": "external"})
            _, requests, count = self.post_anthropic(payload, [search, final])
            self.assertEqual((len(requests), count), (2, 1))
            self.assertNotIn("web_fetch", [tool["function"]["name"] for tool in requests[0]["tools"]])
            fetch["max_uses"] = 1
            forced = dict(payload, tool_choice={"type": "tool", "name": "lookup_order"})
            _, requests, count = self.post_anthropic(forced, [ordinary])
            self.assertEqual(([tool["function"]["name"] for tool in requests[0]["tools"]], count),
                             (["lookup_order"], 0))
            forced_search = dict(payload, tool_choice={"type": "tool", "name": "web_search"})
            for request in (payload, forced_search):
                body, requests, count = self.post_anthropic(request, [], expected_status=400)
                self.assertIn("external fetch endpoint is required", body)
                self.assertEqual((requests, count), ([], 0))

    def test_anthropic_deferred_official_fetch_checks_credentials_with_local_search(self):
        url = "https://example.com/reference"
        payload = dict(self.hosted_payload("/v1/messages"), max_tokens=128,
                       messages=[{"role": "user", "content": "Read " + url}])
        payload["tools"][0] = {"type": "web_fetch_20250910", "name": "web_fetch"}
        mixed = self.anthropic_round({"tool_calls": [
            dict(call("web_fetch", {"url": url}), index=0),
            dict(call("lookup_order", {"order": 17}, "call_order"), index=1)]}, "tool_calls")
        mixed += [{"choices": [], "usage": {"completion_tokens": 12}}]
        final = self.anthropic_round({"content": "Done"})
        account = SimpleNamespace(uid="fetch-credentials", access_token="fake-original", realm="cn",
            realm_config=lambda: {"gateway": "https://gateway.qoder.com.cn", "vpc_instance": ""})
        options = {"account": account, "search_backend": "local"}
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(P, "ACCOUNTS_DIR", directory):
            P.qoder_settings.set_web_tools_config(directory, {"fetch_backend": "qoder"})
            body, _, count = self.post_anthropic(payload, [mixed], **options)
            self.assertEqual(count, 0)
            resume = self.resume_messages(payload, json.loads(body))
            fetched = {"results": [{"statusCode": 200, "markdown": "Official fetch"}]}
            P.qoder_settings.set_web_tools_config(directory, {"external": {"provider": "brave", "api_key": "unused-key"}})
            body, requests, count = self.post_anthropic(resume, [final], fetched=fetched, **options)
            self.assertIn("Official fetch", body)
            self.assertEqual((requests[0]["max_tokens"], count), (116, 1))
            P.qoder_settings.set_web_tools_config(directory, {"fetch_backend": "local"})
            body, requests, count = self.post_anthropic(resume, [], expected_status=400, **options)
            self.assertIn("unchanged web settings", body)
            self.assertEqual((requests, count), ([], 0))
            P.qoder_settings.set_web_tools_config(directory, {"fetch_backend": "qoder"})
            account.access_token = "fake-rotated"
            body, requests, count = self.post_anthropic(resume, [], expected_status=409, **options)
            self.assertIn("account credentials changed", body)
            self.assertEqual((requests, count), ([], 0))

    def test_anthropic_mixed_batch_waits_for_client_results_and_replays_once(self):
        account = SimpleNamespace(uid="continuation-account", access_token="fake-test-token", realm="cn",
            realm_config=lambda: {"gateway": "https://gateway.qoder.com.cn", "vpc_instance": ""})
        search = {"type": "web_search_20250305", "name": "web_search", "max_uses": 1}
        lookup = {"name": "lookup_order", "input_schema": {"type": "object"}}
        original = {"model": "test-model", "max_tokens": 100, "tools": [search, lookup],
                    "messages": [{"role": "user", "content": "Check the order and look it up."}]}
        mixed = self.anthropic_round({"tool_calls": [dict(call(), index=0),
            dict(call("lookup_order", {"order": 17}, "call_order"), index=1)]}, "tool_calls")
        body, _, count = self.post_anthropic(original, [mixed], account=account)
        response = json.loads(body)
        pending, external = response["content"]
        self.assertEqual((count, response["stop_reason"]), (0, "tool_use"))
        self.assertEqual(pending["type"], "server_tool_use")
        self.assertEqual((external["type"], external["id"], external["input"]),
                         ("tool_use", "call_order", {"order": 17}))
        prior = original["messages"] + [{"role": "assistant", "content": response["content"]}]
        resume = dict(original, messages=prior + [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "call_order", "content": "Order 17 exists"}]}])
        invalid = copy.deepcopy(resume)
        invalid["messages"][-1]["content"][0]["tool_use_id"] = "another_call"
        _, requests, count = self.post_anthropic(invalid, [], expected_status=400)
        self.assertEqual((requests, count), ([], 0))
        tampered = copy.deepcopy(resume)
        tampered["messages"][-2]["content"][0]["input"]["query"] = "changed query"
        _, requests, count = self.post_anthropic(tampered, [], expected_status=400)
        self.assertEqual((requests, count), ([], 0))
        answer = self.anthropic_round({"content": "See [S1] for order 17."})
        mismatched = dict(resume, messages=resume["messages"] + [
            {"role": "assistant", "content": [{"type": "web_fetch_tool_result", "tool_use_id": pending["id"],
                                                "content": {"type": "web_fetch_tool_result_error"}}]},
            {"role": "user", "content": "Continue."}])
        body, requests, count = self.post_anthropic(mismatched, [], expected_status=400)
        self.assertIn("invalid result for a pending web tool", body)
        self.assertEqual((requests, count), ([], 0))
        body, requests, count = self.post_anthropic(resume, [answer])
        continuation = json.loads(body)
        result, text = continuation["content"]
        self.assertEqual((count, result["type"], result["tool_use_id"]),
                         (1, "web_search_tool_result", pending["id"]))
        self.assertEqual(continuation["stop_reason"], "end_turn")
        self.assertEqual(text["citations"][0]["url"], result["content"][0]["url"])
        self.assertEqual([message["tool_call_id"] for message in requests[0]["messages"]
                          if message["role"] == "tool"], ["call_order", pending["id"]])
        self.assertNotIn("web_search", [tool["function"]["name"] for tool in requests[0]["tools"]])
        body, _, count = self.post_anthropic(dict(resume, stream=True), [answer])
        events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
        blocks = [event["content_block"] for event in events if event["type"] == "content_block_start"]
        self.assertEqual((count, blocks[0]["type"], blocks[0]["tool_use_id"]),
                         (0, "web_search_tool_result", pending["id"]))
        self.assertEqual(events[-1]["type"], "message_stop")
        account.access_token = "different-fake-token"
        body, requests, count = self.post_anthropic(resume, [], expected_status=409)
        self.assertIn("account credentials changed", json.loads(body)["error"]["message"])
        self.assertEqual((requests, count), ([], 0))
        history = resume["messages"] + [{"role": "assistant", "content": continuation["content"]},
                                        {"role": "user", "content": "Explain the reference."}]
        _, requests, count = self.post_anthropic(dict(original, messages=history, tools=[]), [answer])
        self.assertEqual(count, 0)
        self.assertEqual([message["tool_call_id"] for message in requests[0]["messages"]
                          if message["role"] == "tool"], ["call_order", pending["id"]])

    def test_responses_stream_returns_function_declared_with_hosted_tool(self):
        tools = [{"type": "web_search"},
                 {"type": "function", "name": "lookup_order", "parameters": {"type": "object"}}]
        args = '{"order":17}'
        deltas = [{"tool_calls": [{"index": 0, "id": "call_order", "type": "function",
                                   "function": {"name": "lookup_order", "arguments": args[:5]}}]},
                  {"tool_calls": [{"index": 0, "function": {"arguments": args[5:]}}]}]
        additional = [tools[0], {"type": "namespace", "name": "orders", "tools": [tools[1]]}]
        for declarations, namespace in (({"tools": tools, "input": "Check my order"}, None), ({
                "input": [{"role": "user", "content": "Check my order"},
                          {"type": "additional_tools", "tools": additional}]}, "orders")):
            with self.subTest(declarations=declarations):
                payload = dict(declarations, model="test-model", stream=True)
                body, request = self.post(payload, deltas, "tool_calls")
                names = [t.get("name") or t["function"]["name"] for t in request["tools"]]
                self.assertIn("web_search", names)
                self.assertIn("lookup_order", names)
                self.assertFalse(request["parallel_tool_calls"])
                events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
                final = events[-1]
                self.assertEqual(final["type"], "response.completed")
                item, = final["response"]["output"]
                self.assertEqual((item["type"], item["call_id"], item["name"], item["arguments"]),
                                 ("function_call", "call_order", "lookup_order", args))
                self.assertEqual(item.get("namespace"), namespace)
                argument_events = [e for e in events if e["type"].startswith("response.function_call_arguments.")]
                self.assertEqual([e["type"].rsplit(".", 1)[-1] for e in argument_events], ["delta", "done"])
                delta, done = argument_events
                self.assertEqual(delta["delta"], args)
                self.assertEqual(done["arguments"], args)
                self.assertEqual(delta["item_id"], item["id"])
                self.assertEqual(done["item_id"], item["id"])

    def test_responses_stream_emits_the_first_argument_fragment_without_web_tools(self):
        tools = [{"type": "function", "name": "lookup_order", "parameters": {"type": "object"}}]
        payload = {"model": "test-model", "input": "Check my order", "tools": tools, "stream": True}
        first, rest = '{"order":', '17}'
        deltas = [{"tool_calls": [{"index": 0, "id": "call_order", "type": "function",
                                   "function": {"name": "lookup_order", "arguments": first}}]},
                  {"tool_calls": [{"index": 0, "function": {"arguments": rest}}]}]
        body, _ = self.post(payload, deltas, "tool_calls")
        events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
        args = [e["delta"] for e in events if e["type"] == "response.function_call_arguments.delta"]
        self.assertEqual(args, [first, rest])
        done, = (e for e in events if e["type"] == "response.function_call_arguments.done")
        item, = events[-1]["response"]["output"]
        self.assertEqual(done["item_id"], item["id"])
        self.assertEqual(done["arguments"], first + rest)
        self.assertEqual(item["arguments"], done["arguments"])

    def test_responses_empty_function_arguments_are_valid_json_in_both_modes(self):
        tools = [{"type": "web_search"},
                 {"type": "function", "name": "get_time", "parameters": {"type": "object"}}]
        payload = {"model": "test-model", "input": "Tell me the time", "tools": tools}
        for value in ("", " \t"):
            with self.subTest(value=value):
                deltas = [{"tool_calls": [{"index": 0, "id": "call_time", "type": "function",
                    "function": {"name": "get_time", "arguments": value}}]}]
                body, _ = self.post(payload, deltas, "tool_calls")
                item, = json.loads(body)["output"]
                self.assertEqual(item["arguments"], "{}")
                stream, _ = self.post(dict(payload, stream=True), deltas, "tool_calls")
                events = [json.loads(line[6:]) for line in stream.splitlines() if line.startswith("data: ")]
                delta, done = [e for e in events if e["type"].startswith("response.function_call_arguments.")]
                streamed, = events[-1]["response"]["output"]
                self.assertEqual(delta["type"], "response.function_call_arguments.delta")
                self.assertEqual(done["type"], "response.function_call_arguments.done")
                self.assertEqual(delta["delta"], item["arguments"])
                self.assertEqual(done["arguments"], item["arguments"])
                self.assertEqual(streamed["arguments"], item["arguments"])

    def test_chat_stream_preserves_tool_call_indices_and_finish_reason(self):
        functions = [{"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}
                     for name in ("lookup_order", "lookup_store")]
        payload = {"model": "test-model", "messages": [{"role": "user", "content": "Check my order"}],
                   "tools": [{"type": "web_search"}] + functions, "stream": True}
        upstream = [{"index": index, "id": "call_%s" % index, "type": "function",
                     "function": {"name": tool["function"]["name"], "arguments": "{}"}}
                    for index, tool in enumerate(functions)]
        body, _ = self.post(payload, [{"tool_calls": upstream}], "tool_calls", "/v1/chat/completions")
        data = [line[6:] for line in body.splitlines() if line.startswith("data: ")]
        self.assertEqual(data.pop(), "[DONE]")
        choices = [json.loads(frame)["choices"][0] for frame in data]
        calls = [call for choice in choices for call in choice["delta"].get("tool_calls", [])]
        self.assertEqual([(c["index"], c["id"], c["function"]["name"]) for c in calls if "id" in c],
                         [(0, "call_0", "lookup_order"), (1, "call_1", "lookup_store")])
        self.assertEqual([(c["index"], c["function"]["arguments"]) for c in calls
                          if c["function"].get("arguments")], [(0, "{}"), (1, "{}")])
        self.assertEqual(choices[-1]["finish_reason"], "tool_calls")

    def test_responses_stream_preserves_namespaced_custom_tool_and_unwrapped_input(self):
        tool = {"type": "namespace", "name": "orders",
                "tools": [{"type": "custom", "name": "order_query"}]}
        raw_input = 'STATUS "17"\n查订单'
        arguments = json.dumps({"input": raw_input})
        first, rest = arguments[:10], arguments[10:]
        deltas = [{"tool_calls": [{"index": 0, "id": "call_query", "type": "function",
                                   "function": {"name": "order_query", "arguments": first}}]},
                  {"tool_calls": [{"index": 0, "function": {"arguments": rest}}]}]
        for hosted in (False, True):
            with self.subTest(hosted=hosted):
                declarations = [{"type": "web_search"}, tool] if hosted else [tool]
                payload = {"model": "test-model", "stream": True, "input": [
                    {"role": "user", "content": "Check my order"},
                    {"type": "additional_tools", "tools": declarations},
                ]}
                body, _ = self.post(payload, deltas, "tool_calls")
                events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
                item, = events[-1]["response"]["output"]
                self.assertEqual((item["type"], item["namespace"], item["input"]),
                                 ("custom_tool_call", "orders", raw_input))
                self.assertFalse(any(e["type"].startswith("response.function_call_arguments.") for e in events))
                delta, done = [e for e in events if e["type"].startswith("response.custom_tool_call_input.")]
                self.assertEqual(delta["delta"], raw_input)
                self.assertEqual(done["input"], raw_input)
                self.assertEqual(delta["item_id"], item["id"])
                self.assertEqual(done["item_id"], item["id"])

    def test_responses_stream_replays_final_reasoning_summary_with_hosted_tools(self):
        payload = {"model": "test-model", "input": "Summarize", "tools": [{"type": "web_search"}],
                   "stream": True}
        deltas = [{"reasoning_content": "Summary"}, {"content": "Answer"}]
        body, _ = self.post(payload, deltas, "stop")
        events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
        result = events[-1]["response"]
        self.assertEqual([item["type"] for item in result["output"]], ["reasoning", "message"])
        reasoning, message = result["output"]
        self.assertEqual(reasoning["summary"], [{"type": "summary_text", "text": "Summary"}])
        self.assertEqual(message["content"][0]["text"], "Answer")
        done, = (e for e in events if e["type"] == "response.reasoning_summary_text.done")
        self.assertEqual(done["item_id"], reasoning["id"])

    def test_hosted_responses_echo_the_effective_parallel_setting(self):
        hosted_tool = {"type": "web_search"}
        client_tool = {"type": "function", "name": "get_time", "parameters": {"type": "object"}}
        cases = [
            ([hosted_tool], {}, True),
            ([hosted_tool, client_tool], {}, False),
            ([hosted_tool, client_tool], {"parallel_tool_calls": False}, False),
            ([hosted_tool, client_tool], {"parallel_tool_calls": True}, True),
        ]
        for tools, options, expected in cases:
            with self.subTest(tools=len(tools), options=options):
                payload = dict(options, model="test-model", input="Say hi", tools=tools)
                deltas = [{"content": "Hello"}]
                body, request = self.post(payload, deltas, "stop")
                self.assertIs(request["parallel_tool_calls"], expected)
                self.assertIs(json.loads(body)["parallel_tool_calls"], expected)
                stream, request = self.post(dict(payload, stream=True), deltas, "stop")
                self.assertIs(request["parallel_tool_calls"], expected)
                events = [json.loads(line[6:]) for line in stream.splitlines() if line.startswith("data: ")]
                responses = [event["response"] for event in events if "response" in event]
                self.assertGreaterEqual(len(responses), 3)
                for response in responses:
                    self.assertIs(response["parallel_tool_calls"], expected)

    def test_hosted_responses_distinguish_requested_and_reported_models(self):
        alias = "test-model"
        payload = {"model": alias, "input": "Say hi", "tools": [{"type": "web_search"}]}
        deltas = [{"content": "Hello"}]
        for actual in (None, "reported-model"):
            with self.subTest(actual=actual):
                body, _ = self.post(payload, deltas, "stop", upstream_model=actual)
                stream, _ = self.post(dict(payload, stream=True), deltas, "stop", upstream_model=actual)
                events = [json.loads(line[6:]) for line in stream.splitlines() if line.startswith("data: ")]
                lifecycle = [event for event in events if "response" in event]
                self.assertEqual([event["type"] for event in lifecycle[:2]],
                                 ["response.created", "response.in_progress"])
                for event in lifecycle[:2]:
                    self.assertEqual(event["response"]["model"], "")
                    self.assertIsNone(event["response"]["resolved_model"])
                self.assertEqual(len(lifecycle), 4 if actual else 3)
                if actual:
                    self.assertEqual(lifecycle[2]["type"], "response.in_progress")
                    self.assertEqual(lifecycle[2]["response"]["model"], actual)
                self.assertEqual(lifecycle[-1]["type"], "response.completed")
                self.assertTrue(all(event["response"]["requested_model"] == alias for event in lifecycle))
                self.assertEqual({event["response"]["id"] for event in lifecycle},
                                 {lifecycle[0]["response"]["id"]})
                for response in (json.loads(body), events[-1]["response"]):
                    self.assertEqual(response["model"], actual or "")
                    self.assertEqual(response["resolved_model"], actual)
                    self.assertEqual(response["requested_model"], alias)

    def test_responses_incomplete_has_the_same_status_in_both_modes(self):
        deltas = [{"content": "Partial answer"}]
        for tools in ([], [{"type": "web_search"}]):
            for finish, reason in (("length", "max_output_tokens"), ("content_filter", "content_filter")):
                with self.subTest(tools=tools, finish=finish):
                    payload = {"model": "test-model", "input": "Summarize", "tools": tools}
                    body, _ = self.post(payload, deltas, finish)
                    aggregated = json.loads(body)
                    stream, _ = self.post(dict(payload, stream=True), deltas, finish)
                    events = [json.loads(line[6:]) for line in stream.splitlines() if line.startswith("data: ")]
                    self.assertEqual(events[-1]["type"], "response.incomplete")
                    self.assertNotIn("response.completed", [event["type"] for event in events])
                    for result in (aggregated, events[-1]["response"]):
                        self.assertEqual(result["status"], "incomplete")
                        self.assertEqual(result["incomplete_details"], {"reason": reason})
                        self.assertEqual(result["output_text"], "Partial answer")
                        self.assertEqual(result["output"][0]["status"], "incomplete")

    def test_hosted_calls_in_incomplete_round_are_not_dispatched(self):
        tools = [{"type": "web_search"},
                 {"type": "function", "name": "get_time", "parameters": {"type": "object"}}]
        payload = {"model": "test-model", "input": "Tell me the time", "tools": tools}
        hosted_call = dict(call("web_search", {"query": "time"}, id="call_search"), index=0)
        client_call = dict(call("get_time", {"zone": "UTC"}, id="call_time"), index=1)
        cases = [
            ("length", [hosted_call], "max_output_tokens", []),
            ("length", [hosted_call, client_call], "max_output_tokens", ["call_time"]),
            ("content_filter", [hosted_call, client_call], "content_filter", []),
        ]
        for finish, calls, reason, expected_call_ids in cases:
            with self.subTest(finish=finish, calls=len(calls)):
                deltas = [{"content": "Partial answer", "tool_calls": calls}]
                body, _ = self.post(payload, deltas, finish)
                stream, _ = self.post(dict(payload, stream=True), deltas, finish)
                events = [json.loads(line[6:]) for line in stream.splitlines() if line.startswith("data: ")]
                self.assertEqual(events[-1]["type"], "response.incomplete")
                for result in (json.loads(body), events[-1]["response"]):
                    self.assertEqual(result["status"], "incomplete")
                    self.assertEqual(result["incomplete_details"], {"reason": reason})
                    self.assertEqual(result["output_text"], "Partial answer")
                    self.assertEqual([item["call_id"] for item in result["output"]
                                      if item["type"] == "function_call"], expected_call_ids)

    def test_web_stream_requires_an_upstream_finish_reason(self):
        payload = {"model": "test-model", "input": "Summarize", "tools": [{"type": "web_search"}],
                   "stream": True}
        deltas = [{"content": "Partial answer"}]
        for finish, done, expected in ((None, False, "failed"), (None, True, "failed"),
                                       ("stop", False, "completed")):
            with self.subTest(finish=finish, done=done):
                body, _ = self.post(payload, deltas, finish, done=done)
                events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
                terminal = [e for e in events if e["type"] in
                            ("response.completed", "response.failed", "response.incomplete")]
                self.assertEqual([e["type"] for e in terminal], ["response." + expected])
                self.assertEqual(terminal[0]["response"]["id"], events[0]["response"]["id"])

    def test_hosted_inner_errors_fail_before_during_and_after_terminal(self):
        payload = {"model": "test-model", "input": "Summarize", "tools": [{"type": "web_search"}],
                   "metadata": {"tag": "preserved"}}
        partial = {"choices": [{"index": 0, "delta": {"content": "Partial answer"}, "finish_reason": None}]}
        stop = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        marker = "PRIVATE_FAKE_ERROR_BODY"
        error = {"error": {"code": "provider_error", "message": marker}}
        error_stop = dict(error, **stop)
        error_call = dict(error, choices=[{"index": 0, "delta": {"tool_calls": [
            dict(call("web_search", {"query": "probe"}), index=0)]}, "finish_reason": "tool_calls"}])
        cases = {"before": [error, partial, stop], "during": [partial, error_stop],
                 "after": [partial, stop, error], "tool_call": [error_call],
                 "string_error": [partial, {"error": marker}, stop]}
        for name, chunks in cases.items():
            for stream in (False, True):
                with self.subTest(name=name, stream=stream), mock.patch.object(P, "log") as logs:
                    # post asserts exactly one inference and zero tool executions.
                    body, _ = self.post(dict(payload, stream=stream), [], None, inner_chunks=chunks,
                                        expected_status=200 if stream else 502)
                    self.assertNotIn(marker, body)
                    self.assertNotIn(marker, str(logs.call_args_list))
                    if stream:
                        events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
                        self.assertEqual([e["type"] for e in events],
                                         ["response.created", "response.in_progress", "response.failed"])
                        final = events[-1]["response"]
                        self.assertEqual(final["id"], events[0]["response"]["id"])
                        self.assertEqual(final["status"], "failed")
                        self.assertEqual(final["metadata"], payload["metadata"])
                        self.assertEqual(final["error"]["message"], "upstream stream failed")
                    else:
                        self.assertEqual(json.loads(body)["error"]["message"], "upstream stream failed")
        # The same hosted orchestration is used by Chat Completions.
        chat_payload = dict(payload, messages=[{"role": "user", "content": "Summarize"}])
        for stream in (False, True):
            self.post(dict(chat_payload, stream=stream), [], None, "/v1/chat/completions",
                      inner_chunks=cases["during"], expected_status=502)

    def test_unflagged_inner_error_observation_is_unchanged(self):
        chunk = {"error": {"code": "fixture", "message": "ordinary-path-fixture"},
                 "choices": [{"index": 0, "delta": {"content": "Text"}, "finish_reason": "stop"}]}
        wire = ("data: " + json.dumps({"body": json.dumps(chunk)}) + "\n\n").encode()
        with mock.patch.object(P, "log"):
            result = P.aggregate_stream(io.BytesIO(wire), "test-model", holder={"usage": None})
        self.assertEqual(result["choices"][0]["message"]["content"], "Text")
        self.assertEqual(result["choices"][0]["finish_reason"], "stop")

    def test_upstream_socket_error_is_a_response_failure(self):
        class UpstreamReset(io.BytesIO):
            def __iter__(self):
                yield from iter(self.readline, b"")
                raise ConnectionResetError("simulated upstream peer reset")

        payload = {"model": "test-model", "input": "Summarize", "tools": [{"type": "web_search"}]}
        deltas = [{"content": "Partial answer"}]
        body, _ = self.post(dict(payload, stream=True), deltas, None,
                            done=False, stream_factory=UpstreamReset)
        events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
        self.assertEqual([e["type"] for e in events],
                         ["response.created", "response.in_progress", "response.failed"])
        self.assertEqual(events[-1]["response"]["status"], "failed")
        self.assertEqual(events[-1]["response"]["id"], events[0]["response"]["id"])
        self.post(payload, deltas, None, expected_status=502, done=False,
                  stream_factory=UpstreamReset)


if __name__ == "__main__":
    unittest.main()

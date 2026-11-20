"""Offline protocol, realm isolation and handler tests for official web tools."""
import contextlib
import hashlib
import io
import json
import os
import runpy
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_temp = tempfile.TemporaryDirectory(prefix="qd-official-web-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_temp.name, "accounts")
os.environ["QD_PROXY_USAGE_DIR"] = os.path.join(_temp.name, "usage")

import qoder_accounts as A
import qoder_official_web as O
import qoder_proxy as P
import qoder_webtools as W
from qoder_sign import qoder_decode
from qoder_web_response import WebEvidence

P.ACCOUNTS_DIR = os.environ["ACCOUNTS_DIR"]


def account(uid="test-one", realm="cn", vpc=False, gateway=None):
    return SimpleNamespace(
        uid=uid, access_token="test-secret-" + realm + "-" + uid, realm=realm,
        realm_config=lambda: {
            "gateway": gateway if gateway is not None else (
                "https://api3.qoder.sh" if realm == "intl" else "https://gateway.qoder.com.cn"),
            "vpc_instance": "private-instance" if vpc else "",
        },
    )


def page_items():
    return {"pageItems": [{"title": "Docs", "link": "https://example.com/docs",
                            "snippet": "Snippet", "publishedTime": "2026-11-16"}]}


class OfficialWebTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"QD_WEB_TOOLS_BACKEND": "qoder"})
        self.env.start()
        self.transport = mock.patch.object(A, "http_json", return_value=page_items())
        self.http = self.transport.start()
        self.addCleanup(self.transport.stop)
        self.addCleanup(self.env.stop)
        self.acc = account()

    def test_signed_wire_format(self):
        result = O.search(self.acc, "中文 query")
        self.assertTrue(result["ok"])
        url = self.http.call_args.args[0]
        kwargs = self.http.call_args.kwargs
        self.assertEqual(url, "https://gateway.qoder.com.cn" + O.SEARCH_PATH + "?Encode=1")
        envelope = json.loads(qoder_decode(kwargs["data"].decode("ascii")))
        self.assertEqual(envelope["encodeVersion"], "1")
        payload = json.loads(envelope["payload"])
        self.assertEqual(payload, {
            "query": "中文 query", "timeRange": "NoLimit",
            "contents": {"mainText": False, "markdownText": False, "summary": False},
        })
        headers = kwargs["headers"]
        bearer = headers["authorization"]
        self.assertTrue(bearer.startswith("Bearer COSY."))
        prefix, signature = bearer.rsplit(".", 1)
        payload_b64 = prefix[len("Bearer COSY."):]
        signed = "\n".join([payload_b64, headers["cosy-key"], headers["cosy-date"],
                            kwargs["data"].decode("ascii"), O.SEARCH_PATH[5:]])
        self.assertEqual(signature, hashlib.md5(signed.encode()).hexdigest())
        self.assertEqual(headers["cosy-user"], self.acc.uid)
        self.assertEqual(headers["accept"], "application/json")
        self.assertNotIn("x-model-key", headers)
        self.assertNotIn(self.acc.access_token, str(kwargs))
        self.assertEqual(kwargs["retries"], 1)
        self.assertEqual(kwargs["timeout"], 15)
        self.assertIs(kwargs["follow_redirects"], False)

    def test_signed_transport_installs_no_redirect_policy(self):
        opener = A._opener_for_request(follow_redirects=False)
        handlers = [h for h in opener.handlers if isinstance(h, urllib.request.HTTPRedirectHandler)]
        self.assertEqual(len(handlers), 1)
        self.assertIsInstance(handlers[0], A._NoRedirectHandler)
        self.transport.stop()
        response = io.BytesIO(json.dumps(page_items()).encode("utf-8"))
        with mock.patch.object(A, "_opener_for_request", return_value=opener) as build, \
                mock.patch.object(opener, "open", return_value=response), \
                mock.patch.object(A, "validate_public_http_url"):
            self.assertTrue(O.search(self.acc, "test")["ok"])
        build.assert_called_once_with(follow_redirects=False)

    def test_request_cache_separates_official_origins_and_account_identity(self):
        evidence = WebEvidence()
        config = {"backend": "qoder"}

        def lookup(acc):
            results = []
            W.run_tool("web_search", {"query": "docs"}, account=acc, context=evidence,
                       on_result=results.append, config=config)
            self.assertTrue(results[0]["ok"])

        other_realm = account(self.acc.uid, realm="intl")
        other_realm.access_token = self.acc.access_token
        other_origin = account(self.acc.uid, gateway="https://other.example")
        for count, acc in enumerate((self.acc, other_realm, other_origin), start=1):
            lookup(acc)
            lookup(acc)
            self.assertEqual(self.http.call_count, count)
        other_origin.access_token = "refreshed-test-token"
        lookup(other_origin)
        lookup(other_origin)
        self.assertEqual(self.http.call_count, 4)
        self.assertNotIn(other_origin.access_token, repr(evidence.cache))

    def test_missing_other_realm_and_vpc_rejected_without_network(self):
        for acc in [None, account(realm="unknown"), account(vpc=True),
                    account(realm="intl", vpc=True),
                    SimpleNamespace(access_token="")]:
            with self.subTest(acc=acc):
                self.assertFalse(O.search(acc, "query")["ok"])
        self.http.assert_not_called()

    def test_intl_search_uses_center_and_signs_encoded_body(self):
        acc = account(realm="intl", gateway="https://inference.example.com")
        self.assertTrue(O.search(acc, "query")["ok"])
        url = self.http.call_args.args[0]
        kwargs = self.http.call_args.kwargs
        self.assertEqual(url, "https://center.qoder.sh" + O.SEARCH_PATH + "?Encode=1")
        envelope = json.loads(qoder_decode(kwargs["data"].decode("ascii")))
        self.assertEqual(json.loads(envelope["payload"])["query"], "query")
        headers = kwargs["headers"]
        prefix, signature = headers["authorization"].rsplit(".", 1)
        signed = "\n".join([prefix[len("Bearer COSY."):], headers["cosy-key"],
                            headers["cosy-date"], kwargs["data"].decode("ascii"),
                            O.SEARCH_PATH[5:]])
        self.assertEqual(signature, hashlib.md5(signed.encode()).hexdigest())
        self.assertEqual(headers["cosy-user"], acc.uid)

    def test_cn_gateway_scope_is_preserved(self):
        self.assertTrue(O.search(account(gateway="https://cn-gateway.example.com/"), "query")["ok"])
        self.assertEqual(self.http.call_args.args[0],
                         "https://cn-gateway.example.com" + O.SEARCH_PATH + "?Encode=1")

    def test_invalid_cn_gateway_rejected_before_signing_or_transport(self):
        for origin in ("http://gateway.qoder.com.cn", "https://user:pass@example.com",
                       "https://example.com/path", "https://example.com?query=1", ""):
            with self.subTest(origin=origin), mock.patch.object(O.SESSIONS, "get") as signer:
                self.assertFalse(O.search(account(gateway=origin), "query")["ok"])
                signer.assert_not_called()
        self.http.assert_not_called()

    def test_same_uid_in_different_realms_keeps_the_callers_credentials(self):
        for realm, origin in (("cn", "https://gateway.qoder.com.cn"),
                              ("intl", "https://center.qoder.sh"),
                              ("cn", "https://gateway.qoder.com.cn")):
            acc = account("shared-uid", realm=realm)
            with self.subTest(realm=realm), mock.patch.object(O.SESSIONS, "get", wraps=O.SESSIONS.get) as signer:
                self.assertTrue(O.search(acc, "query")["ok"])
                signer.assert_called_once_with(acc)
                self.assertEqual(self.http.call_args.args[0], origin + O.SEARCH_PATH + "?Encode=1")
                self.assertEqual(O.SESSIONS.get(acc).access_token, acc.access_token)

    def test_intl_auth_failure_has_no_cross_realm_or_local_fallback(self):
        acc = account(realm="intl")
        self.http.side_effect = urllib.error.HTTPError(
            "https://center.qoder.sh/x", 401, acc.access_token, {},
            io.BytesIO(acc.access_token.encode()))
        with mock.patch.object(W, "search") as local:
            output = W.run_tool("web_search", '{"query":"test"}', account=acc)
        self.assertIn("HTTP 401", output)
        self.assertNotIn(acc.access_token, output)
        self.assertEqual(self.http.call_count, 1)
        self.assertEqual(self.http.call_args.args[0], "https://center.qoder.sh" + O.SEARCH_PATH + "?Encode=1")
        local.assert_not_called()

    def test_intl_fetch_uses_center_and_original_account(self):
        acc = account(realm="intl")
        self.http.return_value = {"results": [{"statusCode": 200, "markdown": "Readable page"}]}
        with mock.patch.object(W, "check_url", return_value=(True, "")):
            result = O.fetch(acc, "https://example.com/page")
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], "Readable page")
        self.assertEqual(self.http.call_args.args[0], "https://center.qoder.sh" + O.FETCH_PATH + "?Encode=1")
        self.assertEqual(self.http.call_args.kwargs["headers"]["cosy-user"], acc.uid)

    def test_query_validation_before_network(self):
        for query in ["", "   ", "x" * (O.MAX_QUERY_CHARS + 1)]:
            self.assertFalse(O.search(self.acc, query)["ok"])
        self.http.assert_not_called()

    def test_results_normalized_deduplicated_filtered_and_bounded(self):
        bad = [None, "bad", {"link": "http://127.0.0.1/x"},
               {"link": "http://localhost/x"}, {"link": "http://user:pass@example.com"},
               {"link": "file:///etc/passwd"}, {"link": "https://example.com:bad/x"}]
        good = [{"title": "x" * 2000, "snippet": "y" * 5000,
                 "link": "https://example.com/%s" % i} for i in range(10)]
        self.http.return_value = {"pageItems": bad + [good[0]] + good}
        result = O.search(self.acc, "query", limit=100)
        self.assertEqual(len(result["results"]), 5)
        self.assertEqual(len({r["url"] for r in result["results"]}), 5)
        self.assertEqual(len(result["results"][0]["title"]), 1000)
        self.assertEqual(len(result["results"][0]["snippet"]), 1000)

    def test_empty_results_are_not_a_transport_error(self):
        self.http.return_value = {"pageItems": []}
        self.assertEqual(O.search(self.acc, "query")["results"], [])
        self.assertTrue(O.search(self.acc, "query")["ok"])

    def test_response_errors_not_successful_searches(self):
        for response in [[], None, {}, {"pageItems": None},
                         {"errorCode": 401, "pageItems": []},
                         {"success": False, "pageItems": []}]:
            self.http.return_value = response
            with self.subTest(response=response):
                self.assertFalse(O.search(self.acc, "query")["ok"])

    def test_failure_does_not_leak_tokens_or_fall_back(self):
        self.http.side_effect = urllib.error.HTTPError(
            "https://gateway.qoder.com.cn/x", 401, self.acc.access_token, {},
            io.BytesIO(self.acc.access_token.encode()))
        with mock.patch.object(W, "search") as local:
            output = W.run_tool("web_search", '{"query":"test"}', account=self.acc)
        self.assertIn("HTTP 401", output)
        self.assertNotIn(self.acc.access_token, output)
        local.assert_not_called()
        self.assertEqual(self.http.call_count, 1)

    def test_network_error_is_sanitized(self):
        self.http.side_effect = RuntimeError(self.acc.access_token)
        result = O.search(self.acc, "query")
        self.assertFalse(result["ok"])
        self.assertNotIn(self.acc.access_token, result["error"])

    def test_account_identity_not_selected_from_another_pool(self):
        first = account("first")
        second = account("second")
        O.search(first, "query")
        self.assertEqual(self.http.call_args.kwargs["headers"]["cosy-user"], "first")
        O.search(second, "query")
        self.assertEqual(self.http.call_args.kwargs["headers"]["cosy-user"], "second")

    def test_fetch_request_and_markdown_bounding(self):
        self.http.return_value = {"results": [{
            "statusCode": 200, "markdown": "m" * 50000, "text": "unused",
            "metadata": {"title": "Page", "redirectedUrl": "https://example.com/final"},
        }]}
        with mock.patch.object(W, "check_url", return_value=(True, "")):
            result = O.fetch(self.acc, "https://example.com/start")
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["text"]), 20000)
        self.assertEqual(result["title"], "Page")
        url = self.http.call_args.args[0]
        self.assertIn(O.FETCH_PATH, url)
        envelope = json.loads(qoder_decode(self.http.call_args.kwargs["data"].decode()))
        self.assertEqual(json.loads(envelope["payload"]), {
            "url": "https://example.com/start", "maxAge": 0,
            "timeout": 14000, "formats": ["markdown"],
        })

    def test_fetch_private_targets_rejected_without_network(self):
        for url in ["http://127.0.0.1/", "http://10.0.0.1/",
                    "http://169.254.169.254/", "http://[::1]/", "http://localhost/",
                    "https://user:password@example.com", "file:///etc/passwd",
                    "https://example.com\\@127.0.0.1/"]:
            self.assertFalse(O.fetch(self.acc, url)["ok"], url)
        with mock.patch.object(W, "check_url", return_value=(False, "host resolves to a non-public address")):
            self.assertFalse(O.fetch(self.acc, "https://example.com/x")["ok"])
        self.http.assert_not_called()

    def test_fetch_redirect_private_content_not_returned(self):
        self.http.return_value = {"results": [{"statusCode": 200, "markdown": "secret",
            "metadata": {"redirectedUrl": "http://127.0.0.1/secret"}}]}
        with mock.patch.object(W, "check_url", return_value=(True, "")):
            result = O.fetch(self.acc, "https://example.com")
        self.assertFalse(result["ok"])
        self.assertNotIn("secret", str(result))

    def test_fetch_rejects_error_status_or_missing_content(self):
        for response in [{"results": []}, {"results": [None]},
                         {"results": [{"statusCode": 403, "markdown": "denied"}]},
                         {"results": [{"errorCode": 1, "markdown": "denied"}]},
                         {"results": [{"statusCode": "200", "markdown": "bad"}]},
                         {"results": [{"statusCode": 200, "markdown": " "}]}]:
            self.http.return_value = response
            with mock.patch.object(W, "check_url", return_value=(True, "")):
                self.assertFalse(O.fetch(self.acc, "https://example.com")["ok"])

    def test_local_is_still_default(self):
        with mock.patch.dict(os.environ, {"QD_WEB_TOOLS_BACKEND": ""}), \
                mock.patch.object(W, "search", return_value={"ok": True, "engine": "stub", "results": []}) as local:
            W.run_tool("web_search", '{"query":"hi"}')
        local.assert_called_once_with("hi")
        self.http.assert_not_called()

    def test_unknown_backend_does_not_silently_use_local(self):
        with mock.patch.dict(os.environ, {"QD_WEB_TOOLS_BACKEND": "typo"}):
            with self.assertRaises(ValueError):
                W.run_tool("web_search", '{"query":"hi"}', self.acc)
        self.http.assert_not_called()

    def _assert_handler_passes_inference_account_and_refeeds_result(self, realm):
        self.acc = account(realm=realm)
        call = {"id": "call_search", "type": "function", "function": {
            "name": "web_search", "arguments": '{"query":"test"}'}}
        responses = [{"choices": [{"message": {"content": "", "tool_calls": [call]}}]},
                     {"choices": [{"message": {"content": "Done"}}]}]
        requests = []

        def open_upstream(req, **kwargs):
            requests.append(req)
            return contextlib.nullcontext(), self.acc, None

        handler = object.__new__(P.Handler)
        handler.headers = {}
        handler.key_entry = None
        handler._model_precheck = lambda *args: None
        handler._request_realm = lambda: realm
        handler._key_id_for_usage = lambda: "test-key"
        handler._json = lambda status, result: (status, result)
        with mock.patch.object(P, "open_upstream", side_effect=open_upstream), \
                mock.patch.object(P, "aggregate_stream", side_effect=responses):
            status, result = handler._handle_web_tools(
                "/v1/chat/completions", {"model": "test", "messages": [],
                    "tools": [{"type": "web_search"}]}, {"web_search"})
        self.assertEqual(status, 200)
        self.assertEqual(len(requests), 2)
        tools = [m for m in requests[1]["messages"] if m["role"] == "tool"]
        self.assertEqual(tools[0]["tool_call_id"], "call_search")
        self.assertIn("Search results (qoder; snippets)", tools[0]["content"])
        self.assertIn("https://example.com/docs", tools[0]["content"])
        self.assertEqual(self.http.call_args.kwargs["headers"]["cosy-user"], self.acc.uid)
        self.assertEqual(result["choices"][0]["message"]["content"], "Done")

    def test_real_handler_passes_inference_account_and_refeeds_result(self):
        for realm, origin in (("cn", "https://gateway.qoder.com.cn"),
                              ("intl", "https://center.qoder.sh")):
            with self.subTest(realm=realm):
                self._assert_handler_passes_inference_account_and_refeeds_result(realm)
                self.assertEqual(self.http.call_args.args[0], origin + O.SEARCH_PATH + "?Encode=1")

    def test_live_probe_selects_realm_without_exposing_credentials(self):
        probe = runpy.run_path(os.path.join(ROOT, "scripts", "probe_official_web.py"))
        with tempfile.TemporaryDirectory() as tmp:
            for realm in ("cn", "intl"):
                with open(os.path.join(tmp, realm + ".json"), "w", encoding="utf-8") as f:
                    json.dump({"uid": "probe-" + realm, "realm": realm,
                               "accessToken": "private-probe-token-" + realm}, f)
            for realm in ("cn", "intl"):
                with self.subTest(realm=realm), mock.patch.object(
                        O, "search", return_value={"ok": True, "results": []}) as search:
                    stdout = io.StringIO()
                    with mock.patch.object(sys, "argv", ["probe", "--accounts-dir", tmp,
                            "--realm", realm, "--query", "test"]), contextlib.redirect_stdout(stdout):
                        self.assertEqual(probe["main"](), 0)
                    selected = search.call_args.args[0]
                    self.assertEqual(selected.realm, realm)
                    self.assertEqual(selected.access_token, "private-probe-token-" + realm)
                    self.assertEqual(json.loads(stdout.getvalue())["realm"], realm)
                    self.assertNotIn(selected.access_token, stdout.getvalue())
                    self.assertNotIn(selected.uid, stdout.getvalue())

    def test_live_probe_keeps_ambiguous_accounts_explicit(self):
        probe = runpy.run_path(os.path.join(ROOT, "scripts", "probe_official_web.py"))
        with tempfile.TemporaryDirectory() as tmp:
            for realm in ("cn", "intl"):
                with open(os.path.join(tmp, realm + ".json"), "w", encoding="utf-8") as f:
                    json.dump({"uid": realm, "realm": realm, "accessToken": "fake-token"}, f)
            with mock.patch.object(sys, "argv", ["probe", "--accounts-dir", tmp,
                    "--query", "test"]), contextlib.redirect_stderr(io.StringIO()), \
                    mock.patch.object(O, "search") as search, self.assertRaises(SystemExit) as exc:
                probe["main"]()
            self.assertEqual(exc.exception.code, 2)
            search.assert_not_called()


if __name__ == "__main__":
    unittest.main()

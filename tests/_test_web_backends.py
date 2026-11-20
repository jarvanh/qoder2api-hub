"""Panel persistence/auth, external HTTP contracts and credential isolation."""
import contextlib
import http.server
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_tmp = tempfile.TemporaryDirectory(prefix="qd-web-backends-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_tmp.name, "initial")
os.environ["QD_PROXY_USAGE_DIR"] = os.path.join(_tmp.name, "usage")
import qoder_external_web as E
import qoder_proxy as P
import qoder_settings as S
import qoder_webtools as W
import qoder_web_response as R

P.ACCOUNTS_DIR = os.environ["ACCOUNTS_DIR"]


class FakeSearch(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.handle_search()

    def do_POST(self):
        self.handle_search()

    def handle_search(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        self.server.requests.append({"path": self.path, "method": self.command,
                                     "headers": dict(self.headers), "body": body})
        self.send_response(302 if self.path.startswith("/redirect") else 200)
        if self.path.startswith("/redirect"):
            self.send_header("Location", self.server.url + "/unexpected")
        self.end_headers()
        self.wfile.write(self.server.payload)

    def log_message(self, *args):
        pass


class WebBackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.search_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeSearch)
        cls.search_server.url = "http://127.0.0.1:%s" % cls.search_server.server_port
        cls.thread = threading.Thread(target=cls.search_server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.search_server.shutdown()
        cls.search_server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="qd-web-settings-")
        self.addCleanup(self.directory.cleanup)
        self.path = self.directory.name
        self.env = mock.patch.dict(os.environ)
        self.env.start(); self.addCleanup(self.env.stop)
        os.environ.pop("QD_WEB_TOOLS_BACKEND", None)
        os.environ.pop("QD_WEB_TOOLS", None)
        os.environ.pop("QD_WEB_TOOLS_PROXY", None)
        self.patch = mock.patch.object(P, "ACCOUNTS_DIR", self.path)
        self.patch.start(); self.addCleanup(self.patch.stop)
        self.search_server.requests = []
        self.search_server.payload = json.dumps({"results": [{"title": "<b>Paper</b>",
            "url": "https://example.com/paper", "content": "Abstract"}]}).encode()

    def profile(self, provider="searxng", **kwargs):
        profile = E.defaults(provider)
        profile["endpoint"] = self.search_server.url + "/search"
        profile.update(kwargs)
        return profile

    def test_three_backends_persist_without_changing_enable_on_partial_save(self):
        S.set_web_tools_config(self.path, {"enabled": True})
        for backend in ("qoder", "local"):
            S.set_web_tools_config(self.path, {"backend": backend})
            self.assertEqual(W.backend_name(self.path), backend)
            self.assertTrue(S.web_tools_enabled(self.path))
        S.set_web_tools_config(self.path, {"backend": "external", "external": self.profile()})
        self.assertEqual(W.backend_config(self.path, ready=True)["backend"], "external")

    def test_failed_validation_is_atomic(self):
        S.set_web_tools_config(self.path, {"enabled": True, "backend": "qoder"})
        before = S.load(self.path)
        for update in [{"enabled": "false"}, {"backend": "wrong"}, {"enabled": False, "external": {"provider": "custom", "endpoint": "file:///secret"}},
                       {"backend": "external", "external": {"provider": "tavily"}}]:
            with self.assertRaises(ValueError):
                S.set_web_tools_config(self.path, update)
            self.assertEqual(S.load(self.path), before)

    def test_provider_keys_are_separate_omission_retains_and_empty_clears(self):
        S.set_web_tools_config(self.path, {"external": {"provider": "tavily", "api_key": "fake-tavily"}})
        S.set_web_tools_config(self.path, {"external": {"provider": "brave", "api_key": "fake-brave"}})
        S.set_web_tools_config(self.path, {"external": {"provider": "tavily"}})
        self.assertEqual(S.web_tools_config(self.path)["external"]["api_key"], "fake-tavily")
        public = P.web_tools_settings_view()
        self.assertNotIn("fake-tavily", json.dumps(public))
        self.assertNotIn("fake-brave", json.dumps(public))
        self.assertTrue(public["web_tools_external"]["api_key_set"])
        S.set_web_tools_config(self.path, {"external": {"provider": "tavily", "api_key": ""}})
        cfg = S.web_tools_config(self.path)
        self.assertEqual(cfg["external"]["api_key"], "")
        self.assertEqual(cfg["external_profiles"]["brave"]["api_key"], "fake-brave")

    def test_environment_precedence_is_visible(self):
        S.set_web_tools_config(self.path, {"enabled": True, "backend": "local"})
        os.environ["QD_WEB_TOOLS_BACKEND"] = "qoder"
        os.environ["QD_WEB_TOOLS"] = "off"
        self.assertEqual(W.backend_name(self.path), "qoder")
        self.assertFalse(P.web_tools_enabled())
        self.assertEqual(P.web_tools_settings_view()["web_tools_backend_env"], "qoder")

    def test_searxng_get_contract_and_query_cannot_replace_endpoint(self):
        query = 'site:openreview.net "LoRA" &url=http://127.0.0.1/secret'
        result = E.search(self.profile(), query)
        self.assertTrue(result["ok"])
        req = self.search_server.requests[0]
        args = urllib.parse.parse_qs(urllib.parse.urlsplit(req["path"]).query)
        self.assertEqual(req["method"], "GET")
        self.assertEqual(args["q"], [query])
        self.assertEqual(args["format"], ["json"])
        self.assertTrue(req["path"].startswith("/search?"))
        self.assertEqual(result["results"][0]["title"], "Paper")

    def test_custom_post_nested_mapping_auth_and_fixed_params(self):
        self.search_server.payload = json.dumps({"data": {"items": [{"meta": {"title": "Title"},
            "link": "https://example.com/x", "summary": "Useful"}]}}).encode()
        config = self.profile("custom", method="POST", query_field="query", limit_field="limit",
            results_path="data.items", title_field="meta.title", url_field="link", snippet_field="summary",
            params={"language": "en"}, api_key="fake-secret")
        result = E.search(config, "research")
        self.assertTrue(result["ok"])
        req = self.search_server.requests[0]
        self.assertEqual(json.loads(req["body"]), {"language": "en", "query": "research", "limit": 5})
        self.assertEqual(req["headers"].get("Authorization"), "Bearer fake-secret")
        self.assertEqual(result["results"][0]["snippet"], "Useful")

    def test_cloud_presets_send_correct_fields_and_parse_provider_shapes(self):
        for provider, field, response, auth in [
            ("tavily", "query", {"results": [{"title": "T", "url": "https://example.com/x", "content": "S"}]}, "Authorization"),
            ("brave", "q", {"web": {"results": [{"title": "T", "url": "https://example.com/x", "description": "S"}]}}, "X-Subscription-Token"),
            ("serper", "q", {"organic": [{"title": "T", "link": "https://example.com/x", "snippet": "S"}]}, "X-API-KEY")]:
            self.search_server.payload = json.dumps(response).encode()
            result = E.search(self.profile(provider, api_key="fake-provider-key"), "query")
            self.assertTrue(result["ok"], provider)
            req = self.search_server.requests[-1]
            args = json.loads(req["body"]) if req["method"] == "POST" else urllib.parse.parse_qs(urllib.parse.urlsplit(req["path"]).query)
            self.assertIn(field, args)
            self.assertTrue(any(k.lower() == auth.lower() for k in req["headers"]))
            self.assertEqual(result["results"][0]["snippet"], "S")

    def test_redirect_does_not_forward_secret_or_make_second_request(self):
        profile = self.profile(api_key="must-not-leak")
        profile["endpoint"] = self.search_server.url + "/redirect"
        result = E.search(profile, "query")
        self.assertFalse(result["ok"])
        self.assertEqual(len(self.search_server.requests), 1)
        self.assertNotIn("must-not-leak", json.dumps(result))
        self.assertIn("HTTP 302", result["error"])

    def test_invalid_json_and_large_response_fail_without_echoing_body(self):
        for body in [b'{"secret":"do-not-echo" BROKEN', b"x" * (2 * 1024 * 1024 + 1)]:
            self.search_server.payload = body
            result = E.search(self.profile(), "query")
            self.assertFalse(result["ok"])
            self.assertNotIn("do-not-echo", json.dumps(result))

    def test_root_array_mapping_and_private_result_filtering(self):
        self.search_server.payload = json.dumps([
            {"title": "private", "url": "http://127.0.0.1/admin"},
            {"title": "public", "url": "https://example.com/x", "snippet": "S"},
            {"title": "duplicate", "url": "https://example.com/x"}]).encode()
        result = E.search(self.profile("custom", results_path=""), "query")
        self.assertEqual([x["title"] for x in result["results"]], ["public"])

    def test_bad_configuration_rejected_before_network(self):
        for config in [self.profile(endpoint="file:///secret"),
                       dict(self.profile(), endpoint="http://user:pass@localhost/"),
                       self.profile(auth_header="Host"), self.profile(api_key="key\r\nInjected: value"),
                       self.profile(params=[]), self.profile(method="DELETE")]:
            result = E.search(config, "query")
            self.assertFalse(result["ok"])
        self.assertEqual(self.search_server.requests, [])

    def test_snapshot_pins_backend_and_domain_filters_still_apply(self):
        self.search_server.payload = json.dumps({"results": [
            {"url": "https://example.com/x", "title": "A", "content": "S"},
            {"url": "https://outside.test/x", "title": "B"}]}).encode()
        S.set_web_tools_config(self.path, {"enabled": True, "backend": "external", "external": self.profile()})
        config = W.backend_config(self.path, ready=True)
        S.set_web_tools_config(self.path, {"backend": "qoder"})
        context = R.WebEvidence([{"type": "web_search", "filters": {"allowed_domains": ["example.com"]}}])
        text = W.run_tool("web_search", {"query": "research"}, context=context, config=config)
        self.assertIn("external/searxng", text)
        self.assertIn("https://example.com/x", text)
        self.assertNotIn("outside.test", text)
        self.assertEqual(len(self.search_server.requests), 1)

    def test_external_fetch_keeps_local_ssrf_guard(self):
        config = {"backend": "external", "external": self.profile()}
        result = W.run_tool("web_fetch", {"url": "http://127.0.0.1/secret"}, config=config)
        self.assertIn("error", result)
        self.assertEqual(self.search_server.requests, [])

    def test_real_panel_http_auth_save_and_legacy_toggle(self):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), P.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        token = P.PANEL.create()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def call(body, authenticated=True):
            headers = {"Content-Type": "application/json"}
            if authenticated:
                headers["X-Panel-Token"] = token
            req = urllib.request.Request("http://127.0.0.1:%s/settings/web-tools" % server.server_port,
                data=json.dumps(body).encode(), headers=headers)
            try:
                with opener.open(req, timeout=5) as response:
                    return response.status, json.load(response)
            except urllib.error.HTTPError as exc:
                return exc.code, json.load(exc)
        status, _ = call({"backend": "qoder"}, False)
        self.assertEqual(status, 401)
        status, data = call({"enabled": True, "backend": "external", "external": self.profile(api_key="private-test-key")})
        self.assertEqual(status, 200)
        self.assertTrue(data["web_tools"])
        self.assertEqual(data["web_tools_backend"], "external")
        self.assertNotIn("private-test-key", json.dumps(data))
        status, data = call({"enabled": False})
        self.assertEqual(status, 200)
        self.assertFalse(data["web_tools"])
        self.assertEqual(data["web_tools_backend"], "external")
        self.assertEqual(call({"enabled": "false"})[0], 400)


if __name__ == "__main__":
    unittest.main()

"""The panel setting only removes the Qoder template prompt from inference."""
import http.client
import io
import json
import os
import sys
import tempfile
import threading
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import bootstrap, check, declare_suite, finish

P, A = bootstrap()
declare_suite(__file__, ["qoder_proxy.py", "qoder_settings.py"])

USER = {"role": "user", "content": "Hello"}
TEMPLATE = {"role": "system", "content": "Qoder template prompt"}
CLIENT = "Client system prompt"
STARTUP = "You are a helpful assistant."
SETTING = "omit_default_system_prompt"
MODEL = "Qwen3.8-Flash"


class Handler(P.Handler):
    def _panel_ok(self):
        return self.headers.get("X-Test-Panel") == "panel"

    def log_message(self, *args):
        pass


def upstream_events():
    for delta, reason in (({"content": "Hi"}, None), ({}, "stop")):
        chunk = {"choices": [{"index": 0, "delta": delta, "finish_reason": reason}]}
        envelope = {"statusCodeValue": 200, "body": json.dumps(chunk)}
        yield ("data: " + json.dumps(envelope) + "\n\n").encode()
    yield b'data: {"body":"[DONE]"}\n\n'


with tempfile.TemporaryDirectory() as directory:
    account = A.Account({"uid": "fixture", "realm": "cn", "accessToken": "fake"})
    pool = A.AccountPool(directory, log=lambda *args, **kwargs: None)
    pool.accounts = [account]
    template = dict(P.BASEPROMPT, messages=[TEMPLATE, {"role": "user", "content": "example"}])
    captured = []

    def open_fake(payload, **kwargs):
        body = P.build_qoder_body(payload, account, MODEL, realm="cn")
        captured.append(body["messages"])
        return io.BytesIO(b"".join(upstream_events())), account, MODEL

    with mock.patch.multiple(
        P, POOL=pool, ACCOUNTS_DIR=directory, API_KEY="fixture-key",
        API_KEY_FILE_SET=False, SYSTEM_PROMPT=None, BASEPROMPT=template,
    ), mock.patch.object(P, "open_upstream", side_effect=open_fake), \
            mock.patch.object(P, "record_usage"), mock.patch.object(P, "record_error"):
        server = P.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()

        def request(path, body=None, panel=False):
            headers = {
                "Authorization": "Bearer fixture-key",
                "x-api-key": "fixture-key",
                "Content-Type": "application/json",
            }
            if panel:
                headers["X-Test-Panel"] = "panel"
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                connection.request("GET" if body is None else "POST", path,
                                   None if body is None else json.dumps(body), headers)
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()

        protocols = (
            ("/v1/chat/completions", {"messages": [USER]},
             {"messages": [{"role": "developer", "content": CLIENT}, USER]}),
            ("/v1/responses", {"input": "Hello"}, {"input": "Hello", "instructions": CLIENT}),
            ("/v1/messages", {"messages": [USER]}, {"messages": [USER], "system": CLIENT}),
        )

        def assert_prompts(expected, with_client_prompt=False):
            for path, plain, custom in protocols:
                captured.clear()
                status, response = request(
                    path, dict(custom if with_client_prompt else plain, model=MODEL, max_tokens=64))
                check(path + " sends the expected prompt", status == 200 and captured == [expected],
                      (status, captured, response.get("error")))

        try:
            status, settings = request("/settings", panel=True)
            check("missing setting defaults to keeping the template", status == 200
                  and settings.get(SETTING) is False)
            assert_prompts([TEMPLATE, USER])

            status, _ = request("/settings/save", {SETTING: True})
            check("an inference key cannot change the setting", status == 401)
            status, _ = request("/settings/save", {SETTING: "true"}, panel=True)
            check("settings endpoint rejects a string", status == 400
                  and P.qoder_settings.omit_default_system_prompt(directory) is False)
            status, settings = request("/settings/save", {SETTING: True}, panel=True)
            check("panel saves and returns the opt-in", status == 200 and settings.get(SETTING) is True
                  and P.qoder_settings.load(directory).get(SETTING) is True)

            assert_prompts([USER])
            assert_prompts([{"role": "system", "content": CLIENT}, USER], with_client_prompt=True)
            with mock.patch.object(P, "SYSTEM_PROMPT", STARTUP):
                assert_prompts([{"role": "system", "content": STARTUP}, USER])
                assert_prompts([{"role": "system", "content": CLIENT}, USER], with_client_prompt=True)

            kimi = P.build_qoder_body({
                "model": "Kimi", "messages": [USER],
                "tools": [{"type": "function", "function": {
                    "name": "lookup", "parameters": {"type": "object"},
                }}],
            }, account, "kmodel_latest", realm="cn")
            check("Kimi keeps the tool protocol without the Qoder template",
                  kimi["messages"] == [{"role": "system", "content": P.KIMI_TOOL_PROTOCOL_PROMPT}, USER])

            status, settings = request("/settings/save", {SETTING: False}, panel=True)
            check("turning the setting off restores the default", status == 200
                  and settings.get(SETTING) is False)
            assert_prompts([TEMPLATE, USER])
        finally:
            server.shutdown()
            server.server_close()
            worker.join(3)

with tempfile.TemporaryDirectory() as directory:
    from pathlib import Path
    import builtins
    settings = Path(directory) / "settings.json"
    saved = '{"api_keys": [{"key": "fixture-only"}], "unknown": [1, 2]}'
    settings.write_text(saved, encoding="utf-8")
    with mock.patch.object(builtins, "open", side_effect=PermissionError("fixture read failure")):
        try:
            P.qoder_settings.set_omit_default_system_prompt(directory, True)
        except PermissionError:
            check("unreadable settings reject the write", True)
        else:
            check("unreadable settings reject the write", False)
    check("read failure leaves unrelated config byte-for-byte intact",
          settings.read_text(encoding="utf-8") == saved)
    for raw in ("{unfinished", "[1, 2]"):
        settings.write_text(raw, encoding="utf-8")
        try:
            P.qoder_settings.set_omit_default_system_prompt(directory, True)
        except OSError:
            check("corrupt or non-object settings remain intact", settings.read_text(encoding="utf-8") == raw)
        else:
            check("corrupt or non-object settings reject the write", False)
    settings.write_text(saved, encoding="utf-8")
    P.qoder_settings.set_omit_default_system_prompt(directory, True)
    check("healthy settings preserve unrelated fields", P.qoder_settings.load(directory)
          == dict(json.loads(saved), omit_default_system_prompt=True))
finish()

"""Independent account VPC implementation: strict routing, compatibility and isolation."""
import contextlib
import gc
import http.server
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import ROOT, bootstrap, check, declare_suite, finish
os.environ.pop("QD_CN_VPC_ENDPOINT", None)
P, A = bootstrap()
declare_suite(__file__, ["qoder_accounts.py", "qoder_proxy.py"])


def account(uid="fixture-a", endpoint="fixture-a", **extra):
    data = {"uid": uid, "realm": "cn", "vpcEndpoint": endpoint,
            "accessToken": "dt-fixture", "refreshToken": "drt-fixture"}
    data.update(extra)
    return A.Account(data)


def rejected(call):
    try:
        call()
    except ValueError:
        return True
    return False


a, b, public = account(), account("fixture-b", "fixture-b"), account("public", "")
for acc in (a, b):
    cfg = acc.realm_config()
    check(acc.uid + " inference has exactly one instance host",
          A.gateway_candidates("cn", cfg) == [cfg["gateway"]])
    check(acc.uid + " openapi ignores public override",
          A.openapi_candidates("cn", cfg) == [cfg["openapi"]])
    check(acc.uid + " origin is account-specific", acc.headers()["Origin"] == cfg["website"])
check("explicit public remains public", public.realm_config()["domain"] == "qoder.com.cn")
legacy = A.Account({"uid": "legacy", "realm": "cn", "domain": "fixture-a.vpc.qoder.com.cn"})
check("legacy domain migration", legacy.vpc_endpoint == "fixture-a" and legacy.vpc_source == "domain")
for bad in ("bad value", "a" * 56, "fixture:", "fixture?", " fixture", {}, 42):
    check("invalid account fails closed %r" % bad, rejected(lambda: account(endpoint=bad)))
check("INTL plus instance rejected", rejected(lambda: account(endpoint="fixture-a", realm="intl")))
check("INTL public allowed", account(endpoint="", realm="intl").realm_config()["gateway"].endswith("qoder.sh"))

handler = A._VpcRedirectHandler()
request = urllib.request.Request(a.realm_config()["openapi"] + "/start",
                                 headers={"Authorization": "Bearer fixture"})
for label, url, allowed in (
    ("own", a.realm_config()["website"] + "/next", True),
    ("other registered", b.realm_config()["openapi"] + "/next", False),
    ("public", "https://openapi.qoder.com.cn/next", False),
    ("http", "http://fixture-a.vpc.qoder.com.cn/next", False),
    ("port", "https://fixture-a.vpc.qoder.com.cn:8443/next", False),
):
    try:
        redirected = handler.redirect_request(request, None, 302, "Found", {}, url)
        ok = True
    except urllib.error.HTTPError:
        ok = False
    check("redirect " + label, ok == allowed)

with tempfile.TemporaryDirectory(prefix="qd-account-vpc-") as root:
    pool = A.AccountPool(root)
    pool.add(a)
    pool.add(b)
    pool.add(public)
    for acc in (a, b, public):
        doc = A.build_export_document([acc])
        normalized = A.normalise_import_row(doc["accounts"][0])
        restored = A.Account(normalized)
        check(acc.uid + " export/import preserves override", restored.vpc_override == acc.vpc_override)
        check(acc.uid + " export/import preserves endpoint", restored.realm_config() == acc.realm_config())
    row = A.normalise_import_row({"uid": "only-field", "realm": "cn",
                                 "vpcEndpoint": "fixture-b", "accessToken": "fixture"})
    check("import VPC field without domain", A.Account(row).vpc_endpoint == "fixture-b")
    check("invalid import rejected", rejected(lambda: A.normalise_import_row({
        "uid": "bad", "realm": "cn", "vpcEndpoint": "bad value", "accessToken": "fixture"})))
    for endpoint in ("bad value", "a" * 56):
        with mock.patch.object(A, "http_json") as outbound:
            check("PAT rejects before outgoing " + endpoint[:15], rejected(lambda: pool.import_pat(
                "pt-fixture", vpc=endpoint)) and not outbound.called)
        check("OAuth rejects before creating state", rejected(lambda: pool.start_login(vpc=endpoint)))
    with mock.patch.object(A, "http_json") as outbound:
        check("INTL PAT rejects instance", rejected(lambda: pool.import_pat(
            "pt-fixture", realm="intl", vpc="fixture-a")) and not outbound.called)
    with mock.patch.object(A.AccountPool, "_local_machine_id", return_value="fixture-machine"):
        started = pool.start_login(vpc="fixture-b")
    check("OAuth initial URL is instance-bound", started["authUrl"].startswith(b.realm_config()["website"] + "/"))
    check("OAuth pending host registered", "fixture-b-openapi.vpc.qoder.com.cn" in A.vpc_endpoint_hosts())
    calls = []
    class Response(io.BytesIO):
        status = 200
    def fake_open(req, **kwargs):
        calls.append(req.full_url)
        return Response(json.dumps({"token": "dt-fixture", "user_id": "oauth-fixture"}).encode())
    opener = mock.Mock(open=fake_open)
    with mock.patch.object(A, "_opener_for_request", return_value=opener), mock.patch.object(
            A, "http_json", return_value={"id": "oauth-fixture", "name": "fixture"}) as http_stub:
        result = pool.poll_login(started["state"])
    check("OAuth poll remains on initial instance", calls[0].startswith(b.realm_config()["openapi"] + "/"))
    check("OAuth userinfo remains same instance", http_stub.call_args.args[0].startswith(b.realm_config()["openapi"] + "/"))
    check("OAuth account persists instance", result["account"]["vpcEndpoint"] == "fixture-b")
    with mock.patch.object(A.AccountPool, "_local_machine_id", return_value="fixture-machine"):
        cancelled = pool.start_login(vpc="fixture-a")
    def cancelled_open(req, **kwargs):
        pool.cancel_login(cancelled["state"])
        return Response(json.dumps({"token": "dt-fixture", "user_id": "cancelled-user"}).encode())
    with mock.patch.object(A, "_opener_for_request", return_value=mock.Mock(open=cancelled_open)), mock.patch.object(
            A, "http_json", return_value={"id": "cancelled-user"}):
        late = pool.poll_login(cancelled["state"])
    check("cancelled in-flight OAuth cannot import account", late["status"] == "cancelled" and pool.get("cancelled-user") is None)
    with mock.patch.object(A, "http_json", side_effect=[{"token": "jt-fixture"}, {"id": "pat-fixture"}]) as http_stub:
        imported = pool.import_pat("pt-fixture", vpc="fixture-a")
    check("PAT all requests use same instance", all(c.args[0].startswith(a.realm_config()["openapi"] + "/")
                                                   for c in http_stub.call_args_list))
    check("PAT account persists canonical override", imported.vpc_override == "fixture-a")
    with mock.patch.object(A, "http_json") as outbound:
        check("A rejects cached B quota source", a._fetch_credits_via_source(root, {
            "host": b.realm_config()["openapi"], "path": "/quota"}) is None and not outbound.called)
        check("public rejects cached VPC source", public._fetch_credits_via_source(root, {
            "host": a.realm_config()["openapi"], "path": "/quota"}) is None and not outbound.called)
        check("VPC cross-realm diagnostic does not send", a._other_realm_quota_hint() == "" and not outbound.called)
    check("quota cache scopes differ", a.endpoint_scope() != b.endpoint_scope() != public.endpoint_scope())
    A.mark_openapi_missing(a.endpoint_scope(), "/fixture-404")
    check("negative endpoint cache is instance-scoped", not A.openapi_missing(b.endpoint_scope(), "/fixture-404"))
    old = pool.get(a.uid)
    old.credits = {"remain": 123}
    old.reserve_credits = 321
    old.daily_token_limit = 4000
    changed = pool.set_vpc(a.uid, "fixture-b")
    check("set endpoint publishes new snapshot", pool.get(a.uid) is not old and old.vpc_endpoint == "fixture-a")
    check("set endpoint clears old credits", pool.get(a.uid).credits is None)
    check("set endpoint preserves quota gates and team lock", pool.get(a.uid).reserve_credits == 321 and
          pool.get(a.uid).daily_token_limit == 4000 and pool.get(a.uid)._team_lock is old._team_lock)
    old.save(root)
    with open(pool.get(a.uid).path, encoding="utf-8") as fh:
        check("late old snapshot cannot overwrite endpoint", json.load(fh)["vpcEndpoint"] == "fixture-b")
    before = pool.get(a.uid)
    check("invalid edit does not mutate", rejected(lambda: pool.set_vpc(a.uid, "bad value")) and pool.get(a.uid) is before)
    pool.set_vpc(a.uid, "")
    pool.load()
    check("explicit public survives save/reload", pool.get(a.uid).vpc_override == "")

    P.POOL = pool
    P.API_KEY = ""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), P.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    panel = P.PANEL.create()
    def post(path, payload):
        req = urllib.request.Request("http://127.0.0.1:%s%s" % (server.server_port, path),
                    data=json.dumps(payload).encode(), headers={"Content-Type": "application/json", "X-Panel-Token": panel})
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)
    try:
        with mock.patch.object(A, "http_json") as outbound:
            for path in ("/accounts/login/start", "/accounts/import/pat", "/accounts/import-pat"):
                status, _ = post(path, {"realm": "cn", "vpc": "bad value", "pat": "pt-fixture"})
                check(path + " invalid VPC returns 400", status == 400 and not outbound.called)
                status, _ = post(path, {"realm": "intl", "vpc": "fixture-a", "pat": "pt-fixture"})
                check(path + " INTL VPC returns 400", status == 400 and not outbound.called)
        status, result = post("/accounts/set", {"uid": a.uid, "vpcEndpoint": "fixture-b"})
        check("set API saves instance", status == 200 and result["account"]["vpcEndpoint"] == "fixture-b")
        check("set API leaves enabled unchanged", pool.get(a.uid).enabled)
        status, _ = post("/accounts/set", {"uid": a.uid, "vpcEndpoint": "bad value"})
        check("set API rejects invalid config", status == 400)
    finally:
        server.shutdown()
        server.server_close()

with tempfile.TemporaryDirectory(prefix="qd-vpc-home-") as home, tempfile.TemporaryDirectory() as accounts:
    pool = A.AccountPool(accounts)
    pool.add(account("scanner", "fixture-a"))
    for instance in ("fixture-a", "unconfigured"):
        directory = os.path.join(home, ".qoder-cn", ".vpc", instance, ".auth")
        os.makedirs(directory)
        with open(os.path.join(directory, "user"), "w", encoding="utf-8") as fh:
            json.dump({"uid": "scan-" + instance, "access_token": "dt-fixture"}, fh)
    with mock.patch.object(A.os.path, "expanduser", return_value=home), mock.patch.object(A, "_load_app_auth", side_effect=FileNotFoundError()):
        found = A.scan_desktop_credentials()
        candidates = [r for r in found if r["kind"] == "cli"]
        check("scan visits configured instance only", len(candidates) == 1 and candidates[0]["vpcEndpoint"] == "fixture-a")
        imported = A.import_desktop_credential(candidates[0]["path"], realm="cn")
        check("CLI import preserves path instance", imported.vpc_endpoint == "fixture-a")

# Model discovery and inference must reach each selected account's gateway,
# including the redirect-aware transport, not just return correct cfg strings.
class Stream(io.BytesIO):
    def read(self, size=-1):
        return super().read(size)

for instance in ("fixture-a", "fixture-b"):
    selected = account("transport-" + instance, instance)
    selected.credits = {"remain": 5000, "remain_effective": 5000}
    selected.expires_at = 0
    session = mock.Mock()
    session.headers.return_value = {"X-Fixture": selected.uid}
    seen = []
    def transport(req, **kwargs):
        seen.append(req.full_url)
        return Stream(b'{"chat": []}')
    with mock.patch.object(P, "POOL", mock.Mock(pick=mock.Mock(return_value=selected))), mock.patch.object(
            P.SESSIONS, "get", return_value=session), mock.patch.object(
            A, "_opener_for_request", return_value=mock.Mock(open=transport)), mock.patch.object(
            P, "validate_public_http_url", side_effect=lambda url: url):
        P.read_dynamic_models("cn")
    check(instance + " discovery uses account gateway transport", seen and
          seen[0].startswith(selected.realm_config()["gateway"] + "/"), seen)
    seen.clear()
    with mock.patch.object(P.SESSIONS, "get", return_value=session), mock.patch.object(
            A, "_opener_for_request", return_value=mock.Mock(open=transport)), mock.patch.object(
            P, "validate_public_http_url", side_effect=lambda url: url):
        response, used, _ = P.open_upstream({"model": "auto", "messages": [{"role": "user", "content": "fixture"}]},
                                           target_realm="cn", only_account=selected)
        response.close()
    check(instance + " inference uses account gateway transport", used is selected and seen and
          seen[0].startswith(selected.realm_config()["gateway"] + "/"), seen)

with tempfile.TemporaryDirectory() as root:
    pool = A.AccountPool(root)
    pool.add(account("same-user", "fixture-a"))
    check("duplicate uid across instances cannot inherit tokens or overwrite", rejected(
        lambda: pool.add(account("same-user", "fixture-b"))) and
        pool.get("same-user").vpc_endpoint == "fixture-a")

# A new PAT instance must be registered before any Account exists, including
# private DNS and redirect handling, then unregister when flow references expire.
with tempfile.TemporaryDirectory() as root:
    pool = A.AccountPool(root)
    host = "fresh-pat-openapi.vpc.qoder.com.cn"
    check("fresh PAT host initially unregistered", not A.is_configured_vpc_host(host))
    visited = []
    def pat_request(url, **kwargs):
        visited.append(url)
        A.validate_public_http_url(url)
        return {"token": "jt-fixture"} if url.endswith(A.PATH_JOB_EXCHANGE) else {"id": "fresh-pat"}
    with mock.patch.object(A, "http_json", side_effect=pat_request), mock.patch.object(
            A.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("10.20.30.40", 443))]):
        fresh = pool.import_pat("pt-fixture", vpc="fresh-pat")
    check("PAT private DNS accepted only within active instance scope", len(visited) == 2 and
          fresh.vpc_endpoint == "fresh-pat")
    with open(os.path.join(root, "invalid.json"), "w", encoding="utf-8") as fh:
        json.dump({"uid": "invalid", "realm": "cn", "vpcEndpoint": "bad value", "accessToken": "fixture"}, fh)
    pool.load()
    check("bad account isolated on load without losing healthy account", pool.get("invalid") is None and pool.get("fresh-pat") is not None)
    with mock.patch.object(A.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("10.20.30.40", 443))]):
        for module in (A, P):
            check(module.__name__ + " accepts configured private instance", module.validate_public_http_url(
                "https://fresh-pat-openapi.vpc.qoder.com.cn/test").endswith("/test"))
            check(module.__name__ + " rejects unconfigured private instance", rejected(lambda: module.validate_public_http_url(
                "https://unconfigured-private.vpc.qoder.com.cn/test")))

import qoder_tasks as tasks
row = tasks._campaign_task_row(b, {"ok": False}, {})
check("task fallback website follows account instance", row["jump_url"].startswith(b.realm_config()["website"] + "/"))

code = r'''
import json, tempfile, qoder_accounts as A
from unittest import mock
with tempfile.TemporaryDirectory() as root:
    pool = A.AccountPool(root)
    inherited = A.Account({"uid":"inherited","realm":"cn","accessToken":"fixture"})
    public = A.Account({"uid":"public","realm":"cn","accessToken":"fixture","vpcEndpoint":""})
    explicit = A.Account({"uid":"explicit","realm":"cn","accessToken":"fixture","vpcEndpoint":"fixture-b"})
    with mock.patch.object(A.AccountPool, "_local_machine_id", return_value="fixture"):
        login = pool.start_login()
    print(json.dumps([inherited.vpc_endpoint, public.vpc_endpoint, explicit.vpc_endpoint, login["vpcEndpoint"], inherited.to_dict()["vpcEndpoint"]]))
'''
env = dict(os.environ, QD_CN_VPC_ENDPOINT="fixture-default", PYTHONPATH=ROOT)
values = json.loads(subprocess.check_output([sys.executable, "-c", code], cwd=ROOT, env=env, text=True).strip().splitlines()[-1])
check("deployment default remains compatible incl empty pool login", values == ["fixture-default", "", "fixture-b", "fixture-default", None], values)
finish()

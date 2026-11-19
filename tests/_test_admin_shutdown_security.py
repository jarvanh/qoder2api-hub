"""Offline PR161 regressions: proxy provenance, auth, atomicity and failure recovery."""
import os, sys, tempfile, threading, http.server, http.client, json, concurrent.futures
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import bootstrap, check, finish, declare_suite
TMP = tempfile.TemporaryDirectory(prefix="qd-shutdown-security-")
os.environ.update(ACCOUNTS_DIR=TMP.name+"/accounts", QD_PROXY_USAGE_DIR=TMP.name+"/usage", QD_LOG_DIR=TMP.name+"/logs")
os.environ.pop("QD_SHUTDOWN_ALLOW_REMOTE", None)
os.environ.pop("QD_SHUTDOWN_DISABLE", None)
P, A = bootstrap()
import qoder_settings as ST
P.PANEL = ST.PanelSessions(lambda: os.environ["ACCOUNTS_DIR"])
TOKEN = P.PANEL.create()
P.log = lambda *a, **k: None
declare_suite(__file__, ["qoder_proxy.py"])
class H(P.Handler):
    def setup(self):
        super().setup()
        if getattr(self.server, "remote_fixture", False):
            self.client_address = ("192.0.2.7", 12345)
    def log_message(self, *a): pass
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
srv.daemon_threads = True
thread = threading.Thread(target=srv.serve_forever, daemon=True)
thread.start()
def request(headers, method="POST", path="/admin/shutdown"):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_port, timeout=3)
    conn.request(method, path, json.dumps({"confirm": True}), headers)
    response = conn.getresponse()
    result = response.status, response.getheader("Access-Control-Allow-Origin")
    response.read(); conn.close()
    return result
try:
    with mock.patch.object(P, "schedule_shutdown", return_value=True) as schedule:
        check("query/cookie/Bearer are not panel sessions", request({"Cookie":"X-Panel-Token="+TOKEN,"Authorization":"Bearer "+TOKEN},path="/admin/shutdown?token="+TOKEN)[0] == 401)
        check("cross-origin preflight has no ACAO", request({"Origin":"https://fixture.invalid","Access-Control-Request-Headers":"x-panel-token"}, method="OPTIONS") == (204,None))
        check("foreign Origin without panel header denied", request({"Origin":"https://fixture.invalid"})[0] == 401)
        for header in ("Forwarded", "X-Forwarded-For", "X-Real-IP", "CF-Connecting-IP"):
            check(header+" on loopback denied", request({"X-Panel-Token":TOKEN,header:"127.0.0.1"})[0] == 403)
            check(header+" empty still denied", request({"X-Panel-Token":TOKEN,header:""})[0] == 403)
        srv.remote_fixture = True
        check("real remote peer cannot spoof Host", request({"X-Panel-Token":TOKEN,"Host":"localhost"})[0] == 403)
        with mock.patch.dict(os.environ, {"QD_SHUTDOWN_ALLOW_REMOTE":"1"}):
            check("explicit allow remote accepts forwarded peer", request({"X-Panel-Token":TOKEN,"X-Forwarded-For":"192.0.2.7"})[0] == 200)
        srv.remote_fixture = False
        check("only allowed request scheduled", schedule.call_count == 1)
    with mock.patch.object(P, "schedule_shutdown", side_effect=RuntimeError("fixture")):
        check("worker launch failure controlled HTTP 503", request({"X-Panel-Token":TOKEN})[0] == 503)
finally:
    srv.shutdown(); srv.server_close(); thread.join(3)
P._ACTIVE_SERVER = mock.Mock()
P._SHUTDOWN_STARTED = False
with mock.patch.object(P.threading, "Thread") as worker:
    worker.return_value.start.side_effect = RuntimeError("fixture")
    try: P.schedule_shutdown()
    except RuntimeError: pass
    check("launch failure rolls back state", P._SHUTDOWN_STARTED is False)
with mock.patch.object(P.threading, "Thread") as worker:
    check("retry after launch failure succeeds", P.schedule_shutdown() is True)
    check("stopping plan blocked", P.shutdown_plan_info()["can_shutdown"] is False and "stopping" in P.shutdown_plan_info()["blocked_codes"])
    check("repeated scheduling rejected", P.schedule_shutdown() is False)
P._SHUTDOWN_STARTED = False
with mock.patch.object(P.threading, "Thread") as worker:
    # Invoke captured worker body with a failing listener, no real thread or process.
    P._ACTIVE_SERVER.shutdown.side_effect = RuntimeError("fixture")
    P.schedule_shutdown(delay=0)
    worker.call_args.kwargs["target"]()
    check("listener shutdown failure permits retry", P._SHUTDOWN_STARTED is False)
P._ACTIVE_SERVER.shutdown.side_effect = None
P._SHUTDOWN_STARTED = False
with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
    result = list(pool.map(lambda _:P.schedule_shutdown(delay=0), range(32)))
check("32 concurrent callers schedule once", sum(result) == 1)
TMP.cleanup()
finish()

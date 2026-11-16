"""#158: unknown queue cause, bounded observations and truthful SSE diagnostics."""
import os
import sys
import io
import threading
from types import SimpleNamespace
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *
P, A = bootstrap()
declare_suite(__file__, ["qoder_proxy.py"])

Q = {"code":10605,"isQueued":True,"queueType":"p4","queueCount":1,
     "retryAfterSeconds":60,"serviceAvailable":True,"token":"SECRET"}
for detail in (Q, json.dumps(Q), {"body":json.dumps(Q)}, {"message":json.dumps(Q)}):
    f = P.queue_fields(detail)
    check("nested queue fields", f == {"queue_type":"p4","queue_count":1,"retry_after":60,
          "service_available":True,"reason":"unknown"}, f)
for detail in (None, [], "bad", {"code":112}, {"isQueued":"false"}, "x"*9000):
    check("malformed/nonqueue ignored", P.queue_fields(detail) is None)
f=P.queue_fields(dict(Q,queueType="SECRET",retryAfterSeconds=True,queueCount=-1,serviceAvailable="true"))
check("unknown types never copied raw", f["queue_type"]=="unknown" and f["retry_after"] is None and f["queue_count"] is None and f["service_available"] is None)
P._QUEUE_OBSERVATIONS.clear()
def note(t, scope="scopeA", success=False):
    with mock.patch.object(P.time,"monotonic",return_value=t):
        P.note_queue_observation(scope,Q,success=success)
def snap(t):
    with mock.patch.object(P.time,"monotonic",return_value=t):
        return P.queue_observation_snapshot()
note(100);note(550);note(1000)
s=snap(1000)
check("persistent requires spaced samples", s["persistent"] and s["observations"]==3 and s["max_observed_span_seconds"]==900,s)
check("reason stays unknown even with p4/serviceAvailable", s["reason"]=="unknown")
check("snapshot exposes no raw identifiers or secrets", not any(v in json.dumps(s) for v in ("SECRET","scopeA","token")))
note(1001,"scopeB",True)
check("unrelated success cannot reset episode", snap(1001)["persistent"])
note(1002,success=True)
check("matching success resets episode", snap(1002)["active_scopes"]==0)
note(1100);note(2101)
check("long unobserved gap is not continuous queue", not snap(2101)["persistent"] and snap(2101)["observations"]==1)
check("stale samples not active", snap(3102)["active_scopes"]==0)
check("TTL evicts stale state", snap(90000)["active_scopes"]==0 and not P._QUEUE_OBSERVATIONS)
for i in range(300): note(100000,"scope"+str(i))
check("bounded unique scope memory", len(P._QUEUE_OBSERVATIONS)==256)
P._QUEUE_OBSERVATIONS.clear()
# Do not patch a shared clock in parallel: clock independence is irrelevant here.
threads=[threading.Thread(target=lambda: [P.note_queue_observation("same",Q) for _ in range(20)]) for i in range(5)]
for t in threads:t.start()
for t in threads:t.join()
check("concurrent increments atomic", P.queue_observation_snapshot()["observations"]==100)

class Response(io.BytesIO):
    def __init__(self, body, scope="scopeA"):
        super().__init__(body)
        self._qd_queue_scope=scope

def envelope(body,status=200):
    return ("data: "+json.dumps({"statusCodeValue":status,"body":body})+"\n\n").encode()
def chunk(content="ok",finish=None):
    return json.dumps({"choices":[{"index":0,"delta":{"content":content},"finish_reason":finish}]})
P._QUEUE_OBSERVATIONS.clear()
resp=Response(envelope(Q,403))
try:list(P.iter_inner_sse(resp))
except P.UpstreamStatus:pass
check("SSE queue observed once", P.queue_observation_snapshot()["observations"]==1)
list(P.iter_inner_sse(Response(envelope(chunk("")))))
check("HTTP200 start does not clear queue", P.queue_observation_snapshot()["active_scopes"]==1)
list(P.iter_inner_sse(Response(envelope(chunk(finish="stop")),scope="other")))
check("other scope terminal does not clear", P.queue_observation_snapshot()["active_scopes"]==1)
list(P.iter_inner_sse(Response(envelope(chunk(finish="stop")))))
check("valid terminal clears matching queue", P.queue_observation_snapshot()["active_scopes"]==0)

handler=object.__new__(P.Handler)
handler._json=lambda status,obj:obj
handler._error=lambda status,msg:{"error_status":status,"message":msg}
handler._request_realm=lambda:None
account=A.Account({"uid":"fixture-cn","realm":"cn","accessToken":"fixture"})
def diag(body):
    resp=Response(body)
    with mock.patch.object(P,"open_upstream",return_value=(resp,account,"")) as upstream, mock.patch.object(A,"native_machine_identity",return_value=None):
        out=handler._handle_diag_queue({"realm":"cn"})
        check("diag closes upstream",resp.closed)
        check("diag sets single-attempt flag",upstream.call_args.kwargs["usage_ctx"]["_queue_probe_once"] is True)
        return out
out=diag(envelope(Q,403))
check("HTTP200 envelope queue not success",out["kind"]=="queued" and out["code"]==403,out)
check("diag parsed queue metadata",out["queue_type"]=="p4" and out["service_available"] is True and out["retry_after"]==60,out)
check("diag never copies raw queue error text",out["detail"]=="" and "SECRET" not in json.dumps(out))
check("diag reports actual account/realm",out["account"]=="fixture-" and out["realm"]=="cn")
out=diag(envelope(json.dumps({"error":Q})))
check("inner error is not successful diagnostic",out["kind"]=="queued",out)
out=diag(b"")
check("empty SSE not success",out["code"]==502 and out["kind"]!="ok",out)
out=diag(envelope(chunk())+envelope(chunk("",finish="stop")))
check("valid completed SSE diagnostic success",out["kind"]=="ok" and out["code"]==200,out)
with mock.patch.object(P,"POOL",SimpleNamespace(get=lambda uid:None)),mock.patch.object(P,"open_upstream") as upstream:
    out=handler._handle_diag_queue({"uid":"missing"})
    check("unknown UID does not probe another account",out["error_status"]==404 and not upstream.called)
with mock.patch.object(P,"POOL",SimpleNamespace(get=lambda uid:account)),mock.patch.object(P,"open_upstream") as upstream:
    out=handler._handle_diag_queue({"uid":account.uid,"realm":"intl"})
    check("cross-realm UID rejected",out["error_status"]==400 and not upstream.called)
with mock.patch.object(A,"account_realm_config",return_value={"domain":"TARGET","gateway":"https://HOST_A"}):
    s1=P._queue_scope(account,"MODEL_A");s2=P._queue_scope(account,"MODEL_B")
with mock.patch.object(A,"account_realm_config",return_value={"domain":"TARGET","gateway":"https://HOST_B"}):
    s3=P._queue_scope(account,"MODEL_A")
check("scope isolates endpoint and model",len({s1,s2,s3})==3)
scope_a = P._queue_scope(account,"MODEL_A","https://HOST_A/path")
scope_b = P._queue_scope(account,"MODEL_A","https://HOST_B/path")
check("selected alternate gateway has isolated scope",scope_a != scope_b)
P._QUEUE_OBSERVATIONS.clear()
P.note_queue_observation(scope_a,Q)
list(P.iter_inner_sse(Response(envelope(chunk(finish="stop")),scope=scope_b)))
check("other actual gateway success cannot clear queued gateway",P.queue_observation_snapshot()["active_scopes"]==1)
list(P.iter_inner_sse(Response(envelope(chunk(finish="stop")),scope=scope_a)))
check("actual gateway matching success clears queue",P.queue_observation_snapshot()["active_scopes"]==0)
check("client wording has no capacity/risk guarantee","原因未确认" in P.friendly_upstream_error(503,json.dumps(Q))[0] and "原因未确认" in P._quota_hint(json.dumps(Q)))
# Exercise actual open_upstream HTTP and transport branches with fake I/O.
import urllib.error
for mode in ("queue", "http500", "transport"):
    with tempfile.TemporaryDirectory(prefix="q158-") as tmp:
        pool=A.AccountPool(tmp,log=lambda *a,**k:None)
        pool.accounts.append(account)
        def fail(*args,**kwargs):
            if mode=="transport":raise OSError("fixture connection reset")
            raise urllib.error.HTTPError("https://HOST.invalid",403 if mode=="queue" else 500,"fixture",{},io.BytesIO(json.dumps(Q if mode=="queue" else {"error":"fixture"}).encode()))
        with mock.patch.object(P,"POOL",pool),mock.patch.object(P,"build_qoder_body",return_value={"parameters":{}}), \
             mock.patch.object(P,"resolve_model_key",return_value="dfmodel"),mock.patch.object(P,"exclusive_realm",return_value=""), \
             mock.patch.object(P,"conversation_identity",return_value=None),mock.patch.object(P,"assign_prefix_session"), \
             mock.patch.object(P,"validate_public_http_url",side_effect=lambda url:url), \
             mock.patch.object(P,"_team_acquire",return_value=object()),mock.patch.object(P,"_team_note_client"), \
             mock.patch.object(P,"_team_release") as released,mock.patch.object(P.SESSIONS,"get",return_value=SimpleNamespace(headers=lambda *a,**k:{})), \
             mock.patch.object(P.urllib.request,"urlopen",side_effect=fail) as network,mock.patch.object(P.time,"sleep") as slept:
            try:
                P.open_upstream({"model":"DeepSeek-Flash","messages":[]},only_account=account,target_realm="cn",usage_ctx={"_queue_probe_once":True})
                check("probe failure propagates "+mode,False)
            except (P.UpstreamStatus,OSError):
                check("one probe no retries/sleep "+mode,network.call_count==1 and not slept.called,network.call_count)
                check("probe error releases lease "+mode,released.call_count==1,released.call_count)
check("deep invalid JSON ignored",P.queue_fields("["*2000+"]"*2000) is None)
check("double encoded queue supported",P.queue_fields(json.dumps(json.dumps(Q)))["queue_type"]=="p4")
# Real HTTP /health read stays passive and public without adding scoped secrets.
from http.server import ThreadingHTTPServer
import urllib.request
with mock.patch.object(P,"POOL",None),mock.patch.object(P,"current_account",return_value=None), \
     mock.patch.object(P,"risk_snapshot",return_value={}),mock.patch.object(P,"env_report",return_value={}), \
     mock.patch.object(P,"quality_snapshot",return_value={}),mock.patch.object(P,"open_upstream") as network:
    server=ThreadingHTTPServer(("127.0.0.1",0),P.Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/health" % server.server_port,timeout=5) as response:
            health=json.loads(response.read())
        check("public health exposes observation without probing", "queue_observation" in health and not network.called)
        check("health does not assert risk cause",health["queue_observation"]["reason"]=="unknown")
    finally:
        server.shutdown();server.server_close();thread.join(3)
for malformed in (7, True, "invalid", {}):
    try:
        # Isolate new observation from existing clean_chunk schema validation.
        with mock.patch.object(P,"clean_chunk",side_effect=lambda body:body):
            list(P.iter_inner_sse(Response(envelope(json.dumps({"choices":malformed})))))
        check("observation tolerates malformed choices",True)
    except Exception as exc:
        check("observation tolerates malformed choices",False,repr(exc))
finish()

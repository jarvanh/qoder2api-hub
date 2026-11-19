"""#162: upstream reset is not client disconnect; real chunked HTTP must end."""
import os, sys, io, json, tempfile, threading, http.client
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *
P,A=bootstrap()
declare_suite(__file__,["qoder_proxy.py","qoder_anthropic.py"])

def broken():
    yield b'data: {}\n\n'
    raise ConnectionResetError("fixture upstream reset")
for interval in (0,0.01):
    try:
        list(P.sse_with_heartbeat(broken(),lambda data:None,interval=interval))
        check("read reset classified upstream",False)
    except P.UpstreamStatus as exc:
        check("read reset classified upstream",exc.status==502 and "ConnectionResetError" in exc.detail)
def slow():
    import time
    time.sleep(0.06)
    yield b"data: {}\n\n"
def fail_write(data):raise BrokenPipeError("fixture downstream")
try:list(P.sse_with_heartbeat(slow(),fail_write,interval=0.01))
except BrokenPipeError:check("heartbeat write remains downstream",True)
else:check("heartbeat write remains downstream",False)

class Handler(P.Handler):
    def log_message(self,*args):pass
class Resp:
    def __init__(self,mode):self.mode=mode;self.closed=False;self.close_event=threading.Event()
    def __iter__(self):
        tool={"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_fixture","type":"function","function":{"name":"write","arguments":'{"content":"'+"中文α"*1000}}]},"finish_reason":None}]}
        yield ("data: "+json.dumps({"statusCodeValue":200,"body":json.dumps(tool)})+"\n\n").encode()
        if self.mode=="reset":raise ConnectionResetError("fixture upstream")
        if self.mode=="aborted":raise ConnectionAbortedError("fixture upstream")
        if self.mode=="timeout":raise TimeoutError("fixture upstream")
        if self.mode=="finish":
            yield ("data: "+json.dumps({"statusCodeValue":200,"body":json.dumps({"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]})})+"\n\n").encode()
    def close(self):self.closed=True;self.close_event.set()
    def __enter__(self):return self
    def __exit__(self,*args):self.close()
state={"mode":"reset","calls":0,"response":None}
def open_fake(*args,**kwargs):
    state["calls"]+=1
    response=Resp(state["mode"]);state["response"]=response
    return response,P.POOL.accounts[0],""
with tempfile.TemporaryDirectory(prefix="q162-") as tmp:
    pool=A.AccountPool(tmp,log=lambda *a,**k:None)
    pool.accounts=[A.Account({"uid":"fixture","realm":"cn","accessToken":"FAKE"})]
    with mock.patch.object(P,"POOL",pool),mock.patch.object(P,"ACCOUNTS_DIR",tmp),mock.patch.object(P,"API_KEY","fixture-key"), \
         mock.patch.object(P,"configured_keys",return_value=[]),mock.patch.object(P,"open_upstream",side_effect=open_fake), \
         mock.patch.object(P,"record_usage") as usage,mock.patch.object(P,"record_error") as errors:
        srv=P.ThreadingHTTPServer(("127.0.0.1",0),Handler)
        thread=threading.Thread(target=srv.serve_forever,daemon=True);thread.start()
        try:
            for mode in ("reset","aborted","timeout","eof","finish"):
                for endpoint in ("/v1/chat/completions","/v1/responses","/v1/messages"):
                    state.update(mode=mode,calls=0);usage.reset_mock();errors.reset_mock()
                    payload={"model":"Qwen3.8-Flash","stream":True,"max_tokens":64,"messages":[{"role":"user","content":"fixture"}],"tools":[{"type":"function","function":{"name":"write","parameters":{"type":"object"}}}]}
                    if endpoint.endswith("responses"):payload["input"]="fixture"
                    if endpoint.endswith("messages"):payload["tools"]=[{"name":"write","input_schema":{"type":"object"}}]
                    conn=http.client.HTTPConnection("127.0.0.1",srv.server_port,timeout=5)
                    try:
                        conn.request("POST",endpoint,json.dumps(payload),{"Authorization":"Bearer fixture-key","x-api-key":"fixture-key","Content-Type":"application/json"})
                        response=conn.getresponse()
                        try:
                            body=response.read()
                            check(mode+endpoint+" valid final chunk",response.status==200 and response.chunked)
                        except Exception as exc:
                            check(mode+endpoint+" valid final chunk",False,repr(exc));continue
                        events=[json.loads(line[6:]) for line in body.decode().splitlines() if line.startswith("data: ") and line!="data: [DONE]"]
                        check(mode+endpoint+" all SSE data JSON",bool(events))
                        check(mode+endpoint+" explicit error",any("error" in item or item.get("type")=="error" or (item.get("response") or {}).get("error") for item in events),body[-250:])
                        if endpoint.endswith("completions"):check(mode+" chat DONE",body.rstrip().endswith(b"data: [DONE]"))
                        check(mode+endpoint+" no paid replay",state["calls"]==1,state["calls"])
                        check(mode+endpoint+" error not chat done",errors.called and not usage.called)
                        check(mode+endpoint+" response closed",state["response"].close_event.wait(2))
                    finally:conn.close()
        finally:srv.shutdown();srv.server_close();thread.join(3)
finish()

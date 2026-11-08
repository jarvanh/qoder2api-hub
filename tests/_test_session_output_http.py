"""HTTP end-to-end damaged tool completion must not become successful stop."""
import os,sys,io,json,tempfile,threading,http.client,socket
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *
from unittest import mock
P,A=bootstrap()
declare_suite(__file__,['qoder_proxy.py','qoder_anthropic.py'])
class Handler(P.Handler):
 def log_message(self,*a):pass
current={'frames':[],'calls':0}
def f(delta,finish=None):return {'id':'fixture','choices':[{'index':0,'delta':delta,'finish_reason':finish}]}
def fake_open(payload,**kw):
 current['calls']+=1
 rows=[b'data: '+json.dumps({'statusCodeValue':200,'body':json.dumps(c)}).encode()+b'\n\n' for c in current['frames']]
 rows+=[b'data: {"body":"[DONE]"}\n\n']
 return io.BytesIO(b''.join(rows)),P.POOL.accounts[0],''
with tempfile.TemporaryDirectory() as root:
 pool=A.AccountPool(root,log=lambda *a,**k:None);pool.accounts=[A.Account({'uid':'fake','realm':'cn','accessToken':'FAKE'})]
 with mock.patch.object(P,'POOL',pool),mock.patch.object(P,'ACCOUNTS_DIR',root),mock.patch.object(P,'API_KEY','fixture-key'),mock.patch.object(P,'configured_keys',return_value=[]),mock.patch.object(P,'open_upstream',side_effect=fake_open):
  srv=P.ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=srv.serve_forever,daemon=True).start()
  def post(endpoint,stream):
   payload={'model':'Qwen3.8-Flash','stream':stream,'max_tokens':256,'messages':[{'role':'user','content':'fixture'}],'tools':[{'type':'function','function':{'name':'run_code','parameters':{'type':'object'}}}]}
   if endpoint.endswith('responses'):
    payload['input']='fixture'
   if endpoint.endswith('messages'):
    payload['tools']=[{'name':'run_code','input_schema':{'type':'object'}}]
   c=http.client.HTTPConnection('127.0.0.1',srv.server_port,timeout=10)
   c.request('POST',endpoint,json.dumps(payload),{'Authorization':'Bearer fixture-key','Content-Type':'application/json','x-api-key':'fixture-key'})
   r=c.getresponse();status=r.status;body=r.read();c.close();return status,body
  try:
   cases=[('reasoning_only',[f({'reasoning_content':'synthetic private trace'}),f({},'stop')]),('nameless',[f({'tool_calls':[{'index':0,'function':{'arguments':'{}'}}]}),f({},'tool_calls')]),('truncated',[f({'tool_calls':[{'index':0,'function':{'name':'run_code','arguments':'{"code":'}}]}),f({},'tool_calls')])]
   for label,frames in cases:
    for endpoint in ['/v1/chat/completions','/v1/responses','/v1/messages']:
     for stream in [True,False]:
      current.update(frames=frames,calls=0)
      with mock.patch.object(P,"tool_echo_mode",return_value="lenient"):
       status,body=post(endpoint,stream)
      if stream:
       success=b'response.completed' if endpoint.endswith('responses') else b'event: message_stop' if endpoint.endswith('messages') else b'"finish_reason": "stop"'
       check(label+' '+endpoint+' error terminal',b'error' in body and success not in body,body[-300:])
      else:check(label+' '+endpoint+' HTTP 502',status==502,(status,body[-120:]))
      # fork 语义：坏工具回声/空轮是采样随机问题，四条协议出口默认都允许
      # 至多 QD_BAD_ARGS_MAX_RETRIES 次同账号自动重发（未吐字节才重发；0 = 永不重发）。
      _max_calls = 1 + int(os.environ.get('QD_BAD_ARGS_MAX_RETRIES') or 1)
      check(label+' '+endpoint+' bounded bad-args replay', 1 <= current['calls'] <= _max_calls, current['calls'])
  finally:srv.shutdown();srv.server_close()
finish()

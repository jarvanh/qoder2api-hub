"""No silent tool-turn success; reasoning remains separate; quota is not wallet balance."""
import os,sys,json,tempfile,types
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *
from unittest import mock
P,A=bootstrap()
import qoder_anthropic as AN
declare_suite(__file__,['qoder_proxy.py','qoder_anthropic.py'])
def frame(delta=None,finish=None):
 return ('data: '+json.dumps({'id':'fixture','choices':[{'index':0,'delta':delta or {},'finish_reason':finish}]})+'\n\n').encode()
def tc(name=None,args='',index=0):
 fn={'arguments':args}
 if name is not None:fn['name']=name
 return {'index':index,'type':'function','function':fn}
def collect(source):
 out=[]
 try:
  for f in source:out.append(f)
 except Exception as e:return out,e
 return out,None
def recover(frames):return P.recover_leaked_tool_calls(iter(frames),{'run_code'})
def parse(frames):
 out=[]
 for f in frames:
  s=P.strip_data_prefix(f.decode() if isinstance(f,bytes) else f)
  try:out.append(json.loads(s))
  except (TypeError,ValueError):pass
 return out
for frames,label in [([frame({'tool_calls':[tc(args='{}')]},'tool_calls')],'nameless with finish'),([frame({'tool_calls':[tc(args='{}')]}),b'data: [DONE]\n\n'],'nameless EOF'),([frame({'reasoning_content':'synthetic private trace'}),frame({},'stop')],'reasoning-only stop'),([frame({},'stop')],'empty stop'),([frame({},'tool_calls')],'orphan tool finish')]:
 out,error=collect(recover(frames))
 check(label+' fails explicitly',isinstance(error,P.EmptyToolEcho),type(error).__name__)
 check(label+' no successful terminal',not any(c.get('finish_reason') for f in parse(out) for c in f.get('choices',[])))
for fin in ['stop','tool_calls','length']:
 out,error=collect(recover([frame({'tool_calls':[tc('run_code','{"code":')]}),frame({},fin)]))
 check('truncated args with '+fin+' fails',isinstance(error,P.EmptyToolEcho))
 check('truncated args with '+fin+' no success',not any(c.get('finish_reason') for f in parse(out) for c in f.get('choices',[])))
frames=[frame({'reasoning_content':'synthetic private trace'}),frame({'content':'visible answer'},'stop')]
out,error=collect(recover(frames));obj=parse(out)
check('mixed reasoning/text preserved',error is None and any(c.get('delta',{}).get('reasoning_content')=='synthetic private trace' for f in obj for c in f.get('choices',[])))
check('reasoning never promoted to visible content','synthetic private trace' not in ''.join(c.get('delta',{}).get('content','') for f in obj for c in f.get('choices',[])))
fence=chr(96)*3
out,error=collect(recover([frame({'content':fence+'python\nprint("not executed")\n'+fence},'stop')]))
check('code answer is never guessed as tool',error is None and not any(c.get('delta',{}).get('tool_calls') for f in parse(out) for c in f.get('choices',[])))
audit={}
list(P.recover_leaked_tool_calls(iter([frame({'reasoning_content':'synthetic private trace'}),frame({'content':fence+'python\nprint(1)\n'+fence},'stop')]),{'run_code'},audit=audit))
check('metadata distinguishes code-only answer without storing text',audit.get('output_code_without_tools') is True and audit.get('output_has_tool_call') is False and audit.get('output_reasoning_chars')==len('synthetic private trace') and 'synthetic private trace' not in json.dumps(audit))
valid=[frame({'tool_calls':[tc('run_code','{"code":')]}),frame({'tool_calls':[tc(args='"x"}')]}),frame({},'tool_calls')]
out,error=collect(recover(valid));check('valid tool args remain streamed',error is None and len(out)>=3)
for label,converter,terminal in [('responses',lambda src:P.stream_responses_events(src,'fixture',{'usage':None}),b'response.completed'),('anthropic',lambda src:AN.stream_anthropic_events(src,'fixture',holder={'usage':None}),b'message_stop')]:
 out,error=collect(converter(recover([frame({'reasoning_content':'synthetic private trace'}),frame({},'stop')])))
 check(label+' reasoning-only propagates failure',isinstance(error,P.EmptyToolEcho))
 check(label+' does not send success terminal',terminal not in b''.join(out))
D112=json.dumps({'code':'112','message':json.dumps({'pricingUrl':'https://example.invalid/pricing'})})
check('pricing URL alone is not balance evidence',P._quota_cooldown('{"code":"403","pricingUrl":"x"}')[0]=='')
msg,kind=P.friendly_upstream_error(403,D112)
check('112 message acknowledges balance uncertainty','不等于账号总余额为零' in msg and kind=='quota_exhausted')
check('112 keeps non-auth client classification',P._client_error_status(403,kind)==429)
with tempfile.TemporaryDirectory() as d:
 a=A.Account({'uid':'fake','realm':'intl','accessToken':'FAKE','expiresAt':time.time()+3600});a.credits={'remain':100,'updated_at':time.time()}
 P._note_quota_cooldown(a,403,D112,'fixture',model='Sonus')
 check('positive balance only pauses denied model',a.cooldown_until<=time.time() and a.model_cooldowns.get('Sonus',0)>time.time())
 check('unrelated models remain ready',a.ready(model='Qwen3.8-Flash'))
 check('quota handling never overwrites displayed balance',a.credits['remain']==100)
with mock.patch.object(P,'POOL',types.SimpleNamespace(count_ready=lambda *a,**kw:5)):
 check('international exclusive Sonus never falls back CN',P._alt_realm_ready('intl','Sonus')=='')
with mock.patch.object(P,'POOL',types.SimpleNamespace(count_ready=lambda *a,**kw:0)):
 check('zero ready accounts is zero, not clamped one',P._alt_realm_ready('cn','DeepSeek-Flash')=='')
with mock.patch.object(P,'POOL',None):
 check('missing pool cannot invent cross-region candidate',P._alt_realm_ready('cn','DeepSeek-Flash')=='')
 try:P.open_upstream({'model':'Sonus','messages':[]},target_realm='cn')
 except P.UpstreamStatus as e:check('wrong-realm model fails before network',e.status==400)
 else:check('wrong-realm model fails before network',False)
# A nonempty final plan is a normal model stop, not malformed tool protocol.
plan='Research summary. Next I will try a different analysis approach.'
plan_audit={}
out,error=collect(P.recover_leaked_tool_calls(iter([frame({'content':plan},'stop')]),{'run_code'},audit=plan_audit))
check('planning prose plus stop remains a model stop',error is None and any(c.get('finish_reason')=='stop' for f in parse(out) for c in f.get('choices',[])))
check('planning prose is not guessed into a tool or replay',not plan_audit.get('output_has_tool_call') and plan_audit.get('output_content_chars')==len(plan))
check('updater recognizes corrected version as newer',P.qoder_updater._version_tuple('1.9.1')>P.qoder_updater._version_tuple('1.9.0') and P.qoder_updater._version_tuple('1.9.1')>P.qoder_updater._version_tuple('1.8.1'))
finish()

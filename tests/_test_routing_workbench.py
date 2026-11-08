"""Tier scheduling, policy persistence, preview and outgoing request budgets."""
import os,sys,types,collections,urllib.error
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *
from unittest import mock
P,A=bootstrap()
import qoder_routing as R
declare_suite(__file__,['qoder_routing.py','qoder_proxy.py','dashboard.html'])
with tempfile.TemporaryDirectory() as directory:
 pool=A.AccountPool(directory,log=lambda *a,**k:None)
 for uid,realm in [('a','cn'),('b','cn'),('c','cn'),('i','intl')]:
  pool.accounts.append(A.Account({'uid':uid,'nickname':uid,'realm':realm,'accessToken':'FAKE','expiresAt':time.time()+3600}))
 policy={'strategy':'weighted-round-robin','session_affinity':False,'request_retry':0,'max_retry_accounts':0}
 tiers=[{'id':'first','name':'首选','accounts':['a','b'],'weights':{'a':3,'b':1}}, {'id':'backup','name':'备用','accounts':['c'],'weights':{'c':1}}]
 saved=R.save_config(directory,tiers=tiers,policy=policy)
 check('full policy and tiers atomic roundtrip',R.load_config(directory)==saved)
 check('board persisted as explicit scope',saved['board_enabled'] is True)
 d=R.resolve(directory,pool.accounts,'Qwen',explicit_realm='cn')
 check('tier membership excludes unassigned accounts',d['allowed_uids']=={'a','b','c'})
 counts=collections.Counter(R.pick(pool,d,'cn','Qwen').uid for _ in range(40))
 check('smooth weighted distribution 3:1',counts=={'a':30,'b':10},dict(counts))
 check('no leakage to lower tier while first available','c' not in counts)
 pool.get('a').cooldown_until=time.time()+600
 check('first tier healthy peer takes over',R.pick(pool,d,'cn','Qwen').uid=='b')
 pool.get('b').cooldown_until=time.time()+600
 check('unavailable top tier falls through to next',R.pick(pool,d,'cn','Qwen').uid=='c')
 pool.get('a').cooldown_until=pool.get('b').cooldown_until=0
 restricted=R.resolve(directory,pool.accounts,'Qwen',key={'id':'only-c','account_ids':['c']},explicit_realm='cn')
 check('key scope intersects all tiers',R.pick(pool,restricted,'cn','Qwen').uid=='c')
 denied=R.resolve(directory,pool.accounts,'Qwen',key={'account_ids':['i']},explicit_realm='cn')
 check('empty key tier intersection never widens',denied['allowed_uids']==set() and R.pick(pool,denied,'cn','Qwen') is None)
 d['policy']['session_affinity']=True
 pool.affinity.bind('window','c')
 check('sticky session retains permitted fallback account',R.pick(pool,d,'cn','Qwen','window').uid=='c')
 d['policy']['session_affinity']=False
 check('affinity off ignores existing binding',R.pick(pool,d,'cn','Qwen','window').uid in {'a','b'})
 before=dict(R._SELECTOR_STATE)
 preview=R.preview(directory,pool.accounts,'Qwen',realm='cn')
 check('preview high layer active',preview['active_tier']=='first')
 check('preview computes static weights',preview['tiers'][0]['members'][0]['share']==75 and preview['tiers'][0]['members'][1]['share']==25)
 check('preview does not mutate selector',R._SELECTOR_STATE==before)
 check('preview never leaks credential','FAKE' not in json.dumps(preview))
 cfg={'rules':R.normalize_rules([{'id':'intl-rule','model':'*','realm':'intl'}]),'tiers':[],'policy':R.normalize_policy(None),'configured':True}
 rule_preview=R.preview(directory,pool.accounts,'Qwen',config=cfg,fallback_realm='cn')
 check('explicit rule realm precedes model fallback',rule_preview['realm']=='intl')
 zero_cfg={'rules':[],'tiers':R.normalize_tiers([dict(tiers[0],weights={'a':0,'b':0}),tiers[1]]),'policy':policy,'configured':True,'board_enabled':True}
 zero=R.resolve(directory,pool.accounts,'Qwen',explicit_realm='cn',config=zero_cfg)
 check('weight zero tier falls through without divide-by-zero',R.pick(pool,zero,'cn','Qwen').uid=='c')
 for invalid in [dict(policy,request_retry=4),dict(policy,session_affinity='false'),dict(policy,max_retry_accounts=-1)]:
  try:R.save_config(directory,policy=invalid)
  except ValueError:check('invalid policy rejected',True)
  else:check('invalid policy rejected',False)
 try:R.save_config(directory,tiers=[dict(tiers[0]),dict(tiers[1],accounts=['a'])])
 except ValueError:check('duplicate account across tiers rejected',True)
 else:check('duplicate account across tiers rejected',False)
 try:R.save_config(directory,policy=policy,expected_revision='stale')
 except ValueError:check('stale save rejected',True)
 else:check('stale save rejected',False)
 check('failed save preserves disk config',R.load_config(directory)==saved)
 R.save_config(directory,tiers=[],policy=policy)
 check('explicit empty board blocks instead of all pool',R.resolve(directory,pool.accounts,'Qwen')['allowed_uids']==set())
 R.save_config(directory,tiers=tiers,policy=dict(policy,strategy='fill-first',session_affinity=False,max_retry_accounts=1,request_retry=1))
 attempts=[]
 def fail(req,**kwargs):
  attempts.append(req.headers.get('X-test-uid'))
  raise urllib.error.HTTPError(req.full_url,403,'queued',{},io.BytesIO(b'{"code":10605,"isQueued":true,"retryAfterSeconds":1}'))
 fake_sessions=types.SimpleNamespace(get=lambda a:types.SimpleNamespace(headers=lambda *args,**kwargs:{'X-Test-Uid':a.uid}))
 with mock.patch.object(P,'POOL',pool),mock.patch.object(P,'ACCOUNTS_DIR',directory),mock.patch.object(P,'SESSIONS',fake_sessions),mock.patch.object(P,'validate_public_http_url',side_effect=lambda x:x),mock.patch.object(P.urllib.request,'urlopen',side_effect=fail),mock.patch.object(P,'_queue_wait_seconds',return_value=1),mock.patch.object(P,'QUEUE_MAX_ROUNDS',0),mock.patch.object(P,'queue_mode',return_value='wait'),mock.patch.object(P.time,'sleep'):
  try:P.open_upstream({'model':'Qwen3.8-Flash','messages':[{'role':'user','content':'hi'}]},target_realm='cn')
  except Exception:pass
 check('one account per round and one retry means two attempts',attempts==['a','a'],attempts)
 # Legacy rule-only persistence keeps old selection defaults.
 with tempfile.TemporaryDirectory() as legacy:
  R.save_rules(legacy,[{'id':'r','model':'*','accounts':['b'],'strategy':'fill-first'}])
  conf=R.load_config(legacy)
  check('legacy rules do not silently enable new policy',not conf['configured'] and not conf['board_enabled'])
  old=R.resolve(legacy,pool.accounts,'Qwen',explicit_realm='cn')
  check('legacy per-rule strategy preserved',old['strategy']=='fill-first' and R.pick(pool,old,'cn','Qwen').uid=='b')
finish()

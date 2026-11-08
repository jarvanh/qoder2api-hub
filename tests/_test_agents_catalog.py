"""Popular harness adapter contract and no-secret template regression."""
import os,sys,types,json,tempfile
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *
from unittest import mock
P,A=bootstrap()
import qoder_agents as G
declare_suite(__file__,['qoder_agents.py','dashboard.html'])
automatic=[cid for cid,c in G.CLIENTS.items() if c.get('mode')!='manual']
check('catalog includes 30+ named clients',len(G.CLIENTS)>=30,len(G.CLIENTS))
check('at least thirteen automatic harness adapters',len(automatic)>=13,automatic)
with tempfile.TemporaryDirectory() as root:
 home=os.path.join(root,'home');state=os.path.join(root,'state');os.makedirs(home)
 overrides={'DSH_HOME':os.path.join(home,'.dsh'),'CODEX_HOME':os.path.join(home,'.codex'),'CLAUDE_CONFIG_DIR':os.path.join(home,'.claude'),'PI_CODING_AGENT_DIR':os.path.join(home,'.pi','agent'),'COPILOT_PROVIDERS_CONFIG':os.path.join(home,'.copilot','providers.json'),'XDG_CONFIG_HOME':os.path.join(home,'.config')}
 with mock.patch.dict(os.environ,overrides):
  for cid in automatic:
   try:
    built=G.plan(cid,'http://127.0.0.1:8790','FAKE-SECRET',model='Qwen3.8-Flash',models=[{'id':'Qwen3.8-Flash','context_window':128000,'max_output':8192}],home=home)
    check(cid+' plan produces config',bool(built['files']))
    first={f['path']:f['content'] for f in built['files']}
    result=G.integrate(state,cid,'http://127.0.0.1:8790','FAKE-SECRET',model='Qwen3.8-Flash',models=[{'id':'Qwen3.8-Flash','context_window':128000,'max_output':8192}],home=home)
    check(cid+' apply exactly matches plan',all(open(p,encoding='utf-8').read()==text for p,text in first.items()))
    again=G.plan(cid,'http://127.0.0.1:8790','FAKE-SECRET',model='Qwen3.8-Flash',models=[{'id':'Qwen3.8-Flash','context_window':128000,'max_output':8192}],home=home)
    check(cid+' repeat is idempotent',first=={f['path']:f['content'] for f in again['files']})
    G.restore(state,cid,home=home)
    check(cid+' restore removes new config',all(not os.path.exists(p) for p in first))
   except Exception as e:check(cid+' adapter exception',False,str(e))
  p=os.path.join(home,'.config','opencode','opencode.json');os.makedirs(os.path.dirname(p),exist_ok=True)
  original=b'{"provider":{"unrelated":{"options":{"token":"OTHER-SECRET"}}},"theme":"system"}\r\n';open(p,'wb').write(original)
  G.integrate(state,'opencode','http://127.0.0.1:8790','FAKE-SECRET',model='M',home=home)
  data=json.load(open(p,encoding='utf-8'));check('opencode v1 model schema is map',isinstance(data['provider']['qoder-proxy']['models'],dict));check('other provider retained',data['provider']['unrelated']['options']['token']=='OTHER-SECRET')
  G.restore(state,'opencode',home=home);check('restore byte-exact for existing config',open(p,'rb').read()==original)
  with mock.patch.object(G,'_read_text',side_effect=lambda path: (_ for _ in ()).throw(AssertionError('read outside template')) if not 'qoder-agent-template-' in path else ''):
   for cid in automatic:
    r=G.template(cid,'http://127.0.0.1:8790','MODEL')
    encoded=json.dumps(r)
    check(cid+' template contains placeholder not real key','YOUR_API_KEY' in encoded and 'OTHER-SECRET' not in encoded and 'FAKE-SECRET' not in encoded)
  ext=os.path.join(home,'.vscode','extensions','saoudrizwan.claude-dev-9.0.0');os.makedirs(ext)
  with mock.patch.object(G.shutil,'which',return_value=None):
   ov=G.overview(state,home=home);cline=next(c for c in ov['clients'] if c['id']=='cline')
   check('extension inventory detects Cline without reading state',cline['installed'] and cline['mode']=='manual' and bool(cline['evidence']))
  try:G.plan('cursor','http://127.0.0.1:8790','FAKE-SECRET',home=home)
  except G.AgentConfigError:check('manual-only never writes private state',True)
  else:check('manual-only never writes private state',False)
  G.integrate(state,'codex','http://127.0.0.1:8790','FIRST',model='M',home=home)
  paths=G.CLIENTS['codex']['files'](home);snap={p:open(p,'rb').read() for p in paths.values()};real_write=G._atomic_write_text
  def fail_second(path,text):
   if path==paths['auth']:raise OSError('injected second-file write failure')
   return real_write(path,text)
  with mock.patch.object(G,'_atomic_write_text',side_effect=fail_second):
   try:G.integrate(state,'codex','http://127.0.0.1:9999','SECOND',model='N',home=home)
   except G.AgentConfigError:pass
  check('repeat configuration failure restores immediate prior bytes',all(open(p,'rb').read()==data for p,data in snap.items()))
  G.restore(state,'codex',home=home)
  check('state contains no plaintext configured key','FAKE-SECRET' not in open(G._state_path(state),encoding='utf-8').read())
html=open(os.path.join(ROOT,'dashboard.html'),encoding='utf-8').read()
settings=html[html.index('<div id="pageSettings"'):html.index('<div id="pageSettings"')+5000]
agents=html[html.index('<div id="pageAgents"'):html.index('<div id="pageSettings"')]
check('API examples live only on Agents page','id="adminApiExamples"' in agents and 'id="adminApiExamples"' not in settings)
finish()

"""Routing hot cache: no repeated reads, independent public copies, fail-closed edits."""
import os,sys,tempfile,json
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *
from unittest import mock
P,A=bootstrap()
import qoder_routing as R
from pathlib import Path
declare_suite(__file__,['qoder_routing.py'])
with tempfile.TemporaryDirectory() as d:
 pool=A.AccountPool(d,log=lambda *a,**k:None)
 a=A.Account({'uid':'a','realm':'cn','accessToken':'FAKE','expiresAt':time.time()+3600});pool.accounts.append(a)
 config=R.save_config(d,tiers=[{'id':'one','accounts':['a']}],policy=R.DEFAULT_POLICY)
 with mock.patch.object(Path,'open',side_effect=AssertionError('hot routing read disk')):
  for _ in range(10):decision=R.resolve(d,pool.accounts,'Qwen',explicit_realm='cn')
  check('warm resolve skips file open and JSON decode',decision['allowed_uids']=={'a'})
 public=R.load_config(d);public['tiers'][0]['accounts'].clear();public['policy']['strategy']='fill-first'
 other=R.load_config(d)
 check('public config mutation cannot corrupt runtime cache',other['tiers'][0]['accounts']==['a'] and other['policy']['strategy']=='round-robin')
 path=R.rules_path(d);new={'rules':[],'tiers':[],'policy':R.DEFAULT_POLICY,'board_enabled':True}
 path.write_text(json.dumps(new),encoding='utf-8')
 check('external edit invalidates immediately',R.resolve(d,pool.accounts,'Qwen')['allowed_uids']==set())
 temp=path.with_suffix('.new');temp.write_text(json.dumps(dict(new,board_enabled=False)),encoding='utf-8');os.replace(temp,path)
 check('external atomic replace invalidates immediately',R.resolve(d,pool.accounts,'Qwen')['allowed_uids'] is None)
 path.write_text('{broken',encoding='utf-8')
 try:R.resolve(d,pool.accounts,'Qwen')
 except ValueError:check('malformed edit never uses old scope cache',True)
 else:check('malformed edit never uses old scope cache',False)
 path.unlink();check('delete invalidates cache',R.load_config(d)['revision']=='empty')
 R.save_config(d,tiers=[{'id':'new','accounts':['a']}],policy=R.DEFAULT_POLICY)
 check('recreate invalidates missing cache',R.load_config(d)['tiers'][0]['id']=='new')
finish()

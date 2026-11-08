"""Offline exact traffic buckets and account attribution for CPAMC layout port."""
import os,sys,types,tempfile,time,json
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *
P,A=bootstrap()
import qoder_admin as D
from unittest import mock
declare_suite(__file__,['qoder_admin.py','qoder_proxy.py','dashboard.html'])
now=1800000123
accounts=[types.SimpleNamespace(uid='a',nickname='A',group='production',realm='cn',enabled=True,access_token='FAKE',ready=lambda:True),types.SimpleNamespace(uid='b',nickname='B',group='',realm='intl',enabled=False,access_token='FAKE',ready=lambda:False)]
rows=[{'at':now-20,'account':'a','total_tokens':100},{'at':now-30,'account':'a','error':'failed','total_tokens':2},{'at':now-650,'account':'b','total_tokens':25},{'at':now-700,'total_tokens':10},{'at':now-7*3600,'account':'a','total_tokens':999},{'at':now+100,'account':'a'},{'at':'bad'},None]
r=D.build_overview(rows,accounts,now=now)
check('36 real ten-minute buckets',len(r['traffic']['buckets'])==36 and r['window']['bucket_seconds']==600)
check('window success/failure count',r['traffic']['success']==3 and r['traffic']['failed']==1 and r['traffic']['total']==4)
check('success rate exact',r['traffic']['success_rate']==75)
check('future and out-of-window rows excluded',r['traffic']['tokens']==137)
check('unattributed recorded explicitly',r['unattributed']==1)
a=next(a for a in r['accounts'] if a['uid']=='a')
check('account exact attribution',a['success']==1 and a['failed']==1 and a['total']==2 and a['tokens']==102)
check('account series conserved',sum(a['buckets'])==2)
check('global bucket sum conserved',sum(b['success']+b['failed'] for b in r['traffic']['buckets'])==4)
check('disabled state not presented as ready',not next(a for a in r['accounts'] if a['uid']=='b')['ready'])
check('never return access credentials','FAKE' not in json.dumps(r))
empty=D.build_overview([],accounts,now=now)
check('empty traffic does not fabricate percentage',empty['traffic']['success_rate'] is None and empty['traffic']['total']==0)
check('empty account stays null rate',all(a['success_rate'] is None for a in empty['accounts']))
# Aggregator receives chronological and non-chronological rows identically.
check('input ordering does not change aggregate',D.build_overview(list(reversed(rows)),accounts,now=now)==r)
html=open(os.path.join(ROOT,'dashboard.html'),encoding='utf-8').read()
check('official hero wire and throughput sections',['cp-heroPanel','cpLiveWire','cpTrafficChart','cpCredentialHealth','cpRuntimeRows'].count('absent')==0 and all(s in html for s in ['cp-heroPanel','cpLiveWire','cpTrafficChart','cpCredentialHealth','cpRuntimeRows']))
check('official motion timing retained',all(s in html for s in ['/900','*.07,.36','.45s var(--ease-out-strong)','.36s var(--ease-out-strong)']))
check('curve does not use random data','Math.random' not in html[html.index('function cpSmooth'):html.index('function cpRenderOverview')])
check('official login dot field ported','function cpLoginField' in html and 'function cpStartLogin' in html and 'sampleDotTargets' in html)
check('reduced-motion check retained','prefers-reduced-motion: reduce' in html)
finish()

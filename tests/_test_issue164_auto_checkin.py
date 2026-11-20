"""#164 existing automatic check-in is visible and respects pause/startup stop."""
import os,sys,tempfile,json
from types import SimpleNamespace
from unittest import mock
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *
P,A=bootstrap()
import qoder_scheduler as S
declare_suite(__file__,["qoder_scheduler.py","dashboard.html","docs/accounts.md"])
class Event:
    def __init__(self,cancel=False):self.stopped=False;self.cancel=cancel;self.waits=[]
    def wait(self,seconds):
        self.waits.append(seconds)
        if self.cancel or seconds==15:self.stopped=True
        return self.stopped
    def is_set(self):return self.stopped
for enabled in (True,False):
    with tempfile.TemporaryDirectory(prefix="q164-") as tmp:
        os.makedirs(os.path.join(tmp,"scheduler"))
        with open(os.path.join(tmp,"scheduler","state.json"),"w") as f:json.dump({"enabled":enabled},f)
        scheduler=S.Scheduler(SimpleNamespace(dir=tmp,accounts=[]))
        scheduler._stop_event=Event()
        with mock.patch.object(scheduler,"_execute_cycle") as cycle,mock.patch.object(scheduler,"_next_target",return_value=None):
            scheduler._run_loop()
            check("saved enable/pause restored "+str(enabled),scheduler.enabled is enabled)
            check("startup only when enabled "+str(enabled),cycle.call_count==(1 if enabled else 0))
            if enabled:check("startup reason correct",cycle.call_args.args[1]==S.CYCLE_STARTUP)
        check("startup wait cancellable",scheduler._stop_event.waits[0]==10)
        scheduler._stop_event=Event(cancel=True)
        with mock.patch.object(scheduler,"_execute_cycle") as cycle:
            scheduler._run_loop()
            check("stop during startup sends no cycle",not cycle.called)
        with mock.patch.object(scheduler,"_run_cycle") as cycle:
            scheduler.enabled=False;scheduler._stop_event=Event()
            scheduler._execute_cycle("fixture",S.CYCLE_STARTUP)
            scheduler._execute_cycle("fixture",S.CYCLE_HOUR)
            check("paused execution gate automatic",not cycle.called)
            scheduler._execute_cycle("fixture",S.CYCLE_MANUAL)
            check("explicit manual still permitted",cycle.call_count==1)
            scheduler.enabled=True;scheduler._stop_event.stopped=True
            scheduler._execute_cycle("fixture",S.CYCLE_HOUR)
            check("stopped execution gate automatic",cycle.call_count==1)
with tempfile.TemporaryDirectory(prefix="q164-") as tmp:
    acc=A.Account({"uid":"fixture","realm":"cn","accessToken":"FAKE"});acc.enabled=True
    pool=SimpleNamespace(dir=tmp,accounts=[acc])
    scheduler=S.Scheduler(pool)
    check("auto checkin default enabled",scheduler.enabled)
    check("default daily times",scheduler.status()["checkin_times"]=="09:00,21:00")
    with mock.patch.object(S,"run_keepalive",return_value={"refreshed":0,"failed":0,"logs":[]}), \
         mock.patch.object(S,"run_batch_checkin",return_value={"accounts_count":1,"credit_added":0,"logs":[]}) as batch, \
         mock.patch.object(acc,"fetch_credits"),mock.patch.object(S.time,"sleep"),mock.patch.object(acc,"can_checkin",return_value=True), \
         mock.patch.object(S.time,"localtime",return_value=SimpleNamespace(tm_hour=12)):
        scheduler._run_cycle("fixture",S.CYCLE_STARTUP)
        check("startup actually invokes checkin batch",batch.call_count==1 and batch.call_args.args[0]==[acc])
        scheduler._run_cycle("fixture",S.CYCLE_STARTUP)
        check("same day startup dedup preserved",batch.call_count==1)
        scheduler._run_cycle("fixture",S.CYCLE_MANUAL)
        check("manual explicitly checks eligible",batch.call_count==2)
        scheduler._pending_focus="签到期 09:00"
        scheduler._run_cycle("fixture",S.CYCLE_HOUR)
        check("scheduled checkin actually invokes batch",batch.call_count==3)
        scheduler.enabled=False;scheduler.log("fixture pause")
    restored=S.Scheduler(pool)
    check("pause persisted after restart",restored.enabled is False)
source=open(os.path.join(ROOT,"dashboard.html"),encoding="utf-8").read()
check("panel clearly names automatic checkin","自动签到 / Token 保活" in source)
check("panel explains daily times and running requirement","自动签到默认每日 09:00 / 21:00" in source and "网关需保持运行" in source)
finish()

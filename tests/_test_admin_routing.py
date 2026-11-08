"""Offline account-group routes, policy boundaries and panel-only HTTP API."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *
from unittest import mock
import threading
import http.client
import types

P, A = bootstrap()
import qoder_routing as R
import qoder_admin as ADM
import qoder_settings as S

declare_suite(__file__, ["qoder_routing.py", "qoder_admin.py", "qoder_proxy.py", "qoder_accounts.py"])

with tempfile.TemporaryDirectory() as directory:
    pool = A.AccountPool(directory, log=lambda *a, **k: None)
    for uid, group, realm in (("a", "工作", "cn"), ("b", "工作", "cn"), ("c", "备用", "intl"), ("d", "", "cn")):
        account = A.Account({"uid": uid, "nickname": uid, "realm": realm,
                             "accessToken": "FAKE-TOKEN", "expiresAt": time.time()+3600, "group": group})
        account.save(directory)
        pool.accounts.append(account)
    rules = [{"id":"main","name":"主路由","model":"Qwen*","realm":"cn","groups":["工作"],
              "strategy":"fill-first","priority":1,"enabled":True}]
    saved=R.save_rules(directory,rules)
    check("routing rules persist/reload", R.load_rules(directory)==saved)
    decision=R.resolve(directory,pool.accounts,"Qwen3.8-Flash",key={"account_ids":["b"]})
    check("key scope intersects route groups", decision["allowed_uids"]=={"b"})
    check("fill-first selects permitted account", R.pick(pool,decision,"cn","Qwen3.8-Flash").uid=="b")
    pool.affinity.bind("window","a")
    check("affinity cannot bypass key scope", R.pick(pool,decision,"cn","Qwen3.8-Flash","window").uid=="b")
    empty=R.resolve(directory,pool.accounts,"Qwen3.8-Flash",key={"account_ids":["c"]})
    check("empty intersection never falls back to all", empty["allowed_uids"]==set()
          and R.pick(pool,empty,"cn","Qwen3.8-Flash") is None)
    before=pool._cursor
    preview=R.preview(directory,pool.accounts,"Qwen3.8-Flash",{"account_ids":["b"]})
    check("preview has exact candidates", [a["uid"] for a in preview["eligible_accounts"]]==["b"])
    check("preview never advances selection", pool._cursor==before)
    check("preview never leaks credentials", "FAKE-TOKEN" not in json.dumps(preview))
    explicit=R.resolve(directory,pool.accounts,"Qwen3.8-Flash",explicit_realm="intl")
    check("explicit realm not overwritten by incompatible rule", explicit["realm"]=="intl" and explicit["matched"] is None)
    try:
        R.resolve(directory,pool.accounts,"Qwen",key={"realm":"cn"},explicit_realm="intl")
    except ValueError:
        check("key realm cannot be overridden",True)
    else:
        check("key realm cannot be overridden",False)
    R.save_rules(directory,[dict(rules[0],groups=[""],accounts=[])])
    check("empty-name group means ungrouped only", R.resolve(directory,pool.accounts,"Qwen")["allowed_uids"]=={"d"})
    try:
        R.save_rules(directory,[dict(rules[0],strategy="invalid")])
    except ValueError:
        check("invalid rule rejected before persistence",True)
    else:
        check("invalid rule rejected before persistence",False)
    check("invalid save keeps existing policy",R.load_rules(directory)[0]["groups"]==[""])
    R.save_rules(directory,[])
    S.set_api_keys(directory,[{"id":"k","key":"FAKE-API-KEY","name":"test","account_groups":["工作"],"account_ids":["b"]}])
    with mock.patch.object(P,"POOL",pool), mock.patch.object(P,"ACCOUNTS_DIR",directory), \
         mock.patch.object(P,"configured_keys",side_effect=lambda:S.api_keys(directory)), \
         mock.patch.object(P.Handler,"_panel_ok",side_effect=lambda:True):
        # HTTP tests use a subclass with controllable panel token, never real auth state.
        class Handler(P.Handler):
            def _panel_ok(self):
                return self.headers.get("X-Panel-Token")=="test-panel"
        server=P.ThreadingHTTPServer(("127.0.0.1",0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def request(path,body=None,token="test-panel",key=""):
            conn=http.client.HTTPConnection("127.0.0.1",server.server_port,timeout=8)
            headers={"Content-Type":"application/json"}
            if token: headers["X-Panel-Token"]=token
            if key: headers["Authorization"]="Bearer "+key
            conn.request("POST" if body is not None else "GET",path,
                         json.dumps(body).encode() if body is not None else None,headers)
            response=conn.getresponse();raw=response.read();status=response.status;conn.close()
            return status,json.loads(raw)
        try:
            status,_=request("/admin/groups",token="")
            check("anonymous group administration denied",status==401)
            status,_=request("/admin/groups",token="",key="FAKE-API-KEY")
            check("API Key cannot administer groups",status==401)
            status,_=request("/admin/overview",token="")
            check("anonymous overview denied",status==401)
            with mock.patch.object(P,"fetch_models",side_effect=AssertionError("overview must not fetch upstream")):
                status,overview=request("/admin/overview")
            check("overview real endpoint remains local-only",status==200 and len(overview["traffic"]["buckets"])==36)
            check("overview public response excludes account credentials","FAKE-TOKEN" not in json.dumps(overview))
            status,body=request("/admin/groups")
            check("group overview shows cross-realm groups",status==200 and len(body["groups"])==3)
            check("public accounts exclude tokens", "FAKE-TOKEN" not in json.dumps(body))
            status,body=request("/admin/accounts/group",{"uids":["a"],"group":"生产"})
            check("single account group editing persists",status==200 and pool.get("a").group=="生产")
            status,body=request("/admin/groups/action",{"group":"工作","action":"disable"})
            check("group disable affects only matched group",status==200 and not pool.get("b").enabled and pool.get("a").enabled)
            status,_=request("/admin/groups/action",{"action":"disable"})
            check("missing group never expands to whole pool",status==400 and pool.get("a").enabled)
            status,_=request("/admin/groups/action",{"group":"工作","action":"enable"})
            check("group enable",status==200 and pool.get("b").enabled)
            status,body=request("/admin/routing/save",{"rules":rules})
            check("HTTP routing save",status==200 and body["rules"][0]["id"]=="main")
            status,body=request("/admin/routing/preview",{"model":"Qwen3.8-Flash","key_id":"k"})
            check("HTTP preview uses key restrictions",status==200 and [a["uid"] for a in body["eligible_accounts"]]==["b"])
            status,body=request("/settings")
            check("settings echo account selectors",status==200 and body["api_keys"][0]["account_groups"]==["工作"])
            status,_=request("/settings/save",{"api_keys":[{"id":"k","name":"renamed","enabled":True}]})
            stored=S.api_keys(directory)[0]
            check("editing key without selectors keeps scope",status==200 and stored["account_ids"]==["b"] and stored["account_groups"]==["工作"])
            with mock.patch.object(A.Account,"refresh",return_value=True) as refresh:
                status,body=request("/admin/groups/action",{"group":"工作","action":"refresh"})
                job_id=body["job_id"]
                for _ in range(50):
                    _,state=request("/admin/jobs?id="+job_id)
                    if state["job"]["status"] in ("completed","failed"):break
                    time.sleep(.01)
                check("group refresh job completes",status==202 and state["job"]["status"]=="completed")
                check("group refresh never touches others", refresh.call_count==1 and state["job"]["results"][0]["uid"]=="b")
        finally:
            server.shutdown();server.server_close();thread.join(3)
    # Actual request selection, not just rule preview.
    context={"_key_entry":{"id":"scope","account_ids":["b"]}}
    with mock.patch.object(P,"POOL",pool),mock.patch.object(P,"ACCOUNTS_DIR",directory), \
         mock.patch.object(P,"SESSIONS",types.SimpleNamespace(get=lambda a:types.SimpleNamespace(headers=lambda *a,**k:{}))), \
         mock.patch.object(P,"validate_public_http_url",side_effect=lambda u:u), \
         mock.patch.object(P.urllib.request,"urlopen",return_value=io.BytesIO()) as upstream:
        response,used,_=P.open_upstream({"model":"Qwen3.8-Flash","messages":[{"role":"user","content":"test"}]},
                                        target_realm="cn",usage_ctx=context)
        response.close()
        check("real inference obeys Key plus route scope",used.uid=="b" and upstream.call_count==1)
        denied={"_key_entry":{"account_ids":["c"]}}
        try:
            P.open_upstream({"model":"Qwen3.8-Flash","messages":[{"role":"user","content":"test"}]},
                            target_realm="cn",usage_ctx=denied)
        except P.UpstreamStatus:
            check("scope mismatch fails before network",upstream.call_count==1)
        else:
            check("scope mismatch fails before network",False)
finish()

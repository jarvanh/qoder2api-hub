"""v1.8.0: performance changes must preserve results, freshness and isolation."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *
from unittest import mock
from concurrent.futures import ThreadPoolExecutor
import threading
import copy
import types
import urllib.error
P, A = bootstrap()
import qoder_cache as C

declare_suite(__file__, ["qoder_cache.py", "qoder_proxy.py", "Dockerfile"])

print("[perf] fixed cache expiry and independent LRU recency")
cache = C.JsonResultCache(max_entries=2, max_bytes=1024)
count = [0]
def build():
    count[0] += 1
    return {"n": count[0], "nested": {"values": [1]}}
with mock.patch.object(C.time, "monotonic", return_value=100) as clock:
    first = cache.get("key", build, ttl=5)
    first["nested"]["values"].append(2)
    clock.return_value = 104
    second = cache.get("key", build, ttl=5)
    check("cache hit returns detached nested objects", second["nested"]["values"] == [1])
    clock.return_value = 106
    third = cache.get("key", build, ttl=5)
    check("frequent hits never extend data freshness", third["n"] == 2)
check("builder executed exactly twice", count[0] == 2)
cache = C.JsonResultCache()
with mock.patch.object(C.time, "monotonic", return_value=100) as clock:
    a = cache.get("poll", lambda: {"generation": clock.return_value}, ttl=10)
    clock.return_value = 105
    check("five-second dashboard poll hits ten-second cache", cache.get("poll", lambda: {"generation": 105}, ttl=10) == a)
    clock.return_value = 110
    check("ten-second expiry is fixed", cache.get("poll", lambda: {"generation": 110}, ttl=10) != a)

print("[perf] cold concurrent polls share one scan")
cache = C.JsonResultCache()
barrier = threading.Barrier(8)
started = threading.Event()
release = threading.Event()
counter_lock = threading.Lock()
count = [0]
def slow():
    with counter_lock:
        count[0] += 1
    started.set()
    release.wait(3)
    return {"answer": [42]}
def caller():
    barrier.wait(3)
    return cache.get("single", slow)
with ThreadPoolExecutor(max_workers=8) as executor:
    futures = [executor.submit(caller) for _ in range(8)]
    check("owner builder starts", started.wait(3))
    release.set()
    values = [f.result(5) for f in futures]
check("8 simultaneous reads run 1 builder", count[0] == 1, count)
check("all waiters obtain correct response", all(v == {"answer": [42]} for v in values))
check("inflight slot released", cache.status()["inflight"] == 0)
try:
    cache.get("failure", lambda: (_ for _ in ()).throw(ValueError("expected")))
except ValueError:
    pass
check("failed builder never poisons cache", cache.get("failure", lambda: {"ok": True}) == {"ok": True})
cache = C.JsonResultCache(max_entries=2, max_bytes=30)
for i in range(10):
    cache.get(i, lambda: {"n": 1})
check("entry and byte limits enforced", cache.status()["entries"] <= 2 and cache.status()["bytes"] <= 30)
cache.get("large", lambda: {"text": "x"*1000})
check("oversized response not retained", "large" not in cache._entries)
check("invalidated build not cached", C.JsonResultCache().get("x", lambda: {"ok": True}, valid=lambda: False) == {"ok": True})

print("[perf] analytics invalidation and data parity")
with tempfile.TemporaryDirectory() as directory:
    path = os.path.join(directory, "usage.jsonl")
    row = {"at": time.time(), "model": "Qwen3.8-Flash", "prompt_tokens": 100,
           "completion_tokens": 10, "cached_tokens": 90, "total_tokens": 110}
    def write_rows(rows):
        with open(path, "w", encoding="utf-8") as out:
            for item in rows:
                out.write(json.dumps(item)+"\n")
    write_rows([row])
    with mock.patch.object(P, "USAGE_LOG", path), mock.patch.object(P, "POOL", None), \
         mock.patch.object(P, "_USAGE_REPORT_CACHE", C.JsonResultCache()):
        with mock.patch.object(P, "compute_usage_stats", wraps=P.compute_usage_stats) as spy:
            a = P.cached_usage_report("stats", range="all")
            b = P.cached_usage_report("stats", range="all")
            check("stats repeated reports equal", a == b)
            check("stats repeated query scans once", spy.call_count == 1)
            with open(path, "a", encoding="utf-8") as out:
                out.write(json.dumps(row)+"\n")
            c = P.cached_usage_report("stats", range="all")
            check("append visible immediately despite TTL", c["kpi"]["requests"] == 2)
            write_rows([])
            check("truncate invalidates", P.cached_usage_report("stats", range="all")["kpi"]["requests"] == 0)
            os.replace(path, path+".old")
            write_rows([row])
            check("rotation invalidates", P.cached_usage_report("stats", range="all")["kpi"]["requests"] == 1)
        with mock.patch.object(P, "_count_usage_rows_uncached", wraps=P._count_usage_rows_uncached) as counter:
            check("recent count initial", P.count_usage_rows() == 1)
            check("recent count reused", P.count_usage_rows() == 1 and counter.call_count == 1)
            with open(path, "a", encoding="utf-8") as out:
                out.write(json.dumps(row)+"\n")
            check("recent count append invalidates immediately", P.count_usage_rows() == 2 and counter.call_count == 2)
        account = A.Account({"uid": "fake", "nickname": "before", "realm": "cn", "accessToken": "fake"})
        fake_pool = types.SimpleNamespace(accounts=[account])
        with mock.patch.object(P, "POOL", fake_pool), \
             mock.patch.object(P, "compute_usage_analytics", wraps=P.compute_usage_analytics) as scan:
            first = P.cached_usage_report("analytics", range="all")
            account.nickname = "after"
            second = P.cached_usage_report("analytics", range="all")
            check("account rename invalidates metadata-dependent reports", scan.call_count == 2)
            check("renamed account is shown", any(a["nickname"] == "after" for a in second["accounts"]))
            account.credits = {"remain": 123}
            third = P.cached_usage_report("analytics", range="all")
            check("credit snapshot changes invalidate analytics", scan.call_count == 3)
            check("fresh credits visible", any(a.get("credits", {}).get("remain") == 123 for a in third["accounts"]))
        expected = P.compute_usage_analytics(range="all")
        actual = P.cached_usage_report("analytics", range="all")
        check("analytics response parity", actual == expected)
        other = os.path.join(directory, "other.jsonl")
        with open(other, "w", encoding="utf-8") as out:
            out.write(json.dumps(dict(row, total_tokens=999))+"\n")
        with mock.patch.object(P, "USAGE_LOG", other):
            check("paths never share snapshots", P.cached_usage_report("stats", range="all")["kpi"]["tokens"] == 999)

print("[perf] daily cache must not slide indefinitely")
with mock.patch.object(P, "_daily_cache", P.OrderedDict()), \
     mock.patch.object(P, "_usage_daily_uncached", side_effect=lambda *a: {"generation": time.monotonic()}), \
     mock.patch.object(P.time, "time", return_value=1800000000) as clock:
    a = P.usage_daily(ttl=10)
    clock.return_value = 1800000008
    check("daily still hits before fixed expiry", P.usage_daily(ttl=10) is a)
    clock.return_value = 1800000011
    check("daily re-evaluates after creation TTL", P.usage_daily(ttl=10) is not a)

print("[perf] fingerprint fast path is equivalent")
for text in ("plain 汉字"*100, "X-AnThRoPiC-BiLlInG-HeAdEr: abc; text",
             "unrelated x- text", "x-anthropic-billing-header", "X-anthropic-billing-header",
             "You are a coding agent running in the Codex CLI", "cc_entrypoint=test"):
    expected = any(f in text for f in P.SANITIZE_FEATURES) or bool(P.SANITIZE_BARE_HDR_RE.search(text))
    check("fingerprint equivalence " + text[:28], P.has_fingerprint(text) == expected)

print("[perf] chunk copy preserves other choices and caller")
chunk = {"id": "test", "choices": [{"index": 0, "delta": {"content": "old", "role": "assistant"}},
                                    {"index": 1, "delta": {"content": "keep"}}],
         "usage": {"tokens": 5}, "extension": {"opaque": 1}}
original = copy.deepcopy(chunk)
changed = P._chunk_with_content(chunk, "new")
check("original nested delta untouched", chunk == original)
check("only target content changes", changed["choices"][0]["delta"]["content"] == "new"
      and changed["choices"][1] == original["choices"][1])
check("unknown metadata preserved", changed["extension"] == original["extension"] and changed["usage"] == original["usage"])

print("[perf] wire size exact Base64 math and final guard")
for n in (0, 1, 2, 3, 4, 31, 32, 33, 1024, 8193):
    check("encoded length parity %d" % n, len(P.qoder_encode(b"x"*n)) == 4*((n+2)//3))
with tempfile.TemporaryDirectory() as directory:
    pool = A.AccountPool(directory, log=lambda *a, **k: None)
    acc = A.Account({"uid": "perf-test", "realm": "cn", "accessToken": "fake"})
    acc.enabled = True
    acc.expires_at = 0
    pool.accounts.append(acc)
    fake_session = types.SimpleNamespace(headers=lambda *a, **k: {})
    with mock.patch.object(P, "POOL", pool), \
         mock.patch.object(P, "SESSIONS", types.SimpleNamespace(get=lambda a: fake_session)), \
         mock.patch.object(P, "validate_public_http_url", side_effect=lambda x: x), \
         mock.patch.object(P.urllib.request, "urlopen", return_value=io.BytesIO()), \
         mock.patch.object(P, "qoder_encode", wraps=P.qoder_encode) as encode:
        response, _, _ = P.open_upstream({"model": "Qwen3.8-Flash", "messages": [{"role": "user", "content": "hello"}]}, target_realm="cn")
        response.close()
        check("only one real full-body encoding per account attempt", encode.call_count == 1, encode.call_count)
    def enlarge(body, *a, **k):
        body["session_id"] = "s"*20000
    with mock.patch.object(P, "POOL", pool), mock.patch.object(P, "MAX_UPSTREAM_BODY_BYTES", 10000), \
         mock.patch.object(P, "assign_prefix_session", side_effect=enlarge), \
         mock.patch.object(P.urllib.request, "urlopen") as network:
        try:
            P.open_upstream({"model": "Qwen3.8-Flash", "messages": [{"role": "user", "content": "hello"}]}, target_realm="cn")
        except P.PayloadTooLarge:
            check("final post-session wire size checked", True)
        else:
            check("final post-session wire size checked", False)
        check("oversized final body never sent", not network.called)
print("[perf] real dashboard handler uses reusable reports")
import http.client
with tempfile.TemporaryDirectory() as directory:
    source = os.path.join(directory, "usage.jsonl")
    with open(source, "w", encoding="utf-8") as out:
        out.write(json.dumps({"at": time.time(), "model": "Qwen3.8-Flash", "total_tokens": 4})+"\n")
    with mock.patch.object(P, "USAGE_LOG", source), mock.patch.object(P, "POOL", None), \
         mock.patch.object(P, "auth_required", return_value=False), \
         mock.patch.object(P.Handler, "_panel_ok", return_value=True), \
         mock.patch.object(P, "_USAGE_REPORT_CACHE", C.JsonResultCache()), \
         mock.patch.object(P, "compute_usage_stats", wraps=P.compute_usage_stats) as stats, \
         mock.patch.object(P, "compute_usage_analytics", wraps=P.compute_usage_analytics) as analytics:
        server = P.ThreadingHTTPServer(("127.0.0.1", 0), P.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for path, spy in (("/usage/stats?range=all", stats), ("/usage/analytics?range=all", analytics)):
                results = []
                for _ in range(2):
                    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                    conn.request("GET", path)
                    response = conn.getresponse()
                    results.append(json.loads(response.read()))
                    check(path + " HTTP 200", response.status == 200)
                    conn.close()
                check(path + " preserves response on reuse", results[0] == results[1])
                check(path + " scans once", spy.call_count == 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
finish()

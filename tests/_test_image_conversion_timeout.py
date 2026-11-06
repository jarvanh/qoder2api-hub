"""Image timeout regression: actual codecs, real Handler, stubbed provider only."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *
from unittest import mock
import copy
import http.client
import threading
import types
import urllib.error

P, A = bootstrap()
import qoder_images as Q

declare_suite(__file__, ["qoder_images.py", "qoder_proxy.py", "Dockerfile"])
DETAIL = json.dumps({"code": "provider_error", "message": "Image conversion timeout",
                     "request_id": "fake-request-id", "type": "provider_error",
                     "details": "image conversion timeout after 30 seconds"})

print("[image] classification, retry budget and account health")
check("recognizes exact provider sample", P._is_image_conversion_timeout(DETAIL))
check("not confused with first token timeout",
      not P._is_image_conversion_timeout("First Token Timeout or Upstream Timeout"))
check("plain network timeout remains separate", not P._is_image_conversion_timeout("socket timed out"))
check("nested SSE detail is recognized", P._is_image_conversion_timeout(json.dumps({"body": DETAIL})))
message, kind = P.friendly_upstream_error(504, DETAIL)
check("dedicated type and no fictitious retry claim",
      kind == "upstream_image_conversion_timeout" and "已对同账号自动重试" not in message)
check("request id remains available for provider investigation", "fake-request-id" in message)
exc = P.UpstreamStatus(504, DETAIL)
check("one envelope retry before output", P.should_retry_envelope(exc, False, 0))
check("envelope stops after one retry", not P.should_retry_envelope(exc, False, 1))
check("no retry after emitted output", not P.should_retry_envelope(exc, True, 0))
context = {}
check("HTTP consumes shared conversion retry", P._take_image_retry(context))
check("SSE cannot retry again after HTTP retry",
      not P.should_retry_envelope(exc, False, 0, usage_ctx=context))
context = {}
check("SSE consumes shared conversion retry",
      P.should_retry_envelope(exc, False, 0, usage_ctx=context))
check("HTTP cannot retry after SSE reopen", not P._take_image_retry(context))
acc = mock.Mock()
with mock.patch.object(P, "POOL") as pool:
    P._handle_envelope_account_cooldown(acc, exc, model="Sonus", session_key="fake-window")
    check("conversion timeout never cools healthy account", not acc.note_error.called)
    check("conversion timeout preserves affinity", not pool.affinity.unbind.called)

print("[image] no network, no silent image deletion, bounded memory")
remote = "https://image.example.invalid/original.png"
check("remote URL is never fetched or rewritten", Q.normalize_url(remote) == (remote, "reference"))
small = "data:image/png;base64," + base64.b64encode(b"small-original").decode()
check("small image is byte-identical", Q.normalize_url(small)[0] == small)
large = "data:image/png;base64," + base64.b64encode(b"invalid" * 50000).decode()
with mock.patch.dict(os.environ, {"QD_IMAGE_TRANSCODE": "off"}):
    check("off preserves original", Q.normalize_url(large) == (large, "disabled"))
with mock.patch.object(Q, "_codec", return_value=None):
    check("no Pillow preserves original and reports absence",
          Q.normalize_url(large) == (large, "codec_missing"))
with mock.patch.object(Q, "_MAX_CACHE_BYTES", 20), mock.patch.object(Q, "_MAX_CACHE_ITEMS", 2):
    with Q._LOCK:
        Q._CACHE.clear()
        Q._CACHE_BYTES = 0
    for i in range(5):
        Q._put(str(i), ("1234567890", "test"))
    check("LRU respects item and byte bounds", len(Q._CACHE) == 2 and Q._CACHE_BYTES == 20)
with Q._LOCK:
    Q._CACHE.clear()
    Q._CACHE_BYTES = 0

Image = Q._codec()
if Image is not None:
    image = Image.new("RGB", (512, 512))
    image.putdata([((x * 13) % 256, (y * 7) % 256, (x + y) % 256)
                   for y in range(512) for x in range(512)])
    buf = io.BytesIO()
    image.save(buf, format="PNG", compress_level=0)
    original = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    normalized, state = Q.normalize_url(original)
    out = base64.b64decode(normalized.split(",", 1)[1])
    with Image.open(io.BytesIO(out)) as decoded:
        check("pixel dimensions preserved", decoded.size == (512, 512))
        check("normalization produces a real decodable image", decoded.format in ("PNG", "JPEG"))
    check("transcode reduces bytes by at least 20%", len(normalized) < len(original) * 0.8)
    check("same picture deterministic across retries", Q.normalize_url(original)[0] == normalized)
    check("cached conversion not rerun", Q.normalize_url(original)[1] == "cache_hit")
    with mock.patch.dict(os.environ, {"QD_IMAGE_TRANSCODE": "off"}):
        check("off bypasses cached conversion too", Q.normalize_url(original)[0] == original)
    alpha = Image.new("RGBA", (300, 300), (255, 20, 30, 100))
    buf = io.BytesIO()
    alpha.save(buf, format="PNG", compress_level=0)
    alpha_uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    alpha_out, _ = Q.normalize_url(alpha_uri)
    with Image.open(io.BytesIO(base64.b64decode(alpha_out.split(",", 1)[1]))) as im:
        check("transparent image remains lossless PNG with original alpha",
              im.format == "PNG" and im.convert("RGBA").getpixel((0, 0))[3] == 100)
    check("invalid data retained (no silent drop)", Q.normalize_url(large)[0] == large)
    from PIL.PngImagePlugin import PngInfo
    for mode in ("RGB", "RGBA"):
        sample = Image.new(mode, (512, 512), (60, 80, 100) if mode == "RGB" else (60, 80, 100, 70))
        metadata = PngInfo()
        metadata.add_text("source", "test provenance")
        metadata.add(b"gAMA", struct.pack(">I", 100000))
        data = io.BytesIO()
        sample.save(data, format="PNG", compress_level=0, pnginfo=metadata)
        uri = "data:image/png;base64," + base64.b64encode(data.getvalue()).decode()
        check(mode + " gamma/text metadata retained byte-for-byte", Q.normalize_url(uri)[0] == uri)
    with mock.patch.object(Q, "_MAX_PIXELS", 10):
        Q._CACHE.clear()
        Q._CACHE_BYTES = 0
        check("pixel limit fails open without decoding huge image", Q.normalize_url(original)[1] == "too_many_pixels")
    Q._CACHE.clear()
    Q._CACHE_BYTES = 0
    for label, save_options, mode in (
            ("DPI", {"dpi": (300, 300)}, "RGB"),
            ("ICC", {"icc_profile": b"synthetic profile"}, "RGB"),
            ("rotated EXIF", {"exif": Image.Exif()}, "RGB"),
            ("16-bit depth", {}, "I;16")):
        if label == "rotated EXIF":
            save_options["exif"][274] = 6
        pic = Image.new(mode, (512, 512))
        data = io.BytesIO()
        pic.save(data, format="PNG", compress_level=0, **save_options)
        uri = "data:image/png;base64," + base64.b64encode(data.getvalue()).decode()
        check(label + " original metadata/depth unchanged", Q.normalize_url(uri)[0] == uri)
    palette = Image.new("P", (600, 600))
    palette.putpalette([255, 0, 0, 0, 255, 0] + [0] * 762)
    palette.info["transparency"] = 0
    data = io.BytesIO()
    palette.save(data, format="PNG", compress_level=0)
    uri = "data:image/png;base64," + base64.b64encode(data.getvalue()).decode()
    converted, _ = Q.normalize_url(uri)
    with Image.open(io.BytesIO(base64.b64decode(converted.split(",", 1)[1]))) as im:
        check("palette tRNS retains full alpha transparency", im.convert("RGBA").getpixel((0, 0))[3] == 0)
    import zlib
    data = io.BytesIO()
    Image.new("RGBA", (512, 512), (10, 20, 30, 80)).save(data, format="PNG", compress_level=0)
    raw = data.getvalue()
    chunk = b"pHYs" + struct.pack(">IIB", 2, 1, 0)
    raw = raw[:33] + struct.pack(">I", 9) + chunk + struct.pack(">I", zlib.crc32(chunk) & 0xffffffff) + raw[33:]
    uri = "data:image/png;base64," + base64.b64encode(raw).decode()
    check("PNG non-square pixel aspect metadata retained", Q.normalize_url(uri)[0] == uri)
    frames = [Image.new("RGBA", (512, 512), colour) for colour in ((255, 0, 0, 128), (0, 255, 0, 255))]
    data = io.BytesIO()
    frames[0].save(data, format="PNG", save_all=True, append_images=frames[1:], compress_level=0)
    uri = "data:image/png;base64," + base64.b64encode(data.getvalue()).decode()
    check("APNG animation preserved, never flattened to frame one", Q.normalize_url(uri)[0] == uri)
    source = [{"role": "user", "content": [
        {"type": "text", "text": "compare original"},
        {"type": "image_url", "image_url": {"url": original, "detail": "high"}}]}]
    before = copy.deepcopy(source)
    prepared, stats = Q.prepare_messages(source)
    check("caller input not mutated", source == before)
    check("image count/order/text preserved", len(prepared[0]["content"]) == 2
          and stats["images"] == 1 and prepared[0]["content"][0] == before[0]["content"][0])
    check("detail metadata preserved", prepared[0]["content"][1]["image_url"]["detail"] == "high")
    account = A.Account({"uid": "image-test", "realm": "cn", "accessToken": "fake"})
    body = P.build_qoder_body({"model": "Sonus", "messages": source}, account, "auto", "cn")
    urls = [p["image_url"]["url"] for m in body["messages"]
            if isinstance(m.get("content"), list) for p in m["content"] if p.get("type") == "image_url"]
    check("actual outbound builder uses normalized picture", urls == [normalized])
    check("original payload remains unmodified after builder", source == before)
    repeated = [{"type": "image_url", "image_url": {"url": original}},
                {"type": "text", "text": "between"},
                {"type": "image_url", "image_url": {"url": original}}]
    bridges = [P.responses_to_chat({"model": "Sonus", "input": [{"role": "user", "content": repeated}]}),
               P.qoder_anthropic.messages_to_chat({"model": "Sonus", "max_tokens": 8,
                   "messages": [{"role": "user", "content": [
                       {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": original.split(",", 1)[1]}},
                       {"type": "text", "text": "between"},
                       {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": original.split(",", 1)[1]}}]}]})]
    for label, chat in zip(("Responses", "Anthropic"), bridges):
        if isinstance(chat, tuple):
            chat = chat[0]
        assembled = P.build_qoder_body(chat, account, "auto", "cn")
        contents = [m["content"] for m in assembled["messages"] if isinstance(m.get("content"), list)][0]
        # Existing Anthropic bridge groups text before images; preserve that policy.
        expected_types = (["image_url", "text", "image_url"] if label == "Responses"
                          else ["text", "image_url", "image_url"])
        check(label + " bridge retains repeated pictures and existing ordering",
              [part["type"] for part in contents] == expected_types)
        uris = [part["image_url"]["url"] for part in contents if part.get("type") == "image_url"]
        check(label + " bridge uses identical normalized picture bytes", uris == [normalized, normalized])
    with mock.patch.dict(os.environ, {"QD_KEEP_TOPLEVEL_IMAGES": "1"}):
        redundant = P.build_qoder_body({"model": "Sonus", "messages": source}, account, "auto", "cn")
        check("legacy redundant image fields also use normalized bytes",
              redundant.get("image_urls") == [normalized]
              and redundant["chat_context"].get("imageUrls") == [normalized])
else:
    skip("real codec assertions", "optional Pillow unavailable; dependency-free fallback tested")

print("[image] open_upstream HTTP failures do not rotate the pool")
with tempfile.TemporaryDirectory() as directory:
    pool = A.AccountPool(directory, log=lambda *a, **k: None)
    for i in range(3):
        account = A.Account({"uid": "img-%d" % i, "realm": "cn", "accessToken": "fake"})
        account.expires_at = 0
        account.enabled = True
        pool.accounts.append(account)
    sent = []
    def fail_provider(request, **kwargs):
        sent.append(request.data)
        raise urllib.error.HTTPError(request.full_url, 504, "timeout", {},
                                     io.BytesIO(DETAIL.encode()))
    with mock.patch.object(P, "POOL", pool), \
         mock.patch.object(P, "SESSIONS", types.SimpleNamespace(get=lambda a: types.SimpleNamespace(headers=lambda *a, **k: {}))), \
         mock.patch.object(P, "validate_public_http_url", side_effect=lambda x: x), \
         mock.patch.object(P.urllib.request, "urlopen", side_effect=fail_provider), \
         mock.patch.object(P.time, "sleep"), \
         mock.patch.object(P, "TRANSIENT_MAX_RETRIES", 2):
        try:
            P.open_upstream({"model": "Qwen3.8-Flash", "messages": [{"role": "user", "content": "test"}]},
                            session_key="image-session", target_realm="cn")
        except urllib.error.HTTPError as error:
            check("original 504/detail preserved", error.code == 504 and error.qoder_detail == DETAIL)
        else:
            check("provider failure must not become fake success", False)
    check("at most two HTTP attempts, not two per account", len(sent) == 2, len(sent))
    check("retry sends identical payload (no missing images/history)", len(set(sent)) == 1)
    check("all accounts remain healthy", all(not a.last_error for a in pool.accounts))

print("[image] combined HTTP/SSE attempts share one request budget")
with tempfile.TemporaryDirectory() as directory:
    pool = A.AccountPool(directory, log=lambda *a, **k: None)
    for index in range(2):
        item = A.Account({"uid": "mixed-%d" % index, "realm": "cn", "accessToken": "fake"})
        item.enabled = True
        item.expires_at = 0
        pool.accounts.append(item)
    payload = {"model": "Qwen3.8-Flash", "messages": [{"role": "user", "content": "test"}]}
    for first_layer in ("HTTP", "SSE"):
        attempts = []
        def mixed_provider(request, **kwargs):
            attempts.append(request.data)
            # HTTP first: failure -> SSE error; SSE first: SSE error -> HTTP failure.
            is_http = (len(attempts) == 1) if first_layer == "HTTP" else len(attempts) > 1
            if is_http:
                raise urllib.error.HTTPError(request.full_url, 504, "timeout", {}, io.BytesIO(DETAIL.encode()))
            return io.BytesIO(("data: " + json.dumps({"statusCodeValue": 504, "body": DETAIL}) + "\n\n").encode())
        with mock.patch.object(P, "POOL", pool), \
             mock.patch.object(P, "SESSIONS", types.SimpleNamespace(get=lambda a: types.SimpleNamespace(headers=lambda *a, **k: {}))), \
             mock.patch.object(P, "validate_public_http_url", side_effect=lambda x: x), \
             mock.patch.object(P.urllib.request, "urlopen", side_effect=mixed_provider), \
             mock.patch.object(P.time, "sleep"), \
             mock.patch.object(P, "TRANSIENT_MAX_RETRIES", 2):
            context = {}
            response, account, _ = P.open_upstream(payload, session_key="mixed", target_realm="cn", usage_ctx=context)
            try:
                P.aggregate_with_envelope_retry(response, payload, "mixed", "cn", "Qwen3.8-Flash",
                                                {"usage": None}, account, usage_ctx=context)
            except P.UpstreamStatus as error:
                check(first_layer + " mixed error remains 504", int(error.status) == 504)
            else:
                check(first_layer + " mixed error must fail", False)
            finally:
                response.close()
        check(first_layer + " mixed path total POST count is exactly two", len(attempts) == 2, len(attempts))
        check(first_layer + " mixed path consumes only one retry", context.get("_image_conversion_retries") == 1)
        check(first_layer + " mixed path has no account penalties", all(not x.last_error for x in pool.accounts))

print("[image] original team lease is released before same-account reopen")
team = A.Account({"uid": "team-image", "realm": "cn", "accessToken": "fake"})
team.team_leases = {"old-lease": time.time()}
first = P._TeamLeaseResp(io.BytesIO(("data: " + json.dumps({
    "statusCodeValue": 504, "body": DETAIL}) + "\n\n").encode()), team, "old-lease")
seen_leases = []
def team_reopen(*args, **kwargs):
    seen_leases.append(dict(team.team_leases))
    check("team reopen pinned to same account", kwargs.get("only_account") is team)
    return io.BytesIO(("data: " + json.dumps({"statusCodeValue": 504, "body": DETAIL}) + "\n\n").encode()), team, ""
with mock.patch.object(P, "open_upstream", side_effect=team_reopen), mock.patch.object(P.time, "sleep"):
    try:
        P.aggregate_with_envelope_retry(first, {"model": "Sonus"}, "team-window", "cn", "Sonus",
                                        {"usage": None}, team, usage_ctx={})
    except P.UpstreamStatus:
        pass
check("old lease absent at reopen time (no 30s self-deadlock)", seen_leases == [{}], seen_leases)
check("old lease release is idempotent", team.team_leases == {})
first.close()

print("[image] real HTTP handlers across three protocols")
with tempfile.TemporaryDirectory() as directory:
    with mock.patch.object(P, "API_KEY", ""), mock.patch.object(P, "auth_required", return_value=False), \
         mock.patch.object(P.Handler, "_model_precheck", return_value=None), \
         mock.patch.object(P, "USAGE_LOG", os.path.join(directory, "usage.jsonl")), \
         mock.patch.object(P, "USAGE_SUMMARY", os.path.join(directory, "summary.json")):
        server = P.ThreadingHTTPServer(("127.0.0.1", 0), P.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for route in ("/v1/chat/completions", "/v1/responses", "/v1/messages"):
                with mock.patch.object(P, "open_upstream", side_effect=P.UpstreamStatus(504, DETAIL)):
                    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
                    request = {"model": "Sonus", "messages": [{"role": "user", "content": "image"}],
                               "input": "image", "max_tokens": 8, "stream": False}
                    conn.request("POST", route, json.dumps(request), {"Content-Type": "application/json"})
                    response = conn.getresponse()
                    result = json.loads(response.read())
                    check(route + " retains HTTP 504", response.status == 504)
                    check(route + " exposes image-specific reason", "图片转换超时" in str(result))
                    conn.close()
            # HTTP 200 can contain an error envelope after the stream starts.
            # Exercise those branches, not just errors thrown by open_upstream.
            for route in ("/v1/chat/completions", "/v1/responses", "/v1/messages"):
                calls = []
                reopened_accounts = []
                fake_account = mock.Mock(uid="image-protocol", realm="intl")
                def envelope_failure(*args, **kwargs):
                    calls.append(1)
                    reopened_accounts.append(kwargs.get("only_account"))
                    return io.BytesIO(("data: " + json.dumps({
                        "statusCodeValue": 504, "body": DETAIL}) + "\n\n").encode()), fake_account, ""
                with mock.patch.object(P, "open_upstream", side_effect=envelope_failure), \
                     mock.patch.object(P.time, "sleep"), \
                     mock.patch.object(P, "TRANSIENT_MAX_RETRIES", 2):
                    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
                    request = {"model": "Sonus", "messages": [{"role": "user", "content": "image"}],
                               "input": "image", "max_tokens": 8, "stream": True}
                    conn.request("POST", route, json.dumps(request), {"Content-Type": "application/json"})
                    response = conn.getresponse()
                    result = response.read().decode("utf-8")
                    check(route + " SSE exposes image timeout", "图片转换超时" in result)
                    check(route + " never emits fake completion after timeout",
                          "response.completed" not in result)
                    expected = 1 if route == "/v1/messages" else 2
                    check(route + " SSE attempt count matches protocol policy", len(calls) == expected, len(calls))
                    check(route + " image retry stays on original account",
                          len(calls) == 1 or reopened_accounts[-1] is fake_account)
                    check(route + " SSE account stays healthy", not fake_account.note_error.called)
                    conn.close()
            for route in ("/v1/chat/completions", "/v1/responses", "/v1/messages"):
                calls = []
                def partial_then_error(*args, **kwargs):
                    calls.append(1)
                    chunk = {"id": "partial-image", "model": "Sonus", "choices": [
                        {"index": 0, "delta": {"content": "PARTIAL_VISIBLE_TEXT"}, "finish_reason": None}]}
                    frames = ["data: " + json.dumps({"statusCodeValue": 200, "body": json.dumps(chunk)}),
                              "data: " + json.dumps({"statusCodeValue": 504, "body": DETAIL})]
                    return io.BytesIO(("\n\n".join(frames) + "\n\n").encode()), mock.Mock(uid="partial", realm="intl"), ""
                with mock.patch.object(P, "open_upstream", side_effect=partial_then_error), \
                     mock.patch.object(P.time, "sleep"):
                    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
                    conn.request("POST", route, json.dumps({"model": "Sonus", "stream": True,
                        "messages": [{"role": "user", "content": "image"}], "input": "image", "max_tokens": 8}),
                        {"Content-Type": "application/json"})
                    response = conn.getresponse()
                    text = response.read().decode("utf-8")
                    events = [json.loads(line[5:]) for line in text.splitlines()
                              if line.startswith("data:") and line[5:].strip() != "[DONE]"]
                    delta_count = 0
                    for event in events:
                        if route == "/v1/chat/completions":
                            delta_count += int((((event.get("choices") or [{}])[0].get("delta") or {}).get("content")) == "PARTIAL_VISIBLE_TEXT")
                        elif route == "/v1/responses":
                            delta_count += int(event.get("type") == "response.output_text.delta" and event.get("delta") == "PARTIAL_VISIBLE_TEXT")
                        else:
                            delta_count += int((event.get("delta") or {}).get("text") == "PARTIAL_VISIBLE_TEXT")
                    check(route + " existing output is not replayed", delta_count == 1, delta_count)
                    check(route + " never retries after output", len(calls) == 1, len(calls))
                    check(route + " after-output failure remains explicit",
                          "图片转换超时" in text and "response.completed" not in text)
                    conn.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
finish()
